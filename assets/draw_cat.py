"""畫顧家貓插畫（SPEC 第 6 節）：親切的橘色家貓，固定存成 assets/guardian_cat.png。

只有要重畫時才需要跑。換成別的插畫時，直接覆蓋同檔名的 PNG（透明底、正方形）就好，程式不用改。
    python assets/draw_cat.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUTPUT = Path(__file__).resolve().parent / "guardian_cat.png"
SCALE = 4  # 先畫大再縮小，邊緣才會平滑
CANVAS = 600

LINE = "#4A2E1F"
FUR = "#F2A65A"
STRIPE = "#D9782F"
CREAM = "#FFF1DC"
EAR_INNER = "#F7B9A8"
BLUSH = (240, 130, 110, 110)
COLLAR = "#B93A12"
BELL = "#F6C544"


def main() -> None:
    size = CANVAS * SCALE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = 7 * SCALE

    def p(*points):
        return [(x * SCALE, y * SCALE) for x, y in points]

    def ellipse(cx, cy, rx, ry, fill, outline=None):
        draw.ellipse(p((cx - rx, cy - ry), (cx + rx, cy + ry)), fill=fill, outline=outline,
                     width=width if outline else 0)

    def stroke(points, fill=LINE, line_width=width):
        draw.line(p(*points), fill=fill, width=line_width, joint="curve")
        for x, y in (points[0], points[-1]):  # 線頭畫圓
            r = line_width / SCALE / 2
            ellipse(x, y, r, r, fill)

    def ear(tip, base_left, base_right, inner_tip, inner_left, inner_right):
        draw.polygon(p(tip, base_left, base_right), fill=FUR, outline=LINE, width=width)
        for x, y in (tip, base_left, base_right):  # 補滿轉角
            ellipse(x, y, 3.5, 3.5, LINE)
        draw.polygon(p(inner_tip, inner_left, inner_right), fill=EAR_INNER)

    # 身體和圍兜
    ellipse(300, 565, 190, 150, FUR, LINE)
    ellipse(300, 590, 100, 110, CREAM)

    # 項圈（頭蓋住兩端）
    draw.arc(p((190, 320), (410, 462)), 20, 160, fill=COLLAR, width=24 * SCALE)

    # 耳朵（頭蓋住耳朵下緣）
    ear((118, 42), (110, 240), (262, 120), (133, 88), (124, 205), (232, 134))
    ear((482, 42), (490, 240), (338, 120), (467, 88), (476, 205), (368, 134))

    # 頭
    ellipse(300, 262, 212, 180, FUR, LINE)

    # 額頭和臉頰的條紋
    stripe_width = 13 * SCALE
    for points in ([(300, 92), (300, 150)], [(250, 100), (262, 148)], [(350, 100), (338, 148)],
                   [(102, 250), (150, 262)], [(112, 305), (156, 306)],
                   [(498, 250), (450, 262)], [(488, 305), (444, 306)]):
        stroke(points, STRIPE, stripe_width)

    # 臉頰的紅暈（半透明，另外疊一層）
    blush = Image.new("RGBA", image.size, (0, 0, 0, 0))
    blush_draw = ImageDraw.Draw(blush)
    for cx in (172, 428):
        blush_draw.ellipse(p((cx - 40, 300 - 24), (cx + 40, 300 + 24)), fill=BLUSH)
    image.alpha_composite(blush)

    # 鼻口
    ellipse(262, 330, 58, 46, CREAM)
    ellipse(338, 330, 58, 46, CREAM)

    # 眼睛：圓圓的，看起來親切
    for cx in (208, 392):
        ellipse(cx, 248, 34, 40, LINE)
        ellipse(cx + 11, 234, 13, 15, "#FFFFFF")
        ellipse(cx - 11, 264, 6, 7, "#FFFFFF")

    # 鼻子和微笑
    draw.polygon(p((284, 294), (316, 294), (300, 314)), fill="#E8816F", outline=LINE, width=4 * SCALE)
    stroke([(300, 314), (300, 332)], line_width=5 * SCALE)
    draw.arc(p((256, 310), (302, 356)), 15, 165, fill=LINE, width=6 * SCALE)
    draw.arc(p((298, 310), (344, 356)), 15, 165, fill=LINE, width=6 * SCALE)

    # 鬍鬚
    for start, end in (((130, 332), (58, 318)), ((130, 352), (60, 362)),
                       ((470, 332), (542, 318)), ((470, 352), (540, 362))):
        stroke([start, end], line_width=4 * SCALE)

    # 鈴鐺
    ellipse(300, 470, 28, 28, BELL, LINE)
    stroke([(280, 470), (320, 470)], line_width=4 * SCALE)
    ellipse(300, 484, 5, 5, LINE)

    image.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS).save(OUTPUT, optimize=True)
    print(f"{OUTPUT.name}：{OUTPUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
