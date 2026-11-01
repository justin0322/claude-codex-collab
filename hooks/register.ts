import type { EngineInterface, PluginOptions, Register } from 'claude-code'

// codex-collab：Claude Code × Codex 全自動協作。邏輯在 backend/collab.py，這裡只接事件：
//   prompt.submit  記下回合開始時的工作區快照；附上協作／單獨模式的提醒（討論時要先徵詢 Codex）
//   turn.complete  回合有改檔 → 背景送 Codex 審查（額度不足 → Claude 單獨，記下來之後補審）
//   每 30 秒       檢查背景審查；完成就交給 Claude 驗證（同一串最多 max_rounds 輪）
//   tool.call Bash （g2_guard 開啟時）指定前綴的 branch 上開始訓練前，HEAD 要有 G2 審查紀錄
//   /collab        status｜off｜on（只影響這個對話）｜pause｜resume（全部對話）｜skip-g2
//   狀態列         Codex：協作中(Reviewing code... 3m)／等待中／閒置／關閉／⚠ 關閉(暫停／額度)

const TICK_MS = 30_000
// 粗篩，與 collab.py 的 TRAIN_RE 相同：只認指令位置上的訓練指令
const TRAIN_RE =
  /(?:^|[;&|(\n]|\bnohup\b|\btime\b|\bsetsid\b)\s*(?:\w+=\S*\s+)*(?:torchrun\b|accelerate\s+launch\b|deepspeed\b|(?:\S*\/)?python[0-9.]*\s+(?:-\S+\s+)*\S*train\S*\.py\b|(?:bash|sh)\s+\S*(?:run_(?!codex\b)|train)\S*\.sh\b|\.\/\S*(?:run_(?!codex\b)|train)\S*\.sh\b)/

type Py = { exitCode: number; stdout: string; stderr: string }
// state：working｜waiting｜idle｜off｜solo；gate：AUTO／CONSULT／G1–G4／FOLLOWUP；started：秒
type St = {
  mode: string
  detail: string
  weekly_pct: number | null
  running: string | null
  state?: string
  gate?: string | null
  started?: number | null
}
// Codex 正在做什麼（狀態列括號裡的字）
const ACTIVITY: Record<string, string> = {
  G1: 'Reviewing design',
  G2: 'Reviewing code',
  G3: 'Rechecking numbers',
  G4: 'Reviewing claims',
  CONSULT: 'Discussing',
  AUTO: 'Auto-reviewing',
  FOLLOWUP: 'Follow-up',
}
type Tick = {
  finished: { run_id: string; status: string; round: number; error: string }[]
  launched: string | null
  status?: St
}
type End = { action: string; message?: string }
type G2 = { allow: boolean; reason?: string; warn?: string }

let sid = ''
let backend = ''
let enabled: boolean | undefined
let ticking = false
let opts: PluginOptions = {}

async function py($: EngineInterface, args: string[], stdin?: string, timeoutMs = 20_000): Promise<Py> {
  return $.process.run(['python3', `${backend}/collab.py`, ...args], { stdin, timeoutMs })
}

async function ensure($: EngineInterface): Promise<boolean> {
  if (enabled !== undefined) return enabled
  enabled = false
  try {
    backend = `${$.plugin.root}/backend`
    sid = await $.session.id()
    const cwd = await $.session.cwd()
    const top = await $.process.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'], { timeoutMs: 10_000 })
    const repo = top.exitCode === 0 ? top.stdout.trim() : ''
    const want = String(opts.repo_root ?? '').replace(/\/+$/, '')
    if (!repo || (want && repo !== want)) return false
    const cfg = {
      repo,
      reviewer_mode: opts.reviewer_mode ?? 'sandbox',
      reviewer_user: opts.reviewer_user ?? 'codexrev',
      review_dir: opts.review_dir ?? 'docs/cross-check',
      auto_review: opts.auto_review ?? true,
      auto_limit: opts.auto_limit ?? 90,
      max_rounds: opts.max_rounds ?? 2,
      g2_guard: opts.g2_guard ?? false,
      g2_branch_prefix: opts.g2_branch_prefix ?? 'exp/',
    }
    const r = await py($, ['configure', '--sid', sid], JSON.stringify(cfg))
    enabled = r.exitCode === 0
  } catch {
    enabled = false
  }
  return enabled
}

function parse<T>(r: Py): T | undefined {
  try {
    return JSON.parse(r.stdout) as T
  } catch {
    return undefined
  }
}

function stateOf(s: St): string {
  return s.state ?? (s.mode !== 'collab' ? s.mode : s.running ? 'working' : 'idle')
}

// 經過時間：<1m、3m、1h05m（狀態每 30 秒更新一次，精確到分鐘就夠）
function elapsed(sec: number): string {
  const m = Math.floor(Math.max(0, sec) / 60)
  if (m < 1) return '<1m'
  if (m < 60) return `${m}m`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

// 協作中(Reviewing code... 3m)；省略號用 ASCII 的 ...，終端機寬度才固定
function working(s: St, nowSec: number): string {
  const act = (s.gate && ACTIVITY[s.gate]) || 'Working'
  return `協作中(${act}...${s.started ? ` ${elapsed(nowSec - s.started)}` : ''})`
}

// 狀態列：協作中(...)＝Codex 正在跑；等待中＝審查完成還沒交給 Claude，或有變更排隊等補審；
// 閒置＝開著協作但沒有工作；關閉＝這個對話 /collab off；⚠ 關閉＝全域暫停或額度不足
function label(s: St | undefined, nowSec: number): string | undefined {
  if (!s) return undefined
  const st = stateOf(s)
  if (st === 'working') return `Codex：${working(s, nowSec)}`
  if (st === 'waiting') return 'Codex：等待中'
  if (st === 'idle') return 'Codex：閒置'
  if (st === 'off') return 'Codex：關閉'
  return `Codex：⚠ 關閉(${/pause|暫停/i.test(s.detail) ? '暫停' : '額度'})`
}

function describe(s: St, nowSec: number): string {
  const reason = s.detail.replace(/^(solo|off):/, '')
  const st = stateOf(s)
  if (st === 'working') return `${working(s, nowSec)}${s.running ? `：${s.running}` : ''}`
  if (st === 'waiting') return '等待中（審查完成、待 Claude 處理，或有變更排隊等補審）'
  if (st === 'idle') return '閒置（開著協作，目前沒有工作）'
  if (st === 'off') return `關閉（${reason}）`
  return `⚠ 關閉（${reason}）`
}

async function tick($: EngineInterface): Promise<void> {
  if (ticking || !(await ensure($))) return
  ticking = true
  try {
    const out = parse<Tick>(await py($, ['tick', '--sid', sid], undefined, 60_000))
    if (!out) return
    for (const f of out.finished) {
      if (f.status === 'done') {
        const text = (await py($, ['deliver', f.run_id])).stdout.trim()
        if (!text) continue
        $.ui.toast(`Codex 審查完成（第 ${f.round} 輪），交給 Claude 驗證`)
        void $.ui.notify(`Codex 審查完成（第 ${f.round} 輪）`, { title: 'codex-collab' })
        void $.prompt.submit({ text })
      } else if (f.status === 'quota') {
        $.ui.toast('Codex 額度用完：切換為 Claude 單獨模式，恢復後自動補審')
      } else {
        $.ui.toast(`Codex 背景審查沒有完成：${f.error || f.status}`)
      }
    }
    if (out.launched) $.ui.toast('Codex 開始補審先前累積的變更')
    $.ui.status(label(out.status, (await $.clock.now()) / 1000))
  } catch {
    // 這一輪失敗就等下一輪
  } finally {
    ticking = false
  }
}

export const register: Register = (on, options) => {
  opts = options

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (await ensure($)) {
      await $.command.register({
        name: 'collab',
        description: 'Codex 協作：status｜off／on（只關／開這個對話）｜pause／resume（全部對話）｜skip-g2',
      })
      void tick($)
      $.clock.every(TICK_MS, () => {
        void tick($)
      })
    }
    return started
  })

  on('prompt.submit', async ($, e, next) => {
    if (!(await ensure($)) || e.turnId) return next(e) // 併入進行中回合的訊息不另起快照
    const o = e.origin
    const isMine = o.kind === 'plugin' && o.name === $.plugin.name
    const origin = o.kind === 'composer' || o.kind === 'bridge' ? 'user' : isMine ? 'plugin' : 'continue'
    try {
      await py($, ['turn-start', '--sid', sid], JSON.stringify({ origin, prompt: e.text }))
    } catch {
      // 快照失敗：這回合不自動審查
    }
    if (e.command || origin !== 'user') return next(e)
    let ctx = ''
    try {
      ctx = (await py($, ['context', '--sid', sid])).stdout.trim()
    } catch {
      ctx = ''
    }
    return ctx ? next({ ...e, context: [...(e.context ?? []), ctx] }) : next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const res = await next(e)
    if (e.agentId || !(await ensure($))) return res
    try {
      const out = parse<End>(await py($, ['turn-end', '--sid', sid]))
      if (out?.message && out.action !== 'nochange') $.ui.toast(out.message)
      if (out?.action === 'launched') $.ui.status('Codex：協作中(Auto-reviewing... <1m)')
    } catch {
      // 失敗就不送審
    }
    return res
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (!opts.g2_guard || !TRAIN_RE.test(e.command) || !(await ensure($))) return next(e)
    const v = parse<G2>(await py($, ['check-g2', '--sid', sid], e.command, 15_000))
    if (v && v.allow === false) return { deny: v.reason ?? '[codex-collab] 這個 commit 還沒有 G2 審查紀錄' }
    if (v?.warn) $.ui.toast(v.warn)
    return next(e)
  }).catch(($, e, next) => next(e)) // 守門程式本身壞掉時不擋使用者的工作

  on('command.run', { command: 'collab' }, async ($, e) => {
    if (!(await ensure($))) return { text: 'codex-collab 在這個對話沒有啟用（不在 git repo，或不是設定的 repo_root）。' }
    const arg = e.args.trim().split(/\s+/)[0] || 'status'
    if (arg === 'off' || arg === 'on') {
      await py($, [arg, '--sid', sid])
      void tick($)
      return {
        text:
          arg === 'off'
            ? '這個對話已關閉 Codex 協作（純 Claude）：不送審、不徵詢 Codex。其他對話不受影響。/collab on 重新開啟。'
            : '這個對話已開啟 Codex 協作。',
      }
    }
    if (arg === 'pause' || arg === 'resume') {
      await py($, [arg])
      void tick($)
      return { text: arg === 'pause' ? '已暫停所有對話的自動協作（Claude 單獨）。/collab resume 恢復。' : '已恢復自動協作。' }
    }
    if (arg === 'skip-g2') {
      const head = (await py($, ['skip-g2', '--sid', sid])).stdout.trim()
      return { text: `已記錄：commit ${head.slice(0, 8)} 由使用者同意略過 G2。` }
    }
    const s = parse<St>(await py($, ['status', '--sid', sid]))
    if (!s) return { text: 'collab.py 沒有回應' }
    return {
      text: `codex-collab：${describe(s, (await $.clock.now()) / 1000)}｜Codex 週額度 ${s.weekly_pct ?? '?'}%（>= ${opts.auto_limit ?? 90}% 停止自動協作）`,
    }
  })
}
