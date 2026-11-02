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
- Phase 1 核心骨架、Phase 2 快速過濾、Phase 3 Router／Worker／Thread、Phase 4 圖卡與 Watchdog：完成。
- Phase 5 Gradio 模擬器與實驗區：**回放錄好了**（735 筆，全新 clone 不帶金鑰跑得完），Colab 上實際跑要等 Phase 8 的 notebook。
- Phase 6 LINE 伺服器：**程式完成，驗收還差 LINE channel**（見「下一步」）。`python -m pytest -q` 684 項全過。
- 示範劇本 `scenarios/demo_family.yaml`（21 則）用 offline 跑：6 則風險事件（m03、m06、m08、m09、m14、m16）都判 `high` 並報告守護人；silent 模式只回覆被 @ 的 m09（附圖卡）；日常訊息 0 次模型呼叫。模型呼叫 24 次（router 6、verifier 12、reporter 6：有闢謠資料，offline 假模型每次查證都會多查一輪）。Watchdog 沒有動作。真實模型的回放（`python -m guardian.run scenarios/demo_family.yaml`）是 17 次，m16 判 `medium`，其他 `high`。

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
| `guardian/approaches.py` | 三種做法和消融實驗：`RulesOnlyEngine`（A）、`SingleLLMEngine`（B）、`build_engine()`；模擬和實驗用固定的 165 資料 |
| `guardian/experiment.py` | 指標（`evaluate()`）、`run_experiment()`、`compare_models()`、表格、錄製（`record_all()`）；命令列 `python -m guardian.experiment` |
| `guardian/simulation.py` | `Simulation`：一則一則跑劇本，各欄的文字 |
| `apps/gradio_app.py` | 三欄模擬器和實驗區；邏輯都在 `guardian/` |
| `scenarios/experiment.yaml` | 實驗劇本：200 則，20 則詐騙（5 種手法）、10 則注入，`compare: true` 標出模型比較的 30 則 |
| `guardian/line_bot.py` | `LineBot`：webhook 事件轉給引擎、reply／按鈕／push 的決定、管理指令。不依賴 line-bot-sdk |
| `guardian/records.py` | `RecordStore`：`records/` 裡的最小紀錄、誤報、狀態，保留期限 |
| `guardian/tunnel.py`、`guardian/fileio.py` | 找 Quick Tunnel 的網址；寫檔（Windows 上換名失敗會多試幾次） |
| `apps/line_server.py` | FastAPI、簽章驗證、line-bot-sdk 的轉接、背景執行緒、啟動檢查 |

Phase 3 的決定見 SPEC 第 16 節 D10–D22，Phase 4 見 D23–D32，Phase 5 見 D33–D40 和 D51–D54，Phase 6 見 D41–D50。D10（Thread 直通只送有風險訊號的訊息）是使用者決定的；D24（Watchdog 通知不用 push）、D42（只處理允許的群組）等使用者確認；其他取捨都以 token 最少為準，改之前先看理由。

## 下一步：完成 Phase 6 的驗收，再做 Phase 7

### Phase 6：在只有守護人的測試群組裡，收到 silent 模式的報告
程式用假的傳送層和本機的 LINE API 替身驗證過（簽章、reply、push、按鈕、webhook 更新、紀錄），也真的用 uvicorn 跑過一次。**還沒連過真的 LINE，也沒有跑過真的 cloudflared**：`/quicktunnel` 和 `isSelf` 是照文件寫的，第一次上線要特別看這兩個。

步驟（README「LINE 伺服器」一節有完整版）：
1. 建 Messaging API channel，開啟「Allow bot to join group chats」，在 LINE Official Account Manager 關掉自動回應和加入好友的歡迎訊息。
2. `.env` 填 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`GUARDIAN_HASH_SALT`、`GUARDIAN_LINE_USER_IDS`，先設 `GUARDIAN_LLM_MODE=offline` 測通道。
3. 開 `uvicorn apps.line_server:app --port 8000` 和 `cloudflared tunnel --url http://localhost:8000`。
4. 建一個只有守護人的測試群組，把官方帳號加進去。伺服器畫面會印出群組編號，填進 `GUARDIAN_GROUP_IDS` 再重新啟動。
5. 在群組傳一則有詐騙特徵的訊息：群組裡應該沒有回應，守護人應該收到私訊報告；私訊「今日摘要」應該收到摘要。

