"""產生圖卡用的字型：Noto Sans TC 粗體，只留常用字（Big5 常用字加英數標點），讓 repo 和 Colab 都不必另外裝字型。

只有要重做字型時才需要跑，平常不用。需要 fonttools（不在 requirements.txt）：
    pip install fonttools
    python assets/build_font.py C:/Windows/Fonts/NotoSansTC-VF.ttf
"""

import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

OUTPUT = Path(__file__).resolve().parent / "NotoSansTC-Bold-subset.ttf"
WEIGHT = 700


def common_chars() -> str:
    chars = {chr(code) for code in range(0x20, 0x7F)}  # 英數和半形標點
    chars.update("，、。．：；？！…「」『』（）《》〈〉—～＜＞％＋－＝／　")
    for lead in range(0xA4, 0xC7):  # Big5 常用字（第一字面）
        for trail in [*range(0x40, 0x7F), *range(0xA1, 0xFF)]:
            try:
                chars.add(bytes([lead, trail]).decode("big5"))
            except UnicodeDecodeError:
                pass
    return "".join(sorted(chars))


def main(source: str) -> None:
    font = TTFont(source)
    options = subset.Options()
    options.layout_features = []  # 圖卡用 Pillow 的基本排版，用不到 OpenType 功能
    options.hinting = False
    options.name_IDs = ["*"]  # 名稱表留著：裡面有著作權和授權資訊
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=common_chars())
    subsetter.subset(font)
    instancer.instantiateVariableFont(font, {"wght": WEIGHT}, inplace=True)
    font.save(OUTPUT)
    print(f"{OUTPUT.name}：{OUTPUT.stat().st_size / 1024 / 1024:.2f} MB，{font['maxp'].numGlyphs} 個字形")


if __name__ == "__main__":
    main(sys.argv[1])
