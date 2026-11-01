#!/usr/bin/env python3
"""codex-collab 後端：Claude Code × Codex 全自動協作。

mod（hooks/register.ts）透過子程序呼叫這支程式；也可以手動執行除錯。
每個 Claude session 的設定與狀態分開存在 ~/.claude/codex-collab-state/sessions/<sid>/。

子指令（--sid 也可用環境變數 COLLAB_SID 指定）：
  configure --sid S           stdin JSON：mod 寫入這個 session 的設定（repo、唯讀方式、門檻…）
  config-sh --sid S           印出 shell 變數，給 run_codex.sh eval
  snapshot --sid S            把 repo 的程式／文件檔寫成 git tree，印出 hash（不動真正的 index）
  quota                       印出 Codex 額度 JSON
  mode [limit] --sid S        印出 collab｜off:<原因>｜solo:<原因>
  block <reason>              記錄「Codex 額度用完」直到重置時間
  pause | resume              暫停／恢復自動協作（所有 session）
  off | on --sid S            只關閉／開啟這個 session 的協作
  turn-start --sid S          stdin JSON {origin, prompt}：記下回合開始時的快照
  turn-end --sid S            有變更就在背景送 AUTO 審查；印出 JSON {action, message}
  tick --sid S                檢查背景審查；積欠的變更在允許時補審
  deliver <run_id>            印出交給 Claude 的「審查完成」訊息
  context --sid S             印出使用者每次發訊息時附給 Claude 的提醒
  check-g2 --sid S            stdin 為 Bash 指令；印出 JSON {allow, reason?, warn?}
  skip-g2 --sid S             目前 HEAD 免 G2（使用者同意時）
  status --sid S              狀態 JSON
"""
import datetime
import glob
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import time

BACKEND = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.claude/codex-collab-state")
JOBS = os.path.join(STATE, "jobs")
AUTO_TIMEOUT = 1200  # 自動審查限時（秒）
WATCH_GLOBS = ["*.py", "*.sh", "*.md", "*.yaml", "*.yml", "*.toml", "*.cfg", "*.ipynb",
               "*.js", "*.ts", "*.tsx", "*.r", "*.R", "*.jl", "*.m", "*.tex"]
TRAIN_RE = re.compile(  # 只認「指令位置」上的訓練指令，避免 grep／printf 字串裡的 train.py 誤判
    r"(?:^|[;&|(\n]|\bnohup\b|\btime\b|\bsetsid\b)\s*(?:\w+=\S*\s+)*"
    r"(?:torchrun\b|accelerate\s+launch\b|deepspeed\b|"
    r"(?:\S*/)?python[0-9.]*\s+(?:-\S+\s+)*\S*train\S*\.py\b|"
    r"(?:bash|sh)\s+\S*(?:run_(?!codex\b)|train)\S*\.sh\b|\./\S*(?:run_(?!codex\b)|train)\S*\.sh\b)")
DEFAULTS = {"repo": "", "reviewer_mode": "sandbox", "reviewer_user": "codexrev", "review_dir": "docs/cross-check",
            "auto_review": True, "auto_limit": 90, "max_rounds": 2, "g2_guard": False, "g2_branch_prefix": "exp/"}

os.makedirs(JOBS, exist_ok=True)
SID = None
CFG = dict(DEFAULTS)


def now():
    return time.time()


