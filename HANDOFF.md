# 交接：給接手的 Claude Code

先讀 `CLAUDE.md`（工作守則）和 `SPEC.md`（規格；第 16 節是決策紀錄），再看這份。
這份記錄目前做到哪裡、下一步做什麼、哪些事要等使用者。每做完一個 Phase 就更新這份。

## 使用者說「做下一個 Phase」時
1. 從 README.md 的進度清單找出下一個還沒完成的 Phase。
2. 先跑 `python -m pytest -q`，確認目前全部通過；沒通過就先修好，不要開始新的 Phase。
3. 照 SPEC 對應章節和這份的清單實作。規格不清楚時依 CLAUDE.md 的原則處理，決定寫進 SPEC 第 16 節。
4. 做完跑 `python -m pytest -q` 和 `python -m guardian.run scenarios/demo_family.yaml --mode offline --trace`。
5. 更新 README.md 的進度和這份的「目前進度」「下一步」，然後 git commit。
6. 停下來回報：改了哪些檔案、怎麼驗證、測試結果、做了哪些決定、需要使用者做什麼。不要直接開始下一個 Phase。

## 目前進度（2026-11-02）
- Phase 1 核心骨架、Phase 2 快速過濾、Phase 3 Router／Worker／Thread、Phase 4 圖卡與 Watchdog：完成。`python -m pytest -q` 412 項全過。
- 示範劇本 `scenarios/demo_family.yaml`（21 則）用 offline 跑：6 則風險事件（m03、m06、m08、m09、m14、m16）都判 `high` 並報告守護人；silent 模式只回覆被 @ 的 m09（附圖卡）；日常訊息 0 次模型呼叫。模型呼叫 18 次（router 6、verifier 6、reporter 6）。Watchdog 沒有動作。

| 檔案 | 內容 |
|---|---|
| `guardian/models.py` | Trigger、RiskSignal、Decision（含 `parse_decisions`）、VerifyResult、Thread、EventRecord、GroupReply、GuardianReport、TraceEntry |
| `guardian/llm.py` | `call_llm(role, system_prompt, user_prompt)`；offline／replay／live；`extract_json`、`safe_error`；`write_cache` 開關；`offline_responder` 依 prompt 的 `task` 回應 |
| `guardian/config.py` | 讀取並檢查 `config.yaml`；`GUARDIAN_LLM_MODE` 覆蓋模式 |
| `guardian/urls.py`、`guardian/redact.py` | 網址抽取（拿掉查詢參數）、`strip_urls`；遮罩電話、身分證、帳號、地址 |
| `guardian/rules.py`、`guardian/rules.yaml` | 六類特徵＋情境加權＋165 命中滿分；`FastFilter.observe()` 記住新成員入群；`RuleSet.flags_injection()` |
| `guardian/data_165.py` | 165 名單與闢謠專區的下載、快取；`DomainList.match()`、`RumorIndex.search()` |
| `guardian/prompts.py` | 四段 system prompt 和 user prompt 的固定組法（JSON、鍵名排序、訊息截到 500 字） |
| `guardian/threads.py` | `ThreadStore`：開啟、查找、過期、提前結束 |
| `guardian/router.py` | `Router.decide()` 批次分流、`compose_reply()` 起草→自檢→改寫→模板、`review_draft()`、`should_speak()` |
| `guardian/workers/verifier.py` | 查證 Worker：165 命中不呼叫模型；三個工具；注入防線 |
| `guardian/workers/reporter.py` | `build_report()`、`build_failure_report()`、`build_daily_summary()` |
| `guardian/workers/card.py`、`assets/` | 警示圖卡：固定文字加插畫，`warning_card_png()` 回傳 PNG bytes；`python -m guardian.workers.card 檔名.png` 可以輸出來看 |
| `guardian/watchdog.py` | 四條規則和卡住的候選；`claim_push()`、`snapshot()`、`usage_on()` |
| `guardian/engine.py` | 把以上元件接起來；模擬時間的批次處理；決策紀錄；`daily_summary()` |
| `guardian/scenario.py`、`guardian/run.py` | 劇本載入與檢查；命令列 `python -m guardian.run`（`--speaking` 切換發言模式，`--daily-summary` 印今日摘要） |

