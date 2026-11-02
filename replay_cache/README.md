# 回放快取

`live` 模式每成功呼叫一次模型，就在這裡寫一個 JSON 檔（`<角色>/<雜湊>.json`）。
`replay` 模式只讀這裡，不需要金鑰，讓助教在 Colab 直接重現實驗結果。

- 檔名是「角色＋模型＋system prompt＋user prompt」的 SHA-256。prompt 有任何改動，就要重新錄製。
- 檔案只存雜湊、模型名稱、回應和 token 用量，不存 prompt 原文。
- 找不到對應的檔案時會直接報錯，不會偷偷改用 `live`。

錄製：`python -m guardian.experiment --record`（需要 `GROQ_API_KEY`）。已經錄過的 prompt 不會再花 token；中途失敗會停下來，再跑一次接著錄。

目前這份是 2026-11-02 錄的，735 筆，內容都是模擬訊息。模型給不出能用的回覆時，檔案裡會寫 `"unusable": true`，回放時重現成同樣的失敗（決策 D53）。