def jload(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jsave(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sdir(sid=None):
    d = os.path.join(STATE, "sessions", re.sub(r"[^A-Za-z0-9_-]", "_", sid or SID or "manual"))
    os.makedirs(d, exist_ok=True)
    return d


def load_cfg(sid):
    c = dict(DEFAULTS)
    c.update(jload(os.path.join(sdir(sid), "config.json"), {}) or {})
    return c


def reviewer_home(c=None):
    c = c or CFG
    if c["reviewer_mode"] != "user":
        return os.path.expanduser("~")
    try:
        return pwd.getpwnam(c["reviewer_user"]).pw_dir
    except KeyError:
        return ""


def out_root(c=None):
    c = c or CFG
    return os.path.join(c["repo"], c["review_dir"], "raw")


def git(*args, env=None, input=None):
    return subprocess.run(["git", "-C", CFG["repo"], *args], capture_output=True, text=True, env=env, input=input)


# ---------- 快照 ----------
def pathspec():
    spec = [f":(glob)**/{g}" for g in WATCH_GLOBS]
    return spec + [f":(exclude){CFG['review_dir'].strip('/')}/**"]


def snapshot():
    """暫存 index 只放符合的檔案（含未追蹤、不含 gitignore 的），寫成 tree。
    git add 遇到沒對到任何檔案的 pathspec 會整批失敗，所以先列出檔案再精確加入／移除。"""
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(sdir(), "index"))
    lit = dict(env, GIT_LITERAL_PATHSPECS="1")
    on_disk = set(filter(None, git("ls-files", "-co", "--exclude-standard", "-z", "--", *pathspec()).stdout.split("\0")))
    on_disk = {p for p in on_disk if os.path.isfile(os.path.join(CFG["repo"], p))}
    in_index = set(filter(None, git("ls-files", "-z", env=env).stdout.split("\0")))
    gone = sorted(in_index - on_disk)
    if gone:
        git("rm", "-q", "--cached", "--pathspec-from-file=-", "--pathspec-file-nul", env=lit, input="\0".join(gone))
    if on_disk:
        git("add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", env=lit, input="\0".join(sorted(on_disk)))
    return git("write-tree", env=env).stdout.strip()


# ---------- 額度 ----------
def read_quota():
    """最新的 rate_limits：Codex 每次回應都寫進 ~/.codex/sessions/**/rollout-*.jsonl。"""
    dirs = {os.path.expanduser("~/.codex/sessions")}
    h = reviewer_home()
    if h:
        dirs.add(os.path.join(h, ".codex", "sessions"))
    files = []
    for d in dirs:
        files += glob.glob(os.path.join(d, "**", "rollout-*.jsonl"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    for f in files[:5]:
        last = None
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits":{' in line:
                        last = line
        except OSError:
            continue
        if not last:
            continue
        rl = (json.loads(last).get("payload") or {}).get("rate_limits") or {}
        wins = [w for w in (rl.get("primary"), rl.get("secondary")) if w]
        weekly = next((w for w in wins if w.get("window_minutes") == 10080), None)
        short = next((w for w in wins if w.get("window_minutes") == 300), None)
        return {"weekly_pct": weekly.get("used_percent") if weekly else None,
                "weekly_resets": weekly.get("resets_at") if weekly else None,
                "short_pct": short.get("used_percent") if short else None,
                "short_resets": short.get("resets_at") if short else None}
    return {}


def fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"


def is_off():
    return os.path.exists(os.path.join(sdir(), "off"))


def mode(limit=None):
    """collab｜off:<原因>（只有這個 session）｜solo:<原因>（全域：暫停或額度）"""
    limit = CFG["auto_limit"] if limit is None else limit
    if is_off():
        return "off:這個對話已關閉協作（/collab on 開啟）"
    if os.path.exists(os.path.join(STATE, "paused")):
        return "solo:使用者暫停了自動協作（/collab resume 恢復）"
    t = now()
    bp = os.path.join(STATE, "quota_block.json")
    blk = jload(bp, {})
    if blk.get("until", 0) > t:
        return f"solo:Codex 額度用完，{fmt_time(blk['until'])} 重置"
    if blk:
        os.remove(bp)
    q = read_quota()
    pct, resets = q.get("weekly_pct"), q.get("weekly_resets")
    if pct is not None and pct >= limit and (resets or 0) > t:
        return f"solo:Codex 週額度 {pct:.0f}%（門檻 {limit:.0f}%），{fmt_time(resets)} 重置"
    return "collab"


def block(reason):
    q = read_quota()
    cands = [x for x in (q.get("short_resets"), q.get("weekly_resets")) if x and x > now()]
    until = min(cands) if cands else now() + 3600
    jsave(os.path.join(STATE, "quota_block.json"), {"until": until, "reason": reason, "at": now()})


# ---------- 背景審查 ----------
def jobs(sid=None):
    out = []
    for p in sorted(glob.glob(os.path.join(JOBS, "*.json"))):
        j = jload(p)
        if j and (sid is None or j.get("sid") == sid):
            out.append(j)
    return out


def refresh(job):
    if job["status"] != "running":
        return job
    exit_file = os.path.join(JOBS, job["run_id"] + ".exit")
    if os.path.exists(exit_file):
        code = int(open(exit_file).read().strip() or "1")
        job["status"] = "done" if code == 0 else ("quota" if code == 7 else "failed")
        job["exit"], job["finished"] = code, now()
        log = os.path.join(JOBS, job["run_id"] + ".log")
        tail = open(log, encoding="utf-8", errors="replace").read().strip().splitlines() if os.path.exists(log) else []
        job["error"] = tail[-1] if (code and tail) else ""
    elif now() - job["started"] > AUTO_TIMEOUT + 300:
        job["status"], job["exit"], job["finished"] = "failed", -1, now()
        job["error"] = "背景程序沒有回報結束（可能被中斷）"
    jsave(os.path.join(JOBS, job["run_id"] + ".json"), job)
    return job


def running_job():
    for j in jobs():
        if refresh(j)["status"] == "running":
            return j
    return None


def chain():
    return jload(os.path.join(sdir(), "chain.json"), {"round": 0, "prompt": "", "prev_run": None})


def write_brief(path, base, head, rnd, prompt, prev_run):
    names = git("diff", "--name-status", base, head).stdout.strip() or "（無）"
    dp = os.path.join(STATE, "disputes.md")
    disputes = open(dp, encoding="utf-8").read().strip() if os.path.exists(dp) else ""
    prev = ""
    if rnd > 1 and prev_run:
        ledger = os.path.join(CFG["repo"], CFG["review_dir"], f"{prev_run}.md")
        prev = (f"\n上一輪是 `{prev_run}`：Codex 原始意見在 `{out_root()}/{prev_run}/result.json`"
                + (f"，Claude 的逐條判定與處理在 `{ledger}`" if os.path.exists(ledger) else "（Claude 的判定紀錄尚未寫入）")
                + "。請先確認上一輪的 critical／major 是否已經解決；已解決或已被合理駁回的，不要再提。")
    quoted = (prompt.strip()[:3000] or "（無文字）").replace("\n", "\n> ")
    text = f"""### 本回合的工作
使用者這次的要求（原文）：
> {quoted}

### 變更範圍
`{base[:12]}` 和 `{head[:12]}` 是工作區快照的 git tree 物件，涵蓋程式與文件檔，包含還沒 commit 和還沒追蹤的檔案。
用 `git -C {shlex.quote(CFG['repo'])} diff --stat {base} {head}` 和 `git -C {shlex.quote(CFG['repo'])} diff {base} {head}` 查看這次的變更。
變更的檔案：
```
{names}
```

### 審查輪次
第 {rnd} 輪（同一串最多 {CFG['max_rounds']} 輪）。{prev}

### 已交使用者裁決的爭議（不要再提）
{disputes or '（無）'}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def launch(base, head, rnd, prompt, prev_run):
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + f"_AUTO_r{rnd}"
    brief = os.path.join(JOBS, run_id + ".brief.md")
    write_brief(brief, base, head, rnd, prompt, prev_run)
    jsave(os.path.join(JOBS, run_id + ".json"),
          {"run_id": run_id, "sid": SID, "base": base, "head": head, "round": rnd, "status": "running",
           "started": now(), "delivered": False, "prompt": prompt[:500]})
    log, exitf = os.path.join(JOBS, run_id + ".log"), os.path.join(JOBS, run_id + ".exit")
    cmd = (f"COLLAB_SID={shlex.quote(SID)} RUN_ID={run_id} CODEX_EFFORT=medium CODEX_TIMEOUT={AUTO_TIMEOUT} "
           f"bash {shlex.quote(BACKEND)}/run_codex.sh new AUTO auto {brief} {base} {head} > {log} 2>&1; echo $? > {exitf}")
    subprocess.Popen(["bash", "-c", cmd], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return run_id


def set_pending(base):
    p = os.path.join(sdir(), "pending.json")
    if not jload(p):
        jsave(p, {"base": base, "since": now()})


def take_pending():
    p = os.path.join(sdir(), "pending.json")
    d = jload(p)
    if d:
        os.remove(p)
        return d["base"]
    return None


def turn_start(inp):
    jsave(os.path.join(sdir(), "turn.json"),
          {"t0": snapshot(), "origin": inp.get("origin", "user"), "prompt": inp.get("prompt", ""), "at": now()})
    return {"ok": True}


def turn_end():
    tp = os.path.join(sdir(), "turn.json")
    turn = jload(tp)
    if not turn:
        return {"action": "noturn"}
    os.remove(tp)
    t0, t1 = turn["t0"], snapshot()
    if not t0 or not t1 or t0 == t1:
        return {"action": "nochange"}
    if is_off() or not CFG["auto_review"]:
        return {"action": "off"}
    ch = chain()
    if turn["origin"] == "user":        # 使用者的新要求 → 新的一串
        ch = {"round": 1, "prompt": turn["prompt"], "prev_run": None}
    elif turn["origin"] == "plugin":    # 交付審查結果後，Claude 依結果修改的回合 → 下一輪
        ch["round"] = ch.get("round", 0) + 1
    else:                               # 背景工作完成通知等延續性的回合 → 同一輪
        ch["round"] = max(ch.get("round", 0), 1)
        ch.setdefault("prompt", turn["prompt"])
    jsave(os.path.join(sdir(), "chain.json"), ch)
    mr = int(CFG["max_rounds"])
    if ch["round"] > mr:
        set_pending(t0)
        return {"action": "round-limit", "message": f"已達 {mr} 輪上限，這回合的修改沒有再送審（併入下一次審查）"}
    m = mode()
    if m != "collab":
        set_pending(t0)
        return {"action": "solo", "message": m.split(":", 1)[1] + "；這次的變更已記下，恢復後補審"}
    if running_job():
        set_pending(t0)
        return {"action": "busy", "message": "上一個 Codex 審查還在跑，這次的變更等它結束後補審"}
    base = take_pending() or t0
    run_id = launch(base, t1, ch["round"], ch.get("prompt", ""), ch.get("prev_run"))
    return {"action": "launched", "run_id": run_id, "message": f"Codex 背景審查第 {ch['round']} 輪已啟動"}


def tick():
    out = {"finished": [], "launched": None}
    for j in jobs(SID):
        j = refresh(j)  # 別的呼叫（status 等）也可能先把狀態改成完成，所以用 notified 旗標判斷
        if j["status"] != "running" and not j.get("notified"):
            j["notified"] = True
            jsave(os.path.join(JOBS, j["run_id"] + ".json"), j)
            out["finished"].append({"run_id": j["run_id"], "status": j["status"], "round": j["round"],
                                    "error": j.get("error", "")})
    pend = jload(os.path.join(sdir(), "pending.json"))
    if (pend and CFG["auto_review"] and not os.path.exists(os.path.join(sdir(), "turn.json"))
            and not running_job() and mode() == "collab"):
        head = snapshot()
        base = take_pending()
        if base and head and base != head:
            ch = chain()
            rnd = min(max(ch.get("round", 1), 1), int(CFG["max_rounds"]))
            out["launched"] = launch(base, head, rnd, ch.get("prompt", ""), ch.get("prev_run"))
    out["status"] = status()
    return out


def deliver(run_id):
    global SID, CFG
    jp = os.path.join(JOBS, run_id + ".json")
    j = jload(jp)
    if not j or j.get("delivered"):
        return ""
    j["delivered"] = True
    jsave(jp, j)
    if j["status"] != "done":
        return ""
    SID, CFG = j["sid"], load_cfg(j["sid"])
    res = jload(os.path.join(out_root(), run_id, "result.json"), {})
    sev = {}
    for f in res.get("findings", []):
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
    ch = chain()
    ch["prev_run"] = run_id
    jsave(os.path.join(sdir(), "chain.json"), ch)
    mr = int(CFG["max_rounds"])
    serious = sev.get("critical", 0) + sev.get("major", 0)
    counts = "、".join(f"{k} {v}" for k, v in sev.items()) or "沒有任何 finding"
    head = (f"[codex-collab] Codex 自動審查完成：`{run_id}`（第 {j['round']}／{mr} 輪），{counts}。"
            f"結果：`{out_root()}/{run_id}/result.json`。")
    if serious == 0:
        return head + "\n沒有 critical／major：讀一下 summary，確認沒有誤解，用一句話告訴使用者即可；不必寫審查紀錄檔，也不要改任何檔案。"
    last = j["round"] >= mr
    step3 = ("**這是最後一輪**：還沒解決的 critical／major 不要再修，做成爭議卡（問題／Codex 的證據／Claude 的反證／怎麼判斷誰對／建議），"
             f"交給使用者裁決，並把每張卡的一行標題追加到 `{STATE}/disputes.md`。") if last else \
            "修完後，下一輪審查會自動啟動；駁回的要在紀錄裡寫清楚理由，Codex 下一輪會看到。"
    return head + f"""
請照 cross-check skill 的「逐條驗證」與「寫審查紀錄」步驟處理：
1. 每一條 critical／major 都回到 code 驗證，判定為確認、駁回或爭議；minor／question 只記錄。
2. 確認的問題中，屬於這次任務裡 Claude 自己寫的 code 的明確 bug，可以直接修（不 commit，除非使用者先前已同意）；
   涉及設計、別人的程式、資料切分、超參數，或會改變結論的，不要動手，列給使用者決定。
3. {step3}
4. 紀錄寫到 `{os.path.join(CFG['repo'], CFG['review_dir'], run_id + '.md')}`，簡短回報使用者最重要的兩三項。"""


def context():
    lines = []
    for j in jobs(SID):
        refresh(j)
        if j["status"] in ("failed", "quota") and not j.get("delivered"):
            j["delivered"] = True
            jsave(os.path.join(JOBS, j["run_id"] + ".json"), j)
            lines.append(f"[codex-collab] 背景審查 {j['run_id']} 沒有完成（{j['status']}：{j.get('error', '')}）。"
                         "那次的變更改由 Claude 單獨負責，回覆時順帶告訴使用者一句。")
    m = mode()
    run = f"COLLAB_SID={SID} bash {BACKEND}/run_codex.sh"
    if m.startswith("off:"):
        lines.append("[codex-collab] 這個對話已關閉 Codex 協作（使用者用 /collab off 關閉）：不要呼叫 Codex，包括 CONSULT 與 G1–G4；"
                     "改檔後也不會自動送審。")
    elif m == "collab":
        pct = read_quota().get("weekly_pct")
        lines.append(
            f"[codex-collab] 協作模式（Codex 週額度 {pct if pct is not None else '?'}%）。"
            "若本回合是研究或設計討論（下一步、設計取捨、結果解讀、方案選擇），或使用者說「問 Codex」，"
            "先照 cross-check skill 的 CONSULT 流程取得 Codex 的獨立意見（同步等待，限時約 5 分鐘），"
            "再用「共識／分歧／Codex 補充／綜合建議」的格式回覆；使用者說「不用問 Codex」就跳過。"
            + ("改檔後的審查會在回合結束時自動在背景進行，不必手動呼叫。" if CFG["auto_review"] else "")
            + f"\n呼叫方式：`{run} new <G1|G2|G3|G4|CONSULT> <slug> <brief 檔>`（審查紀錄在 `{os.path.join(CFG['repo'], CFG['review_dir'])}`）。")
    else:
        lines.append(f"[codex-collab] Claude 單獨模式（{m.split(':', 1)[1]}）：不要呼叫 Codex；恢復後會自動補審這段期間的變更。")
    return "\n".join(lines)


# ---------- G2 守門 ----------
def check_g2(command):
    if not CFG["g2_guard"] or not TRAIN_RE.search(command):
        return {"allow": True}
    branch = git("branch", "--show-current").stdout.strip()
    if not branch.startswith(CFG["g2_branch_prefix"]):
        return {"allow": True}
    head = git("rev-parse", "HEAD").stdout.strip()
    if os.path.exists(os.path.join(STATE, "g2_skip", head)):
        return {"allow": True, "warn": "使用者已同意這個 commit 略過 G2"}
    for meta in glob.glob(os.path.join(out_root(), "*_G2_*", "meta.json")):
        h = (jload(meta, {}) or {}).get("head", "")
        if h and head.startswith(h):
            return {"allow": True}
    m = mode(100)
    if m != "collab":
        return {"allow": True, "warn": f"{m.split(':', 1)[1]}：這次訓練沒有經過 G2 審查，請在實驗紀錄註明"}
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    return {"allow": False, "reason":
            f"[codex-collab] 這個訓練指令在 {branch} 的 HEAD {head[:8]} 上執行，但這個 commit 還沒有 G2 程式審查紀錄。"
            + ("工作區還有未 commit 的變更，建議先 commit。" if dirty else "")
            + "請先用 cross-check skill 做 G2；使用者明確同意略過時，請使用者輸入 /collab skip-g2。"}


def skip_g2():
    head = git("rev-parse", "HEAD").stdout.strip()
    os.makedirs(os.path.join(STATE, "g2_skip"), exist_ok=True)
    with open(os.path.join(STATE, "g2_skip", head), "w") as f:
        f.write(datetime.datetime.now().isoformat())
    return head


def active_runs(sid=None):
    """run_codex.sh 執行中留下的標記（CONSULT／G1–G4／追問）；程序已不在的標記順手清掉。"""
    out = []
    for p in glob.glob(os.path.join(STATE, "active", "*.json")):
        a = jload(p)
        pid = (a or {}).get("pid")
        if not a or not pid or not pid_alive(pid) or now() - a.get("started", 0) > 6 * 3600:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        if sid is None or a.get("sid") == sid:
            out.append(a)
    return out


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status():
    """state：working（Codex 正在跑）｜waiting（審查完成還沒交給 Claude，或有變更排隊等補審）｜
    idle（閒置）｜off（這個對話關閉）｜solo（全域暫停或額度不足）；gate：AUTO／CONSULT／G1–G4"""
    m = mode()
    kind = m.split(":", 1)[0]
    run = running_job()
    if run and SID and run.get("sid") != SID:
        run = None
    act = active_runs(SID)
    gate = "AUTO" if run else (act[0].get("gate") if act else None)
    run_id = run["run_id"] if run else (act[0].get("run_id") if act else None)
    started = run.get("started") if run else (act[0].get("started") if act else None)  # 秒，給狀態列算經過時間
    if kind != "collab":
        state = kind
    elif run_id:
        state = "working"
    else:
        undelivered = any(j["status"] != "running" and not j.get("notified") for j in jobs(SID)) if SID else False
        queued = bool(SID) and os.path.exists(os.path.join(sdir(), "pending.json"))
        state = "waiting" if undelivered or queued else "idle"
    return {"mode": kind, "detail": m, "state": state, "gate": gate, "started": started,
            "weekly_pct": read_quota().get("weekly_pct"), "running": run_id}


def config_sh():
    c = CFG
    pairs = {"REPO": c["repo"], "OUT_ROOT": out_root(), "REVIEWER_MODE": c["reviewer_mode"],
             "REV_USER": c["reviewer_user"], "REV_HOME": reviewer_home(), "STATE": STATE}
    return "\n".join(f"{k}={shlex.quote(str(v))}" for k, v in pairs.items())


def main():
    global SID, CFG
    args = sys.argv[1:] or ["status"]
    SID = os.environ.get("COLLAB_SID")
    if "--sid" in args:
        i = args.index("--sid")
        SID = args[i + 1]
        del args[i:i + 2]
    CFG = load_cfg(SID)
    c = args[0]
    needs_repo = {"snapshot", "turn-start", "turn-end", "tick", "check-g2", "skip-g2", "config-sh"}
    if c in needs_repo and not CFG["repo"]:
        print(json.dumps({"error": "這個 session 還沒有設定 repo（mod 會在 session 開始時寫入）"}, ensure_ascii=False))
        sys.exit(3)
    if c == "configure":
        inp = json.load(sys.stdin)
        jsave(os.path.join(sdir(), "config.json"), {k: inp[k] for k in DEFAULTS if k in inp})
        print("ok")
    elif c == "config-sh":
        print(config_sh())
    elif c == "snapshot":
        print(snapshot())
    elif c == "quota":
        print(json.dumps(read_quota(), ensure_ascii=False))
    elif c == "mode":
        print(mode(float(args[1]) if len(args) > 1 else None))
    elif c == "block":
        block(args[1] if len(args) > 1 else "")
    elif c in ("off", "on"):
        f = os.path.join(sdir(), "off")
        if c == "off":
            open(f, "w").write(datetime.datetime.now().isoformat())
            p = os.path.join(sdir(), "pending.json")
            if os.path.exists(p):
                os.remove(p)
        elif os.path.exists(f):
            os.remove(f)
        print(c)
    elif c == "pause":
        open(os.path.join(STATE, "paused"), "w").write(datetime.datetime.now().isoformat())
        print("paused")
    elif c == "resume":
        if os.path.exists(os.path.join(STATE, "paused")):
            os.remove(os.path.join(STATE, "paused"))
        print("resumed")
    elif c == "turn-start":
        print(json.dumps(turn_start(json.load(sys.stdin)), ensure_ascii=False))
    elif c == "turn-end":
        print(json.dumps(turn_end(), ensure_ascii=False))
    elif c == "tick":
        print(json.dumps(tick(), ensure_ascii=False))
    elif c == "deliver":
        print(deliver(args[1]))
    elif c == "context":
        print(context())
    elif c == "check-g2":
        print(json.dumps(check_g2(sys.stdin.read()), ensure_ascii=False))
    elif c == "skip-g2":
        print(skip_g2())
    elif c == "status":
        print(json.dumps(status(), ensure_ascii=False))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
