# claude-codex-collab

Claude Code × OpenAI Codex 全自動協作 mod：Claude 寫、Codex 盲審、你裁決。
A Claude Code mod that has OpenAI Codex review Claude's work blind and read-only, and gives a second opinion in discussions. You make the final call.

最初是為碩士論文的實驗研究而做，所以特別重視「看結果前先審設計」「從原始輸出獨立重算數字」「結論不能說得比證據滿」。一般軟體專案也適用。

## 它會做什麼

| 時機 | 動作 |
|---|---|
| 一個回合結束，程式或文件有變更 | 在背景送 Codex 審查（唯讀、盲審，只看變更與 repo，不看 Claude 的推理），審完自動交給 Claude 逐條驗證後回報你 |
| 研究或設計討論，或你說「問 Codex」 | Claude 先寫下自己的看法，再徵詢 Codex 的獨立意見，用「共識／分歧／Codex 補充／綜合建議」回覆 |
| 同一件事來回超過 `max_rounds` 輪（預設 2） | 不再送審，整理成爭議卡交給你裁決；之後的審查會避開這些爭議 |
| Codex 週額度達 `auto_limit`%（預設 90）或用完 | 切回 Claude 單獨，期間的變更記下來，額度恢復後自動補審 |
| （選用）在 `exp/` branch 上開始訓練 | HEAD 沒有 G2 程式審查紀錄就擋下 |

手動關卡（`cross-check` skill）：**G1** 設計審（看結果前）、**G2** 程式審（執行前）、**G3** 從原始輸出獨立重算數字、**G4** 結論審（寫進論文或簡報前）。

狀態列會顯示 `Codex：協作中`／`● 審查中`／`協作關閉`／`⚠ Claude 單獨`。

## 需求
- Claude Code（支援 mod／hooks module 的版本）
- [Codex CLI](https://github.com/openai/codex)（`npm install -g @openai/codex`），並已 `codex login`
- Python 3.8+、git、bash（Linux 或 macOS）
- 審查用的是你自己的 Codex 額度（ChatGPT 方案或 API key）
- G3 數值重算：Codex 執行時要能用到裝有 numpy（建議再加 pandas、scipy、scikit-learn）的 Python。user 模式的審查帳號通常用不了你自己的 venv，要另外替它建一個

## 安裝

在 Claude Code 的提示列輸入：

```
/plugin install codex-collab --marketplace justin0322/claude-codex-collab
```

或從 shell：

```
claude plugin install codex-collab --marketplace justin0322/claude-codex-collab
```

接著出現 `Add marketplace?` 時按 `y`，選擇安裝範圍（建議 user），再依畫面設定選項（都有預設值，可以直接 Enter）。

### 唯讀方式：sandbox 或 user

- **sandbox（預設）**：用 Codex 內建的唯讀沙箱（`codex exec -s read-only`）。一般 Linux／macOS 直接可用。
- **user**：在 Docker 這類**不允許建立 namespace 的容器**裡，Codex 的沙箱（bubblewrap／Landlock）起不來，read-only 模式下連 `ls` 都不能跑。這時改用另一個低權限 Linux 帳號執行 Codex，唯讀由檔案權限保證。需要以 root 執行 Claude Code：

  ```
  sudo bash <plugin 資料夾>/backend/setup_reviewer.sh /path/to/your/repo codexrev
  # 照最後印出的指令讓 codexrev 另外登入 Codex（不要複製你的 auth.json）
  ```

  然後在 `/config` 把 `reviewer_mode` 設成 `user`。

判斷方法：執行 `codex exec -s read-only "ls"`，如果看到 `bwrap: No permissions to create a new namespace`，就要用 user 模式。

## 設定（`/config`）

| 選項 | 預設 | 說明 |
|---|---|---|
| `repo_root` | （空） | 只在這個 repo 啟用；留空＝任何 git repo 都啟用 |
| `reviewer_mode` | `sandbox` | `sandbox` 或 `user`（見上） |
| `reviewer_user` | `codexrev` | user 模式用的帳號 |
| `review_dir` | `docs/cross-check` | 審查紀錄的位置（相對於 repo）；這裡的變更不會觸發審查 |
| `auto_review` | `true` | 改檔後自動審查 |
| `auto_limit` | `90` | 週額度到這個 % 就停止自動協作 |
| `max_rounds` | `2` | 同一串最多來回幾輪 |
| `g2_guard` | `false` | 訓練前 G2 守門 |
| `g2_branch_prefix` | `exp/` | G2 守門適用的 branch 前綴 |

## 指令

- `/collab`：目前狀態與 Codex 額度
- `/collab off`／`/collab on`：只關閉／開啟**目前這個對話**的協作
- `/collab pause`／`/collab resume`：暫停／恢復**所有對話**的自動協作
- `/collab skip-g2`：你同意這個 commit 不做 G2 就開跑（只有你能下這個指令）

## 讓 Codex 懂你的專案：AGENTS.md

Codex 會自動讀 repo 根目錄的 `AGENTS.md`。把專案的規則、容易出錯的地方、資料的授權限制寫在那裡，審查就會照著檢查。
Claude Code 也能讀同一份：在 `CLAUDE.md` 第一行寫 `@AGENTS.md`，兩邊就共用一份規則。

## 運作方式

- 每個回合開始時，mod 把 repo 裡的程式與文件檔（含未 commit、未追蹤，不含 gitignore）寫成一個暫存的 git tree；回合結束時再寫一次，兩個 tree 不同就送審，Codex 用 `git diff <tree1> <tree2>` 看變更。不會動到你真正的 index。
- 狀態存在 `~/.claude/codex-collab-state/`，每個對話各自一份。
- 審查紀錄（prompt、Codex 的 JSON 結果）存在 `<review_dir>/raw/<run_id>/`；Codex 完整的事件流（含它讀到的檔案內容）不放進 repo。
- 一次自動審查大約 1～3 分鐘、不到 0.5% 的 ChatGPT Plus 週額度；完整的 G1 設計審約 5～10 分鐘、1% 左右（因專案大小而異）。

## 注意

- Codex 讀到的內容會送到 OpenAI；有授權或隱私限制的資料，請在 `AGENTS.md` 寫明不要讀。
- 兩個模型都說沒問題，不代表正確：輸入資料本身錯了，兩邊會一起錯。重要數字請用 G3 從原始輸出重算。
- 自動審查只回報會影響結論、正確性、安全性或可重現性的問題，風格問題不會提。

## 授權

MIT