### Phase 5 的真實結果（2026-11-02，gpt-oss-120b）
`python -m guardian.experiment` 不需要金鑰就能重現，完整表格在 `results/experiment.md`。模型比較的結論見決策 D54。

| 做法 | 召回率 | 精確率 | 打擾／100 則 | 延遲（則） | 模型呼叫 | token | 注入成功率 |
|---|---|---|---|---|---|---|---|
| A 純規則 | 80% | 58% | 9.1 | 0.0 | 0 | 0 | 0% |
| B 單一 LLM | 95% | 78% | 13.6 | 0.0 | 198 | 94,786 | 0% |
| C Super-agent | 75% | 86% | 12.1 | 0.2 | 110 | 61,734 | 0% |
| C 拿掉快速過濾 | 100% | 83% | 14.6 | 0.3 | 313 | 159,408 | 0% |
| C 拿掉提前行動 | 60% | 95% | 7.6 | 0.2 | 71 | 38,867 | 30% |
| C 拿掉批次處理 | 75% | 86% | 12.1 | 0.0 | 113 | 62,570 | 0% |

要寫進報告的重點：
- C 的召回率比 B 低。漏掉的 5 則裡，4 則是規則完全抓不到（e172、e174、e176、e177，到不了模型）；1 則模型判 `medium`，照 3-6 丟棄（e020）。
- C 的誤報 4 則都是真的家人要錢的訊息（e069、e086、e121、e192），人也不容易判斷。
- 批次處理在這份資料幾乎沒差（少 3 次呼叫）：連續轉傳只有兩波。
- 10 則注入攻擊模型全部沒上當，程式防線這次沒有派上用場（覆蓋 0 次）；「拿掉提前行動」的 30% 是沒查證，不是模型被騙。
- 劇本和規則是同一個人寫的，規則抓得到的比例不代表真實情況；詐騙密度（15%）也遠高於真實群組，真實群組裡 C 的呼叫數應該比 B 少更多。
- 如果要改善召回率：改規則或 prompt 要用新的訊息驗證，不能看著這份答案調，改了 prompt 要重錄。

要重錄時（改了 prompt、模型或實驗劇本，`tests/test_recorded_replay.py` 會失敗）：`python -m guardian.experiment --record`，沒變的 prompt 不會重花 token，再更新測試裡的數字和 `results/experiment.md`。這次全部花了約 36 萬 token；Groq 的 gpt-oss-120b 每天上限 20 萬 token（回復很慢，每分鐘約 140），碰到上限會停下來，再跑一次接著錄。

## 之後的 Phase（細節見 SPEC 第 12 節）
- Phase 7 家庭試行（需要使用者本人）：真實訊息要用 `live`，開啟 Groq 的 Zero Data Retention，取得全員同意（SPEC 第 8、14 節）。`records/` 的紀錄就是 11-2 要統計的資料，統計工具還沒做。
- Phase 8 收尾（notebook、報告、影片）。

## 要等使用者的事
- LINE 伺服器要用真實的 165 名單：每週跑一次 `python -m guardian.data_165 --update`（2026-11-02 跑過，115,589 個網域）。模擬和實驗用的是凍結的那一份，不受影響。
- 「遭停止解析涉詐網站」的資料集編號 176455 已查過 API 確認標題相符，不用再確認。
- 確認決策 D24（Watchdog 通知不用 push）、D31（截圖文字辨識先不做）和 D42（只處理允許的群組）。
- Phase 6 要 LINE channel 和 cloudflared（步驟見「下一步」）；Phase 7 要家人同意。Groq 金鑰已經放在 `.env`。
- 這些都不要自己繞過：沒有金鑰就用 offline 模式寫完、測完，回報時列出還沒錄製的部分。

## 在這台電腦（Windows）上
- `python -m venv .venv`，PowerShell 用 `.venv\Scripts\Activate.ps1`（cmd 用 `.venv\Scripts\activate`），再 `pip install -r requirements.txt`。
- 測試用 `python -m pytest -q`。程式要相容 Colab（Python 3.12）。
- 第一次接手先 `git init`，commit 目前狀態；之後每個 Phase 做完 commit 一次。
