#!/usr/bin/env bash
# codex-collab 執行器：以唯讀方式跑 Codex
#
#   COLLAB_SID=<sid> run_codex.sh new <G1|G2|G3|G4|AUTO|CONSULT> <slug> <brief.md> [base_ref] [head_ref]
#   COLLAB_SID=<sid> run_codex.sh followup <run_id> <question.md>
#
# 唯讀方式（設定 reviewer_mode）：
#   sandbox：同一個使用者，用 Codex 內建的唯讀沙箱（codex exec -s read-only）
#   user   ：另一個低權限 Linux 帳號執行，唯讀由檔案權限保證（沙箱無法啟動的容器用；需要 root）
# 環境變數：CODEX_TIMEOUT（秒）、CODEX_EFFORT（low/medium/high）、RUN_ID
# AUTO／CONSULT 在 Codex 週額度 >= auto_limit 時不執行（exit 7）；G1–G4 用到 100% 為止
#
# 成功：結果寫在 <repo>/<review_dir>/raw/<run_id>/，最後一行印出 RUN_ID=<run_id>
# 失敗：exit != 0 並印出原因（7 = 額度不足）。呼叫端不可在失敗時自行編造審查結果。
set -euo pipefail

BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLAB="python3 $BACKEND/collab.py"
TIMEOUT_SEC="${CODEX_TIMEOUT:-2400}"

die() { echo "[codex-collab] 失敗：$1" >&2; exit "${2:-1}"; }

[[ -n "${COLLAB_SID:-}" ]] || die "需要 COLLAB_SID（mod 附給 Claude 的提醒裡有完整指令）"
cfg="$($COLLAB config-sh)" || die "讀不到這個 session 的設定：$cfg"
eval "$cfg"   # REPO OUT_ROOT REVIEWER_MODE REV_USER REV_HOME STATE

# 執行期間留下標記，讓狀態列知道 Codex 正在跑（CONSULT／G1–G4／追問；AUTO 另有 jobs 紀錄）。
# 程序結束時 trap 自動移除；意外中斷留下的標記由 collab.py 依 pid 清掉。
mark_active() {  # $1=run_id  $2=關卡
  mkdir -p "$STATE/active"
  ACTIVE_FILE="$STATE/active/$1.$$.json"
  printf '{"run_id": "%s", "gate": "%s", "sid": "%s", "pid": %d, "started": %d}\n' \
    "$1" "$2" "$COLLAB_SID" "$$" "$(date +%s)" > "$ACTIVE_FILE"
  trap 'rm -f "$ACTIVE_FILE"' EXIT
}

as_rev() {
  if [[ "$REVIEWER_MODE" == user ]]; then
    runuser -u "$REV_USER" -- env -i HOME="$REV_HOME" CODEX_HOME="$REV_HOME/.codex" \
      PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 "$@"
  else
    "$@"
  fi
}

preflight() {
  command -v codex >/dev/null || die "找不到 codex 指令（npm install -g @openai/codex）" 3
  if [[ "$REVIEWER_MODE" == user ]]; then
    [[ $EUID -eq 0 ]] || die "reviewer_mode=user 需要以 root 執行 Claude Code" 3
    id "$REV_USER" >/dev/null 2>&1 || die "找不到帳號 $REV_USER，請先執行 backend/setup_reviewer.sh" 3
    as_rev codex login status >/dev/null 2>&1 || die "$REV_USER 尚未登入 Codex：runuser -u $REV_USER -- env -i HOME=$REV_HOME PATH=/usr/bin:/bin codex login --device-auth" 3
    if as_rev touch "$REPO/.codex_collab_write_probe" 2>/dev/null; then
      rm -f "$REPO/.codex_collab_write_probe"
      die "$REV_USER 可以寫入 $REPO，唯讀保證失效，停止執行" 4
    fi
  else
    codex login status >/dev/null 2>&1 || die "Codex 尚未登入：codex login" 3
  fi
}

extract_thread() {
  python3 - "$1" <<'EOF_PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        e = json.loads(line)
    except Exception:
        continue
    stack = [e]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k in ("thread_id", "session_id", "conversation_id"):
                if isinstance(x.get(k), str):
                    print(x[k]); sys.exit(0)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
EOF_PY
}

validate_result() {  # $1=result.json  $2=schema.json
  python3 - "$1" "$2" <<'EOF_PY' || return 1
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
sc = json.load(open(sys.argv[2], encoding="utf-8"))
for k in sc["required"]:
    assert k in d, f"缺少欄位 {k}"
print(" ".join(f"{k}={len(v)}" for k, v in d.items() if isinstance(v, list)))
EOF_PY
}

