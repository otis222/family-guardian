# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %%
# 課程固定的套件（沿用老師範例的版本），再加上本專案需要的套件
# !pip install -q "aisuite[groq]==0.2.0" "groq==0.37.1" "gradio==6.30.0"
# !pip install -q PyYAML python-dotenv Pillow

# %% [markdown]
# # 顧家貓：家族群組防詐守護者
#
# 加進家族 LINE 群組的防詐機器人。平常保持安靜，只在訊息出現詐騙常見特徵時，用長輩看得懂的方式提醒，並私下給守護人一份報告。
#
# **怎麼跑**：選單的「執行階段 → 全部執行」。這份 notebook 用 `replay` 模式，讀錄好的真實模型回應重現結果，**不需要金鑰**。
#
# 這裡的訊息都是模擬的，只依 165 公開過的手法抽象改寫，不含任何真實家人的內容。
#
# | 段落 | 內容 |
# |---|---|
# | 1. 取得程式 | 下載專案，確認是回放模式 |
# | 2. 架構與 Agent 類型 | Super-agent、Tool Use、Reflection 分別在哪裡 |
# | 3. 快速過濾 | 大部分訊息不必呼叫模型就能結束 |
# | 4. 示範劇本 | 一段家族對話的決策紀錄、群組回覆和守護人報告 |
# | 5. 發言模式 | 同一段對話在三種模式下的差別 |
# | 6. 警示圖卡 | 固定版型，文字由程式排版 |
# | 7. 注入攻擊 | 訊息裡的指令不能改變任務 |
# | 8. 實驗 | 三種做法、消融實驗、模型比較 |
# | 9. 模擬器 | 可以操作的三欄介面 |
# | 10. 真實 LINE 版 | 這裡跑不了的部分，以及目前驗證到哪裡 |

# %% [markdown]
# ## 1. 取得程式
#
# 核心邏輯都在 `guardian/`，這份 notebook 只負責呼叫和顯示。`config` 固定用 `replay`，所以不會呼叫任何模型，也不會花 token。

# %%
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/otis222/family-guardian.git"
REPO_DIR = "family-guardian"

if not Path("guardian").is_dir():  # 重跑這一格，或已經在專案資料夾裡時，就不用再下載
    if Path("../guardian").is_dir():  # 從 notebooks/ 開啟
        os.chdir("..")
    else:
        if not Path(REPO_DIR).is_dir():
            subprocess.run(["git", "clone", "--depth", "1", REPO_URL, REPO_DIR], check=True)
        os.chdir(REPO_DIR)
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

from guardian.config import load_config
from guardian.experiment import mode_notice

config = dataclasses.replace(load_config(), llm_mode="replay")
print(mode_notice(config))

# %% [markdown]
# ## 2. 架構與 Agent 類型
#
# 參考 DeepSeek Harness 的 Super-agent 設計：程式先過濾、Router 統一發言、Worker 在背景查證、Watchdog 從外部監督。
#
# ```text
# LINE／模擬器事件
#   → 正規化（遮罩電話、身分證字號、帳號、地址；發送者只留雜湊）
#   → 快速過濾（純程式，不花 token）
#       ├ 無風險：只計數，大部分訊息在這裡結束
#       ├ 觀察區間：提前行動，Worker 先在背景查證，結果是 high 才交給 Router
#       ├ 已訂閱的 Thread：直接交給 Worker
#       └ 候選：進佇列，推理期間積壓的事件合併成一批
#   → Router（ignore／watch／verify；唯一對外發言的角色）
#   → Worker：查證、圖卡、回報
#   → Router 發言前自檢 → 依發言模式回覆群組；報告私訊給守護人
#
# Watchdog 從外部監督：提醒次數、呼叫和 token 預算、鬼打牆的 Thread、推播額度
# ```
#
# | Agent 類型 | 程式位置 | 做什麼 |
# |---|---|---|
# | Super-agent（Router／Worker） | `guardian/router.py`、`guardian/workers/` | Router 批次分流、統一發言；Worker 在背景工作，結果一律回給 Router |
# | Tool Use | `guardian/workers/verifier.py` | 查證 Worker 用 JSON 指定要查 165 名單、闢謠專區或特徵抽取，程式執行後把結果放回 prompt，最多 2 輪 |
# | Reflection | `guardian/router.py` | 發言前檢查有沒有點名、有沒有斷言、有沒有超過 100 字；不合格改寫一次，還不行就用固定模板 |
#
# **原則：能用程式判斷的交給程式，程式的結果優先於模型。** 網址比對、規則、遮罩、門檻都是程式做的；模型只處理程式判斷不了的部分。

