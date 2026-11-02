# 165 名單快取

`python -m guardian.data_165 --update` 會把 165 開放資料下載到這裡（來源設定在 `config.yaml` 的 `data_165`）：

- `<來源 id>.csv`：只有 `domain` 一欄，已經轉小寫、拿掉 `www.`。
- `rumors.csv`：165 詐騙闢謠專區，`title`、`content` 兩欄，給查證 Worker 的 `search_165_rumors` 用。
- `meta.json`：每個來源的下載時間、筆數和下載網址。

超過 7 天才會重新下載；下載失敗會沿用舊檔案。資料是政府開放資料（政府資料開放授權第 1 版）。

這裡的資料每週更新，只有 LINE 伺服器用，不進版控。模擬和實驗用的是 `scenarios/snapshot_165/` 凍結的那一份，回放才對得上（決策 D35）。
