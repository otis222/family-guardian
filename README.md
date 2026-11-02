# 顧家貓：家族群組防詐守護者

加進家族 LINE 群組的防詐機器人，採用 Super-agent 架構：快速過濾 → Router → Worker → Watchdog。
規格見 `SPEC.md`，開發守則見 `CLAUDE.md`。

## 進度
- [x] Phase 1 核心骨架：資料模型、`call_llm`（offline／replay／live）、設定檔、測試框架
- [x] Phase 2 快速過濾：規則引擎、165 名單下載與快取、網址抽取、個資遮罩
- [x] Phase 3 Router、Worker、Thread
- [x] Phase 4 圖卡與 Watchdog
- [ ] Phase 5 Gradio 模擬器與實驗區（程式完成，等金鑰錄製回放）
- [ ] Phase 6 LINE 伺服器（程式完成，等 LINE channel 實測）
- [ ] Phase 7 家庭試行
- [ ] Phase 8 收尾

## 快速開始
```bash
python -m venv .venv
source .venv/bin/activate          # Windows：.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # 需要 live 模式時再填金鑰

pytest -q
python -m guardian.run scenarios/demo_family.yaml --mode offline --trace
python -m guardian.data_165 --update   # 下載 165 涉詐網站名單和闢謠專區（每週一次）
python -m guardian.workers.card card.png   # 輸出警示圖卡來看
python apps/gradio_app.py --mode offline   # 模擬器和實驗區（還沒錄製回放時先用 offline）
python -m guardian.experiment --mode offline   # 用命令列跑實驗表格
```

- `--trace` 會逐則印出決策紀錄（只有分數、命中的規則名稱和處理結果，不印訊息原文）；`--speaking gentle` 可以切換發言模式；`--daily-summary` 會印出給守護人的今日摘要。
- 顧家貓插畫和圖卡字型在 `assets/`，換插畫直接覆蓋 PNG 就好（見 `assets/README.md`）。
- offline 是假模型，實驗數字只驗證流程。真實結果要先錄製：`python -m guardian.experiment --record`（需要 `GROQ_API_KEY`），之後預設的 replay 模式不需要金鑰就能重現。
- 規則的權重和正反例在 `guardian/rules.yaml`，改完跑 `pytest -q` 就知道有沒有改壞。
- 規格沒寫清楚的地方怎麼決定的，記在 `SPEC.md` 第 16 節。

## LINE 伺服器（家庭試行）
訊息只在你自己的電腦上處理，電腦關機時收不到事件。第一次先用只有守護人的測試群組。

1. 到 [LINE Developers](https://developers.line.biz/) 建 Messaging API channel，在 Messaging API 分頁開啟「Allow bot to join group chats」，發一個 channel access token。
2. 到 LINE Official Account Manager，把自動回應和加入好友的歡迎訊息關掉，webhook 打開。
3. 安裝 cloudflared：`winget install Cloudflare.cloudflared`。
4. 把 `.env.example` 複製成 `.env`，填這些值。先設 `GUARDIAN_LLM_MODE=offline` 測通道，它是假模型，不會把訊息送到外面。
   - `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`
   - `GUARDIAN_HASH_SALT`：自己編一串只有你知道的隨機字元
   - `GUARDIAN_LINE_USER_IDS`：你的 LINE 使用者編號，在 channel 的 Basic settings 最下面（Your user ID）
5. 開兩個終端機：

   ```bash
   uvicorn apps.line_server:app --port 8000
   ```

   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```

   伺服器會自己找到通道的網址，並更新 LINE 的 webhook 網址。
6. 建一個只有你的測試群組，把官方帳號加進去。伺服器畫面會印出群組編號，填進 `.env` 的 `GUARDIAN_GROUP_IDS`，重新啟動伺服器。不在這個清單裡的群組，事件一律不處理。
7. 在群組傳一則有詐騙特徵的訊息（例如「訂單被誤設成分期付款，請到 ATM 操作解除分期」）。預設是 `silent`：群組裡不會有回應，你會收到私訊報告。

私訊官方帳號可以下這些指令：`今日摘要`、`暫停`、`恢復`、`模式 silent｜gentle｜on_mention`、`誤報 事件編號`。

加進真實家族群組之前：取得全員同意（SPEC 第 14 節有公告範本），改成 `GUARDIAN_LLM_MODE=live`，並到 Groq 的 Data Controls 開啟 Zero Data Retention（SPEC 第 8 節）。

## 接手開發（Claude Code）
在這個資料夾開 Claude Code（終端機執行 `claude`，或桌面版 Claude 的 Code 分頁選這個資料夾），跟它說「做下一個 Phase」。
它會讀 `CLAUDE.md`、`SPEC.md`、`HANDOFF.md`，做下一個 Phase，做完停下來回報。