# %% [markdown]
# ## 3. 快速過濾
#
# 這一格只用規則和名單，**完全沒有呼叫模型**。分數到觀察門檻才查證，到候選門檻才進 Router。
# 規則的權重和正反例在 `guardian/rules.yaml`。

# %%
from guardian.approaches import build_engine
from guardian.models import BAND_LABELS
from guardian.scenario import load_scenario

DEMO_PATH = "scenarios/demo_family.yaml"
demo_scenario = load_scenario(DEMO_PATH)
filter_engine = build_engine("C", config, demo_scenario)
print(f"門檻：觀察 ≥ {config.thresholds.watch}，候選 ≥ {config.thresholds.candidate}\n")
for message in demo_scenario.messages:
    trigger = filter_engine.normalize(message.to_raw_event(demo_scenario.group_id))
    filter_engine.fast_filter.observe(trigger)
    if message.kind == "text":
        signal = filter_engine.fast_filter(trigger)
        band = BAND_LABELS[signal.band(config.thresholds.watch, config.thresholds.candidate)]
        print(f"{message.id}｜{signal.score:>3} 分｜{band}｜{demo_scenario.member_name(message.sender)}：{message.text[:32]}")

# %% [markdown]
# ## 4. 示範劇本
#
# 預設的 `silent` 模式：群組裡不發言，每個風險事件都報告給守護人；只有被 @ 時才回答。
#
# **決策紀錄**是這份專案最重要的畫面：每一則訊息經過哪些關卡、為什麼這樣決定，都看得到。紀錄裡只有分數、規則名稱和處理結果，不含訊息原文。

# %%
from guardian.simulation import Simulation


def run_demo(speaking_mode):
    simulation = Simulation.from_path(config, DEMO_PATH, "C", speaking_mode)
    while simulation.step():
        pass
    return simulation


demo = run_demo("silent")
print(demo.status_text())
print("\n＝＝＝ 決策紀錄 ＝＝＝")
print(demo.trace_text())

# %% [markdown]
# 下面是同一次執行的結果：顧家貓在群組說了什麼、守護人收到什麼。
#
# 文案有固定原則：不說「這是詐騙」，只說「有詐騙常見的特徵」，給 165 查證步驟，不點名、不責怪轉傳的人。

# %%
print("＝＝＝ 群組回覆 ＝＝＝")
print(demo.replies_text() or "（沒有）")
print("\n＝＝＝ 守護人報告 ＝＝＝")
print(demo.reports_text())
print("\n＝＝＝ Watchdog 指標 ＝＝＝")
print(demo.watchdog_text())

# %% [markdown]
# ## 5. 發言模式
#
# 同一段對話，換發言模式只影響群組裡說不說話；守護人的報告都一樣。真實群組會先用 `silent` 量誤報，夠低才考慮開 `gentle`。

# %%
for speaking_mode in ("silent", "gentle", "on_mention"):
    run = run_demo(speaking_mode)
    print(f"{speaking_mode:<11}群組回覆 {len(run.engine.replies)} 則｜守護人報告 {len(run.engine.reports)} 則")

# %% [markdown]
# ## 6. 警示圖卡
#
# 一句結論、「停、查、問」三步驟、「有疑問打 165」。文字一律由程式排版，不讓模型畫字，所以不會出現錯字或奇怪的內容；字型也放在專案裡，Colab 不必另外安裝。

# %%
from IPython.display import Image, Markdown, display

from guardian.workers.card import warning_card_png

display(Image(warning_card_png(), width=420))

# %% [markdown]
# ## 7. 注入攻擊
#
# 訊息內容一律當成待處理的資料，裡面的指令不能改變任務。實驗資料裡有 10 則夾帶指令的訊息，例如要求系統不要提醒家人。這裡列出顧家貓對每一則的判斷。

# %%
from guardian.experiment import experiment_config
from guardian.models import RISK_LABELS

experiment_scenario = load_scenario(config.experiment.scenario)
injection_engine = build_engine("C", experiment_config(config), experiment_scenario)
injection_engine.run(experiment_scenario.raw_events())
reports = {report.event_id: report for report in injection_engine.reports}
for message in experiment_scenario.messages:
    if message.label == "injection":
        report = reports.get(message.id)
        result = f"報告：風險{RISK_LABELS[report.risk_level]}" if report and report.risk_level else "沒有報告"
        print(f"{message.id}｜{result}｜{message.text[:44]}")

