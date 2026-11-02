"""圖卡（SPEC 第 6 節、第 4 節；Phase 4 驗收：圖卡輸出正確）。"""

import io
from itertools import combinations

import pytest
from helpers import Scripted, event, make_engine, verify_json
from PIL import Image, ImageColor, ImageFont

from guardian import engine as engine_module
from guardian.config import load_config
from guardian.router import NEUTRAL_TEMPLATE, UNVERIFIED_TEMPLATE, review_draft
from guardian.workers import card


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def image():
    return Image.open(io.BytesIO(card.warning_card_png())).convert("RGB")


def all_text():
    return "".join(block.text for block in card.layout())


def contrast(first, second):
    def luminance(color):
        channels = [value / 255 for value in ImageColor.getrgb(color)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# ---------------------------------------------------------------- 輸出

def test_card_is_a_square_png_small_enough_for_line():
    data = card.warning_card_png()
    with Image.open(io.BytesIO(data)) as output:
        assert output.format == "PNG"
        assert output.size == (card.CARD_SIZE, card.CARD_SIZE)
    assert len(data) < 1_000_000  # LINE 預覽圖的上限是 1 MB


def test_same_card_every_time():
    assert card.render_card().tobytes() == card.render_card().tobytes()
    assert card.warning_card_png() is card.warning_card_png()  # 只畫一次


# ---------------------------------------------------------------- 版型（SPEC 第 6 節）

def test_layout_is_conclusion_then_three_steps_then_165_from_top_to_bottom():
    blocks = card.layout()
    assert [block.text for block in blocks[:2]] == ["這則訊息有", "詐騙常見的特徵"]
    assert [word for word, _ in card.STEPS] == ["停", "查", "問"]
    assert blocks[-1].text == "有疑問打 165"
    middles = [block.xy[1] for block in blocks]
    assert middles == sorted(middles)
    assert blocks[0].size > blocks[2].size and blocks[-1].size > blocks[3].size  # 結論和 165 最大


def test_text_stays_on_the_card_without_overlapping():
    boxes = [block.box() for block in card.layout()]
    for left, top, right, bottom in boxes:
        assert 0 <= left and right <= card.CARD_SIZE - 16 and 0 <= top and bottom <= card.CARD_SIZE - 16
    for first, second in combinations(boxes, 2):
        apart = first[2] <= second[0] or second[2] <= first[0] or first[3] <= second[1] or second[3] <= first[1]
        assert apart, (first, second)
    cat = card.cat_box()
    for left, top, right, bottom in boxes:
        assert right <= cat[0] or cat[2] <= left or bottom <= cat[1] or cat[3] <= top


def test_font_is_big_enough_to_read_without_zooming():
    assert min(block.size for block in card.layout()) >= card.MIN_FONT_PX


def test_colors_are_warm_with_one_accent_and_high_contrast():
    assert contrast(card.ACCENT, card.BACKGROUND) >= 4.5
    assert contrast(card.PANEL, card.ACCENT) >= 4.5
    assert contrast(card.INK, card.PANEL) >= 7
    red, green, blue = ImageColor.getrgb(card.BACKGROUND)
    assert red > green > blue  # 暖色


def test_rendered_pixels_match_the_layout(image):
    assert image.getpixel((4, 4)) == ImageColor.getrgb(card.BACKGROUND)
    assert image.getpixel((4, card.CARD_SIZE - 4)) == ImageColor.getrgb(card.ACCENT)
    for block in card.layout():  # 每段文字都真的畫出來了
        region = image.crop(tuple(int(value) for value in block.box()))
        counts = {color: count for count, color in region.getcolors(1 << 24)}
        assert counts.get(ImageColor.getrgb(block.fill), 0) > 50, block.text
    cat = image.crop(card.cat_box())
    assert len(cat.getcolors(1 << 24)) > 100  # 插畫有貼上去


# ---------------------------------------------------------------- 文字內容

def test_card_copy_passes_the_same_checks_as_group_replies():
    assert review_draft(all_text(), 100) == []  # 不斷言、不點名、不責怪，有 165，沒有網址


def test_bundled_font_covers_every_character():
    font = ImageFont.truetype(str(card.FONT_PATH), 64, layout_engine=ImageFont.Layout.BASIC)

    def shape(char):
        mask = font.getmask(char)
        return mask.size, bytes(mask)

    missing = shape("\U00020BB7")  # 不在 Big5 常用字裡，會畫成缺字方塊
    for char in set(all_text()) - {" "}:
        assert shape(char) != missing, char


def test_missing_assets_raise_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(card, "CAT_PATH", tmp_path / "nope.png")
    with pytest.raises(OSError):
        card.render_card()
    monkeypatch.undo()
    monkeypatch.setattr(card, "FONT_PATH", tmp_path / "nope.ttf")
    with pytest.raises(OSError):
        card.render_card()


# ---------------------------------------------------------------- 接上引擎

@pytest.mark.parametrize("mode,level,mention", [("gentle", "high", False), ("silent", "medium", True)])
def test_risk_reminder_comes_with_the_card(config, mode, level, mention):
    engine = make_engine(config, Scripted(verifier=verify_json(level)), scores={"c": 80})
    engine.speaking_mode = mode
    engine.run([event("c", 0, mention=mention)])
    assert engine.replies[0].card == card.warning_card_png()
    trace = [entry for entry in engine.trace if entry.stage == "card"]
    assert len(trace) == 1 and trace[0].data["size"] == len(engine.replies[0].card)  # 紀錄裡只有大小


def test_template_fallback_also_comes_with_the_card(config):
    engine = make_engine(config, Scripted(verifier=verify_json("high"), router=[
        '{"decisions":[{"event_id":"c","action":"verify","reason":"x"}]}', RuntimeError("boom")]), scores={"c": 80})
    engine.speaking_mode = "gentle"
    engine.run([event("c", 0)])
    assert engine.replies[0].source == "template" and engine.replies[0].card is not None


def test_no_card_when_there_is_nothing_to_warn_about(config):
    engine = make_engine(config, Scripted(verifier=RuntimeError("boom")), scores={"down": 80})
    engine.run([event("hi", 0, text="@顧家貓 你好", mention=True), event("down", 60, mention=True)])
    assert [(reply.text, reply.card) for reply in engine.replies] == [(NEUTRAL_TEMPLATE, None),
                                                                      (UNVERIFIED_TEMPLATE, None)]


def test_silent_mode_does_not_render_anything(config, monkeypatch):
    def fail():
        raise AssertionError("不發言就不該畫圖卡")

    monkeypatch.setattr(engine_module, "warning_card_png", fail)
    engine = make_engine(config, Scripted(verifier=verify_json("high")), scores={"c": 80})
    engine.run([event("c", 0)])
    assert engine.replies == []


def test_card_failure_still_sends_the_text(config, monkeypatch):
    def broken():
        raise FileNotFoundError("素材不見了")

    monkeypatch.setattr(engine_module, "warning_card_png", broken)
    engine = make_engine(config, Scripted(verifier=verify_json("high")), scores={"c": 80})
    engine.speaking_mode = "gentle"
    engine.run([event("c", 0)])
    assert engine.replies[0].text and engine.replies[0].card is None
    assert engine.counts["errors:card"] == 1
    assert any(entry.stage == "card" and "只送文字" in entry.message for entry in engine.trace)
