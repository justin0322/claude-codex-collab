#!/usr/bin/env bash
# reviewer_mode=user 的一次性設定（Codex 內建沙箱在容器裡無法啟動時用）。需要 root。
#
#   sudo bash setup_reviewer.sh <repo 絕對路徑> [帳號名稱，預設 codexrev]
#
# 會做的事：
#   1. 建立低權限帳號（沒有 sudo），只能讀、不能寫你的 repo（靠一般的 Linux 檔案權限）
#   2. 讓它的 git 信任所有目錄（safe.directory），才能讀你的 repo 歷史
#   3. 寫入它的 Codex 設定：沙箱關閉（容器裡本來就起不來），唯讀改由檔案權限保證
#   4. 驗證它真的寫不進 repo，最後印出登入指令（要另外登入，不要複製你的 auth.json：
#      ChatGPT 的 refresh token 會輪替，兩份會互相失效）
set -euo pipefail
REPO="${1:?用法：setup_reviewer.sh <repo 絕對路徑> [帳號]}"
USER_NAME="${2:-codexrev}"
[[ $EUID -eq 0 ]] || { echo "需要 root"; exit 1; }
[[ -d "$REPO/.git" ]] || { echo "$REPO 不是 git repo 根目錄"; exit 1; }

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd -m -r -s /bin/bash -c "codex-collab read-only reviewer" "$USER_NAME"
  echo "已建立帳號 $USER_NAME"
fi
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
runuser -u "$USER_NAME" -- env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin git config --global --add safe.directory '*'

install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 "$HOME_DIR/.codex"
cat > "$HOME_DIR/.codex/config.toml" <<EOF
# codex-collab 唯讀審查帳號：容器內 Codex 沙箱無法啟動，唯讀由 Linux 檔案權限保證
sandbox_mode = "danger-full-access"
approval_policy = "never"

[projects."$REPO"]
trust_level = "trusted"
EOF
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.codex/config.toml"

echo "--- 唯讀檢查"
fail=0
for p in "$REPO" "$REPO/.git"; do
  if runuser -u "$USER_NAME" -- touch "$p/.codex_collab_probe" 2>/dev/null; then
    rm -f "$p/.codex_collab_probe"; echo "✘ $USER_NAME 可以寫入 $p（請修正權限，例如移除 o+w）"; fail=1
  else
    echo "✔ 不能寫入 $p"
  fi
done
if runuser -u "$USER_NAME" -- test -r "$REPO/.git/HEAD"; then echo "✔ 可以讀取 repo"; else echo "✘ 讀不到 repo（需要 o+r／o+x）"; fail=1; fi
w=$(find "$REPO" -path "$REPO/.git" -prune -o -perm -o+w -not -type l -print 2>/dev/null | head -5)
[[ -z "$w" ]] || { echo "⚠ 以下路徑任何人都能寫，審查帳號也能改："; echo "$w"; }

echo
echo "--- 下一步：用審查帳號登入 Codex（另外登入，不要複製 auth.json）"
echo "runuser -u $USER_NAME -- env -i HOME=$HOME_DIR PATH=/usr/bin:/bin codex login --device-auth"
exit $fail