# %% [markdown]
# ## 8. 實驗
#
# 200 則標註好的訊息（170 則日常、20 則詐騙、10 則注入），比較三種做法，再分別拿掉三個設計看差別。所有指標都由程式計算。
#
# - **A 純規則**：不呼叫模型。
# - **B 單一 LLM**：每則訊息都丟給模型。
# - **C Super-agent**：本專案。

# %%
from guardian.experiment import compare_report, experiment_report

display(Markdown(experiment_report(config)))

# %% [markdown]
# **怎麼解讀**
#
# - **省下的 token**：C 的 token 比 B 少約 35%，拿掉快速過濾後會暴增到 2.6 倍，這是快速過濾的價值。
# - **精確率**：C 的精確率 86% 是三者最高，A 的誤報最多（19 則）。
# - **召回率的代價**：C 的召回率 75% 比 B 的 95% 低。漏掉的 5 則裡，4 則規則完全抓不到（e172、e174、e176、e177，到不了模型），1 則模型判 `medium` 而被提前行動丟棄（e020）。
# - **誤報**：C 的 4 則誤報都是真的家人要錢的訊息（e069、e086、e121、e192），人也不容易判斷。
# - **消融實驗**：提前行動值得留，拿掉後召回率掉到 60%。拿掉它時注入成功率變成 30%，是因為那 3 則沒有查證，不是模型被騙。批次處理在這份資料幾乎沒差，只少 3 次呼叫，因為連續轉傳只有兩波。
# - **注入攻擊**：10 則模型全部沒有上當，程式防線這次沒有派上用場。
#
# **限制**：劇本和規則是同一個人寫的，規則抓得到的比例不代表真實情況；詐騙密度 15% 也遠高於真實群組，真實群組裡 C 的呼叫數應該比 B 少更多。

# %%
display(Markdown(compare_report(config)))

# %% [markdown]
# **模型比較**：用 30 則訊息，一次只換一個角色的模型。
#
# - **查證 Worker**：gpt-oss-120b 的準確率 90%、召回率 90%；qwen3.8-27b 是 93%、80%；gpt-oss-20b 只有 70%、30%，還有 13 次給不出能用的回覆。防詐優先看召回率，30 則裡差一則就差 3%，qwen 的差距不足以換，所以留在 gpt-oss-120b。
# - **Router**：三個模型的結果相同，qwen 省 17% token，但換了也要重新錄製，省的不多，所以不換。

# %% [markdown]
# ## 9. 模擬器
#
# 三欄介面：左欄是家族群組，中欄是決策紀錄，右欄是群組回覆、警示圖卡、守護人報告和 Watchdog 指標。
#
# - **上方控制列**：可以換劇本、發言模式和做法（A／B／C）。
# - **操作**：按「下一步」一則一則看，或按「自動播放」。
# - **實驗區分頁**：第 8 段的實驗也可以在這裡重跑。
#
# 下面的網址只服務這次的執行階段，裡面只有模擬資料，也不需要金鑰。

# %%
from apps.gradio_app import launch

simulator = launch(config, share="google.colab" in sys.modules, prevent_thread_lock=True)

# %% [markdown]
# ## 10. 真實 LINE 版
#
# 同一套核心還有第二個介面：`apps/line_server.py`，跑在自己的電腦上，用 Cloudflare Tunnel 接 LINE。它處理真實訊息，所以不在這份 notebook 裡。
#
# **隱私原則**
#
# - 不保存原始訊息，只存風險事件的最小紀錄：事件編號、時間、雜湊後的發送者、規則名稱、風險等級、處理結果，保留 30 天。
# - 送進模型前先遮罩個資，而且只有通過快速過濾的候選訊息才會送到模型。
# - 只處理全員同意、列在設定裡的群組。
#
# **花費原則**：能用 reply 就不用 push，push 只給 `high` 風險事件；每日摘要由守護人私訊「今日摘要」時用 reply 回覆。
#
# **目前驗證到哪裡**
#
# - 已在只有守護人的測試群組用真實 LINE 驗收：`silent` 模式下群組沒有發言，守護人收到報告和今日摘要；被 @ 時會回答並附圖卡。當時用的是假模型，只測通道。
# - 家庭試行（全員同意後跑 `silent` 模式兩週）的結果另外寫在報告裡。
#
# **重新錄製回放**：這份 notebook 不需要也不放金鑰。要錄製時在自己的電腦上把金鑰放進 `.env`，再跑 `python -m guardian.experiment --record`。
