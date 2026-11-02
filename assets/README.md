# 圖卡素材

- `guardian_cat.png`：顧家貓插畫（600×600，透明底），由 `draw_cat.py` 畫成。要換別的插畫時，直接覆蓋這個檔案（正方形、透明底），程式不用改，再用 `python -m guardian.workers.card card.png` 看結果。
- `NotoSansTC-Bold-subset.ttf`：Noto Sans TC 粗體，只留 Big5 常用字加英數標點，由 `build_font.py` 從完整字型產生。Colab 沒有中文字型，所以放在 repo 裡。
- `OFL.txt`：字型的授權（SIL Open Font License 1.1）。字型散布時要一起帶著。

圖卡上的文字改在 `guardian/workers/card.py`，改完跑 `pytest -q`：測試會檢查字數放不放得下、字型有沒有這個字，也會用發言前自檢檢查措辭。