mark_quota_if_limited() {
  if grep -qiE 'usage limit|rate limit|quota|429|too many requests' "$1" 2>/dev/null; then
    $COLLAB block "Codex 回報額度上限" || true
    return 0
  fi
  return 1
}

sandbox_flags() {  # user 模式唯讀靠檔案權限（沙箱在容器裡起不來）；sandbox 模式用 Codex 內建唯讀沙箱
  if [[ "$REVIEWER_MODE" == user ]]; then echo "-c sandbox_mode=\"danger-full-access\" -c approval_policy=\"never\""
  else echo "-c sandbox_mode=\"read-only\" -c approval_policy=\"never\""; fi
}

run_codex() {  # $1=work dir  $2=prompt file  $3=result name  [$4=thread id 表示 resume]
  local work="$1" prompt="$2" name="$3" thread="${4:-}" rc=0
  local flags; flags="$(sandbox_flags)"
  if [[ -z "$thread" ]]; then
    # shellcheck disable=SC2086
    as_rev bash -c "timeout -k 30 \"\$1\" codex exec -C \"\$2\" --json $flags -c model_reasoning_effort=\\\"\$3\\\" --output-schema \"\$4\" -o \"\$5\" -" _ \
      "$TIMEOUT_SEC" "$REPO" "$EFFORT" "$work/schema.json" "$work/$name.json" \
      < "$prompt" > "$work/$name.events.jsonl" 2> "$work/$name.stderr.log" || rc=$?
  else
    as_rev bash -c "cd \"\$2\" && timeout -k 30 \"\$1\" codex exec resume \"\$6\" --json $flags -c model_reasoning_effort=\\\"\$3\\\" --output-schema \"\$4\" -o \"\$5\" -" _ \
      "$TIMEOUT_SEC" "$REPO" "$EFFORT" "$work/schema.json" "$work/$name.json" "$thread" \
      < "$prompt" > "$work/$name.events.jsonl" 2> "$work/$name.stderr.log" || rc=$?
  fi
  if [[ $rc -eq 124 || $rc -eq 137 ]]; then die "Codex 超過 ${TIMEOUT_SEC}s 逾時（stderr：$work/$name.stderr.log）" 5; fi
  if [[ $rc -ne 0 ]]; then
    if mark_quota_if_limited "$work/$name.stderr.log" || mark_quota_if_limited "$work/$name.events.jsonl"; then
      die "Codex 額度已達上限，切換為 Claude 單獨模式（重置後自動恢復）" 7
    fi
    die "Codex 結束碼 $rc。stderr 最後幾行：$(tail -5 "$work/$name.stderr.log" | tr '\n' ' ')" 5
  fi
  [[ -s "$work/$name.json" ]] || die "Codex 沒有產生結果檔" 5
  validate_result "$work/$name.json" "$work/schema.json" || die "結果不符合 schema：$work/$name.json" 5
}

publish() {  # 可進 git 的部分複製到 <review_dir>/raw/<run_id>/（事件流含讀到的檔案內容，不複製）
  local work="$1" run_id="$2"; shift 2
  local out="$OUT_ROOT/$run_id"
  mkdir -p "$out"
  for f in "$@"; do cp "$work/$f" "$out/"; done
  cp "$work/meta.json" "$out/"
  echo "[codex-collab] 已輸出到 $out"
  echo "RUN_ID=$run_id"
}

workdir() {  # user 模式放在審查帳號的家目錄（它要能寫結果）；sandbox 模式放在 state 目錄
  if [[ "$REVIEWER_MODE" == user ]]; then echo "$REV_HOME/codex-collab-runs/$1"; else echo "$STATE/runs/$1"; fi
}

