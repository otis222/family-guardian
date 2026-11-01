# 顧家貓：家族群組防詐守護者

加進家族 LINE 群組的防詐機器人，採用 Super-agent 架構：快速過濾 → Router → Worker → Watchdog。
規格見 `SPEC.md`，開發守則見 `CLAUDE.md`。

## 進度
- [x] Phase 1 核心骨架：資料模型、`call_llm`（offline／replay／live）、設定檔、測試框架
- [x] Phase 2 快速過濾：規則引擎、165 名單下載與快取、網址抽取、個資遮罩
- [x] Phase 3 Router、Worker、Thread
- [ ] Phase 4 圖卡與 Watchdog
- [ ] Phase 5 Gradio 模擬器與實驗區
- [ ] Phase 6 LINE 伺服器
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
```

- `--trace` 會逐則印出決策紀錄（只有分數、命中的規則名稱和處理結果，不印訊息原文）；`--speaking gentle` 可以切換發言模式。
- 規則的權重和正反例在 `guardian/rules.yaml`，改完跑 `pytest -q` 就知道有沒有改壞。
- 規格沒寫清楚的地方怎麼決定的，記在 `SPEC.md` 第 16 節。

## 接手開發（Claude Code）
在這個資料夾開 Claude Code（終端機執行 `claude`，或桌面版 Claude 的 Code 分頁選這個資料夾），跟它說「做下一個 Phase」。
它會讀 `CLAUDE.md`、`SPEC.md`、`HANDOFF.md`，做下一個 Phase，做完停下來回報。
