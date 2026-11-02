"""圖卡 Worker（SPEC 3-4、第 6 節）：模板排版加上固定的顧家貓插畫。

文字全部固定、由程式排版：不放訊息內容，也不放模型的文字，所以不會有個資或斷言（決策 D23）。
素材都在 assets/，不依賴系統字型，Colab 上也能跑。

    python -m guardian.workers.card 圖卡.png
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
FONT_PATH = ASSETS_DIR / "NotoSansTC-Bold-subset.ttf"
CAT_PATH = ASSETS_DIR / "guardian_cat.png"

CARD_SIZE = 1040  # 正方形，LINE 聊天室裡的縮圖最大
MIN_FONT_PX = 60  # 縮圖寬約 270 dp 時大約是 15 dp，長輩不用放大也讀得到
SCALE = 2  # 先畫大再縮小，邊緣才會平滑

BACKGROUND = "#FFF3DE"  # 暖米色
ACCENT = "#B93A12"  # 單一警示色（橘紅）
INK = "#3B2A20"
PANEL = "#FFFFFF"

HEADLINE = ("這則訊息有", "詐騙常見的特徵")
STEPS = (
    ("停", "先不要點連結、不要轉帳"),
    ("查", "打 165 查證真假"),
    ("問", "問問家人再決定"),
)
FOOTER = "有疑問打 165"

MARGIN = 52
CAT_SIZE = 300
ROWS_TOP = 330
ROW_HEIGHT = 160
ROW_GAP = 12
BADGE_SIZE = 116
FOOTER_TOP = 858


@dataclass(frozen=True)
class TextBlock:
    """一段文字的位置。anchor 是 Pillow 的錨點：lm 靠左、mm 置中。"""

    text: str
    size: int
    xy: tuple[int, int]
    anchor: str
    fill: str

    def box(self) -> tuple[float, float, float, float]:
        """畫出來的範圍（以 CARD_SIZE 為準），給排版檢查用。"""
        left, top, right, bottom = _font(self.size).getbbox(self.text, anchor=self.anchor)
        x, y = self.xy
        return (x + left / SCALE, y + top / SCALE, x + right / SCALE, y + bottom / SCALE)


def _font(size: int) -> ImageFont.FreeTypeFont:
    # 指定基本排版：不管有沒有裝 raqm，結果都一樣
    return ImageFont.truetype(str(FONT_PATH), size * SCALE, layout_engine=ImageFont.Layout.BASIC)


def _row_top(index: int) -> int:
    return ROWS_TOP + index * (ROW_HEIGHT + ROW_GAP)


def cat_box() -> tuple[int, int, int, int]:
    """顧家貓坐在第一列上面。"""
    left = CARD_SIZE - 20 - CAT_SIZE
    return (left, ROWS_TOP - CAT_SIZE, left + CAT_SIZE, ROWS_TOP)


def layout() -> tuple[TextBlock, ...]:
    """上方一句結論、中間「停、查、問」、底部「有疑問打 165」（SPEC 第 6 節的固定版型）。"""
    blocks = [TextBlock(line, 92, (MARGIN, 112 + 110 * index), "lm", ACCENT) for index, line in enumerate(HEADLINE)]
    badge_x = MARGIN + 24 + BADGE_SIZE // 2
    for index, (word, text) in enumerate(STEPS):
        middle = _row_top(index) + ROW_HEIGHT // 2
        blocks.append(TextBlock(word, 76, (badge_x, middle), "mm", PANEL))
        blocks.append(TextBlock(text, 64, (MARGIN + 24 + BADGE_SIZE + 28, middle), "lm", INK))
    blocks.append(TextBlock(FOOTER, 96, (CARD_SIZE // 2, (FOOTER_TOP + CARD_SIZE) // 2), "mm", PANEL))
    return tuple(blocks)


def render_card() -> Image.Image:
    size = CARD_SIZE * SCALE
    canvas = Image.new("RGB", (size, size), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    for index in range(len(STEPS)):
        top = _row_top(index)
        draw.rounded_rectangle(
            (MARGIN * SCALE, top * SCALE, (CARD_SIZE - MARGIN) * SCALE, (top + ROW_HEIGHT) * SCALE),
            radius=36 * SCALE, fill=PANEL,
        )
        badge_left = MARGIN + 24
        badge_top = top + (ROW_HEIGHT - BADGE_SIZE) // 2
        draw.ellipse(
            (badge_left * SCALE, badge_top * SCALE,
             (badge_left + BADGE_SIZE) * SCALE, (badge_top + BADGE_SIZE) * SCALE),
            fill=ACCENT,
        )
    draw.rectangle((0, FOOTER_TOP * SCALE, size, size), fill=ACCENT)

    left, top, right, bottom = cat_box()
    with Image.open(CAT_PATH) as cat:
        cat = cat.convert("RGBA").resize(((right - left) * SCALE, (bottom - top) * SCALE), Image.Resampling.LANCZOS)
    canvas.paste(cat, (left * SCALE, top * SCALE), cat)

    for block in layout():
        x, y = block.xy
        draw.text((x * SCALE, y * SCALE), block.text, font=_font(block.size), fill=block.fill, anchor=block.anchor)
    return canvas.resize((CARD_SIZE, CARD_SIZE), Image.Resampling.LANCZOS)


@lru_cache(maxsize=1)
def warning_card_png() -> bytes:
    """警示圖卡的 PNG。內容固定，只畫一次。素材遺失時丟出 OSError，由呼叫的人決定只送文字。"""
    buffer = io.BytesIO()
    render_card().save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


if __name__ == "__main__":
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "card.png")
    output.write_bytes(warning_card_png())
    print(f"已輸出 {output}")
