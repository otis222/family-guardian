# 凍結的 165 闢謠專區

模擬和實驗用的闢謠資料。`python -m guardian.experiment --record` 第一次錄製時，會把 `data/165/rumors.csv` 複製一份到這裡。

查證的 prompt 會帶檢索結果，資料一變，回放就對不上（決策 D35），所以這份不跟著每週更新。要換資料就刪掉 `rumors.csv` 和 `replay_cache/`，重新錄製。

資料來源：165 反詐騙諮詢專線詐騙闢謠專區（data.gov.tw 資料集 38262），政府資料開放授權第 1 版。
