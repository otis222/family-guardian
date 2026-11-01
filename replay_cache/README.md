# 回放快取

`live` 模式每成功呼叫一次模型，就在這裡寫一個 JSON 檔（`<角色>/<雜湊>.json`）。
`replay` 模式只讀這裡，不需要金鑰，讓助教在 Colab 直接重現實驗結果。

- 檔名是「角色＋模型＋system prompt＋user prompt」的 SHA-256。prompt 有任何改動，就要重新錄製。
- 檔案只存雜湊、模型名稱、回應和 token 用量，不存 prompt 原文。
- 找不到對應的檔案時會直接報錯，不會偷偷改用 `live`。
