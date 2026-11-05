import io
import cards
from PIL import Image


def png(buf):
    img = Image.open(buf)
    img.load()
    return img


def test_all_cards_render():
    assert png(cards.make_card("Заявка", "Заявка • x", "Принята", cards.GREEN, "x", None, "#1")).size[0] == cards.W
    assert png(cards.make_banner("Сервер", "Баг")).size[0] == cards.W
    assert png(cards.make_leaderboard("Топ", [("a", 3, 2, "1 мин")] * 10, "x")).size[0] == cards.W


def test_quote_styles_and_background():
    bg = io.BytesIO()
    Image.new("RGB", (300, 200), (10, 90, 160)).save(bg, "PNG")
    for style in ("cream", "dark"):
        assert png(cards.make_quote("Привет, мир", "@x", None, style)).size[0] == cards.W
    assert png(cards.make_quote("С фоном", "@x", None, "cream", bg.getvalue())).size[0] == cards.W
    assert png(cards.make_quote("Битый фон", "@x", None, "cream", b"not an image")).size[0] == cards.W


def test_quote_long_text_is_bounded():
    img = png(cards.make_quote("слово " * 300, "@x"))
    assert img.size[1] < 700


def test_base_is_cached_but_copied():
    a, b = cards.base(300), cards.base(300)
    assert a is not b
    a.putpixel((5, 5), (1, 2, 3, 255))
    assert cards.base(300).getpixel((5, 5)) != (1, 2, 3, 255)