Phase 3 的決定見 SPEC 第 16 節 D10–D22，Phase 4 見 D23–D32。D10（Thread 直通只送有風險訊號的訊息）是使用者決定的；D24（Watchdog 通知不用 push）等使用者確認；其他取捨都以 token 最少為準，改之前先看理由。

## 下一步：Phase 5（Gradio 模擬器與實驗區）
規格：SPEC 第 9 節、11-1、第 12 節。驗收：Colab 在 `replay` 模式下不需金鑰就能跑完整個實驗。

接點：
- 右欄：`engine.replies`（`reply.card` 是 PNG bytes，可能是 None）、`engine.reports`、`engine.notices`、`engine.watchdog.snapshot()`、`engine.daily_summary()`。
- 中欄：`engine.trace`，Phase 4 多了 `card`、`watchdog` 兩個 stage。
- 實驗：資料的詐騙密度（200 則裡約 30 則）遠高於真實群組，gentle 模式會觸發 Watchdog 切回 silent，也可能超過每天的 token 預算。跑實驗前用 `dataclasses.replace(config, watchdog=...)` 放寬，並在報告裡註明。
- 預算和提醒上限的數字是先抓的（決策 D25、D26），有真實用量後再調。

注意：回放快取要到 Phase 5 才錄。現在沒加 `--mode offline` 跑劇本會一路 replay miss：不會崩潰，候選改用規則結果報告，錯誤數會顯示出來。

## 之後的 Phase（細節見 SPEC 第 12 節）
- Phase 5 Gradio 模擬器與實驗區：三欄介面、三種做法（A 純規則、B 單一 LLM、C Super-agent）、實驗表格。錄回放快取需要 `GROQ_API_KEY`。
- Phase 6 LINE 伺服器：FastAPI＋line-bot-sdk v3、簽章驗證、reply 和 push 的零成本規則、Cloudflare Tunnel。啟動時強制 `replay_cache_write=false`（決策 D2），沒有 `GUARDIAN_HASH_SALT` 就拒絕啟動（決策 D4）。
  - 引擎產生的報告已經過 `claim_push()`，`report.push` 是 True 才推播。其他 push（例如 reply 來不及時）也要先問它。用官方額度 API 校正 `watchdog.push_used`；每多一位守護人，一次推播就多算一則。
  - 圖卡要有 HTTPS 網址才能當 LINE 圖片訊息送出：由伺服器提供 `warning_card_png()` 的內容。
  - 管理指令：`今日摘要` 用 `engine.daily_summary(今天的日期)`，`模式 ...` 改 `engine.speaking_mode`；`誤報` 的統計還沒做（SPEC 第 5 節）。
  - Watchdog 通知可以搭 `high` 的推播順便送，不多花額度（決策 D24）。
- Phase 7 家庭試行（需要使用者本人）、Phase 8 收尾（notebook、報告、影片）。

## 要等使用者的事
- 在自己電腦跑一次 `python -m guardian.data_165 --update`。現在也會下載闢謠專區（資料集 38262）；沒有這份資料時查證不提供 `search_165_rumors`，每則只呼叫模型 1 次。
- 「遭停止解析涉詐網站」的資料集編號 176455 已查過 API 確認標題相符，不用再確認。
- 確認決策 D24（Watchdog 通知不用 push）和 D31（截圖文字辨識先不做）。
- Phase 5 錄回放要 Groq 金鑰（放 `.env`）；Phase 6 要 LINE channel；Phase 7 要家人同意。
- 這些都不要自己繞過：沒有金鑰就用 offline 模式寫完、測完，回報時列出還沒錄製的部分。

## 在這台電腦（Windows）上
- `python -m venv .venv`，PowerShell 用 `.venv\Scripts\Activate.ps1`（cmd 用 `.venv\Scripts\activate`），再 `pip install -r requirements.txt`。
- 測試用 `python -m pytest -q`。程式要相容 Colab（Python 3.12）。
- 第一次接手先 `git init`，commit 目前狀態；之後每個 Phase 做完 commit 一次。