cmd="${1:-}"; shift || true
case "$cmd" in
  new)
    gate="${1:?缺少關卡}"; slug="${2:?缺少 slug}"; brief="${3:?缺少 brief 檔}"; base="${4:-main}"; headref="${5:-}"
    [[ "$gate" =~ ^(G[1-4]|AUTO|CONSULT)$ ]] || die "關卡必須是 G1~G4、AUTO 或 CONSULT"
    [[ -s "$brief" ]] || die "brief 檔不存在或是空的：$brief"
    [[ "$slug" =~ ^[A-Za-z0-9._-]+$ ]] || die "slug 只能用英數字與 . _ -"
    case "$gate" in
      AUTO|CONSULT) EFFORT="${CODEX_EFFORT:-medium}"; qmode="$($COLLAB mode 2>/dev/null || echo collab)" ;;
      *) EFFORT="${CODEX_EFFORT:-high}"; qmode="$($COLLAB mode 100 2>/dev/null || echo collab)" ;;
    esac
    [[ "$qmode" == collab ]] || die "目前不呼叫 Codex（$qmode）" 7
    preflight
    branch="$(git -C "$REPO" branch --show-current)"
    head="${headref:-$(git -C "$REPO" rev-parse --short HEAD)}"
    run_id="${RUN_ID:-$(date +%Y%m%d_%H%M%S)_${gate}_${slug}}"
    work="$(workdir "$run_id")"; tmpdir="/tmp/codex_collab_$run_id"
    if [[ "$REVIEWER_MODE" == user ]]; then
      install -d -o "$REV_USER" -g "$REV_USER" -m 700 "$(dirname "$work")" "$work" "$tmpdir"
      tmpnote="需要寫暫存腳本或中間檔時，一律放在 \`$tmpdir\`。"
    else
      mkdir -p "$work"
      tmpnote="你在唯讀沙箱裡，不能寫任何檔案；需要計算時，用 \`python3 -c\` 或 heredoc 直接執行。"
    fi
    sed -e "s|{{GATE}}|$gate|g" -e "s|{{BRANCH}}|$branch|g" -e "s|{{HEAD}}|$head|g" \
        -e "s|{{BASE}}|$base|g" -e "s|{{REPO}}|$REPO|g" -e "s|{{TMPNOTE}}|$tmpnote|g" \
        "$BACKEND/templates/common.md" "$BACKEND/templates/$gate.md" > "$work/prompt.md"
    { echo; echo "## 審查說明（brief）"; echo; cat "$brief"; } >> "$work/prompt.md"
    if [[ "$gate" == CONSULT ]]; then cp "$BACKEND/schema_consult.json" "$work/schema.json"; else cp "$BACKEND/schema.json" "$work/schema.json"; fi
    [[ "$REVIEWER_MODE" == user ]] && chown -R "$REV_USER:$REV_USER" "$work"
    mark_active "$run_id" "$gate"
    echo "[codex-collab] $run_id：branch=$branch HEAD=$head base=$base effort=$EFFORT mode=$REVIEWER_MODE，Codex 執行中（逾時 ${TIMEOUT_SEC}s）…"
    run_codex "$work" "$work/prompt.md" result
    thread="$(extract_thread "$work/result.events.jsonl" || true)"
    python3 - "$work/meta.json" "$run_id" "$gate" "$branch" "$head" "$base" "$thread" "$(as_rev codex --version)" "$EFFORT" <<'EOF_PY'
import json, sys, datetime
p, run_id, gate, branch, head, base, thread, ver, effort = sys.argv[1:]
json.dump({"run_id": run_id, "gate": gate, "branch": branch, "head": head, "base": base,
           "thread_id": thread or None, "codex_version": ver, "effort": effort,
           "created": datetime.datetime.now().isoformat(timespec="seconds"), "followups": 0},
          open(p, "w"), ensure_ascii=False, indent=2)
EOF_PY
    publish "$work" "$run_id" prompt.md result.json
    ;;
  followup)
    run_id="${1:?缺少 run_id}"; q="${2:?缺少問題檔}"
    work="$(workdir "$run_id")"
    [[ -f "$work/meta.json" ]] || die "找不到 $work/meta.json"
    n="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['followups'])" "$work/meta.json")"
    [[ "$n" -lt 1 ]] || die "這次審查已追問過一輪，依規則不再追問；剩下的爭議交給使用者裁決" 6
    thread="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['thread_id'] or '')" "$work/meta.json")"
    [[ -n "$thread" ]] || die "沒有記錄到 thread id，無法追問"
    EFFORT="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('effort','high'))" "$work/meta.json")"
    qmode="$($COLLAB mode 100 2>/dev/null || echo collab)"
    [[ "$qmode" == collab ]] || die "目前不呼叫 Codex（$qmode）" 7
    preflight
    { echo "## 追問（僅此一輪）"; echo; cat "$q"; echo; echo "請依同一個 JSON schema 回答：只列出與追問相關的內容，並說明你維持或修正原判斷的依據。"; } > "$work/followup.md"
    [[ "$REVIEWER_MODE" == user ]] && chown "$REV_USER:$REV_USER" "$work/followup.md"
    mark_active "$run_id" "$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['gate'])" "$work/meta.json")"
    run_codex "$work" "$work/followup.md" followup "$thread"
    python3 -c "import json,sys;p=sys.argv[1];d=json.load(open(p));d['followups']=1;json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)" "$work/meta.json"
    publish "$work" "$run_id" followup.md followup.json
    ;;
  *)
    sed -n '2,15p' "$0"; exit 2 ;;
esac
