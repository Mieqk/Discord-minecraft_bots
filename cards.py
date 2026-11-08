"""Карточки в палитре: чёрный фон, красные тени, золотые тонкие акценты."""
import io
import threading
from functools import lru_cache, wraps
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

W = 900
BLACK = (10, 10, 12)
DARK_RED = (46, 6, 10)
RED = (170, 18, 32)
GOLD = (212, 175, 55)
WHITE = (240, 236, 228)
GREY = (150, 145, 140)
ORANGE = (230, 140, 40)
GREEN = (90, 180, 110)
RED_LIGHT = (214, 54, 64)

FONT_CANDIDATES = [
    "fonts/bold.ttf",  # свой шрифт с кириллицей (Montserrat / Unbounded / Inter)
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arialbd.ttf",
]


_render_lock = threading.Lock()


def serialized(fn):
    """Шрифты из кэша общие, а FreeType не потокобезопасен: рисуем по одной картинке за раз."""
    @wraps(fn)
    def wrapper(*a, **kw):
        with _render_lock:
            return fn(*a, **kw)
    return wrapper


@lru_cache(maxsize=None)
def font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def base(h, glow=(-120, None)):
    """Копия кэшированного фона (градиент, свечение и уголки считаются один раз)."""
    return _base(h, glow[0]).copy()


@lru_cache(maxsize=32)
def _base(h, glow_x):
    """Чёрный фон, внизу тёмно-красный градиент, красное свечение, золотые уголки."""
    glow = (glow_x, None)
    grad = Image.linear_gradient("L").resize((W, h))
    img = Image.composite(Image.new("RGB", (W, h), DARK_RED), Image.new("RGB", (W, h), BLACK), grad).convert("RGBA")

    layer = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((glow[0], h - 140, glow[0] + 520, h + 160), fill=RED + (120,))
    img = Image.alpha_composite(img, layer.filter(ImageFilter.GaussianBlur(70)))

    d = ImageDraw.Draw(img)
    m, L = 16, 38  # отступ и длина уголка
    g = GOLD + (210,)
    for (x, y, dx, dy) in [(m, m, 1, 1), (W - m, m, -1, 1), (m, h - m, 1, -1), (W - m, h - m, -1, -1)]:
        d.line((x, y, x + dx * L, y), fill=g, width=2)
        d.line((x, y, x, y + dy * L), fill=g, width=2)
    return img


def circle_avatar(data, size):
    av = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(av, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return out


def wrap(d, text, fnt, max_w, max_lines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if d.textlength(test, font=fnt) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(". ") + "…"
    return lines or [""]


def to_buf(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf


@serialized
def make_card(label, title, status, status_color, author, avatar=None, footer=""):
    """Карточка заявки: тема крупно, статус плашкой."""
    h = 300
    img = base(h)
    x = 70
    if avatar:
        sh = Image.new("RGBA", (W, h), (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse((40, 70, 230, 260), fill=RED + (230,))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(18)))
        av = circle_avatar(avatar, 150)
        img.paste(av, (60, 75), av)
        ImageDraw.Draw(img).ellipse((56, 71, 214, 229), outline=GOLD, width=2)
        x = 250
    d = ImageDraw.Draw(img)

    d.text((x, 48), label.upper(), font=font(18), fill=GOLD)
    y = 82
    for line in wrap(d, title, font(38), W - x - 60, 2):
        d.text((x, y), line, font=font(38), fill=WHITE)
        y += 48
    d.line((x, y + 8, x + 70, y + 8), fill=GOLD, width=2)

    d.text((x, h - 100), author, font=font(21), fill=GREY)
    tw = d.textlength(status, font=font(20))
    d.rounded_rectangle((x, h - 66, x + tw + 36, h - 28), radius=19, outline=status_color, width=2)
    d.text((x + 18, h - 60), status, font=font(20), fill=status_color)
    if footer:
        d.text((W - 60 - d.textlength(footer, font=font(20)), h - 60), footer, font=font(20), fill=GREY)
    return to_buf(img)


@serialized
def make_banner(title, subtitle=""):
    """Баннер для панели."""
    h = 260
    img = base(h, glow=(180, None))
    d = ImageDraw.Draw(img)
    f = font(52)
    while d.textlength(title, font=f) > W - 120 and f.size > 24:
        f = font(f.size - 4)
    d.text(((W - d.textlength(title, font=f)) / 2, 78), title, font=f, fill=WHITE)
    d.line((W / 2 - 45, 150, W / 2 + 45, 150), fill=GOLD, width=2)
    if subtitle:
        d.text(((W - d.textlength(subtitle, font=font(24))) / 2, 168), subtitle, font=font(24), fill=GREY)
    return to_buf(img)


@serialized
def make_leaderboard(title, rows, summary=""):
    """rows: [(имя, всего обработано, одобрено, среднее время текстом)]"""
    rows = rows[:8]
    h = 190 + 62 * max(len(rows), 1)
    img = base(h)
    d = ImageDraw.Draw(img)
    d.text((60, 44), title.upper(), font=font(32), fill=WHITE)
    d.line((60, 92, 130, 92), fill=GOLD, width=2)
    if summary:
        d.text((60, 104), summary, font=font(19), fill=GREY)
    y = 150
    if not rows:
        d.text((60, y + 10), "Пока никто не обработал заявки", font=font(24), fill=GREY)
    for i, (name, total, ok, avg) in enumerate(rows, 1):
        d.rounded_rectangle((60, y, W - 60, y + 50), radius=12, fill=(24, 24, 27, 235))
        d.rounded_rectangle((60, y, 66, y + 50), radius=3, fill=RED)
        d.text((86, y + 10), f"{i}", font=font(24), fill=GOLD)
        d.text((130, y + 10), name[:22], font=font(24), fill=WHITE)
        right = f"{total} шт  •  принято {ok}  •  ср. {avg}"
        d.text((W - 80 - d.textlength(right, font=font(20)), y + 14), right, font=font(20), fill=GOLD if i == 1 else GREY)
        y += 62
    return to_buf(img)


# ====== ЦИТАТЫ ======
REGULAR_CANDIDATES = [
    "fonts/regular.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "arial.ttf",
]
PEACH_TOP = (255, 214, 168)
PEACH_BOTTOM = (255, 249, 242)
INK = (45, 28, 22)
BROWN = (140, 98, 62)
ORANGE_SOFT = (235, 170, 110)


@lru_cache(maxsize=None)
def font_regular(size):
    for p in REGULAR_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return font(size)


@serialized
def make_quote(text, handle, avatar=None, style="dark", background=None):
    """Цитата: аватар слева, крупный текст, тонкая линия и подпись.
    style="dark" - палитра проекта (чёрный фон, красное свечение, золотые уголки), style="cream" - светлый градиент.
    background - байты картинки: ложится фоном с затемнением (как в примере с драконом)."""
    text = " ".join(text.split())[:400]
    tf = font_regular(44) if style == "cream" else font(40)
    probe = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    x = 230
    lines = wrap(probe, text, tf, W - x - 60, 6)
    h = max(240, 90 + len(lines) * 58 + 100)

    bg = None
    if background:
        try:
            bg = ImageOps.fit(Image.open(io.BytesIO(background)).convert("RGB"), (W, h), Image.LANCZOS).convert("RGBA")
        except Exception:
            bg = None  # битая картинка - рисуем без неё
    if bg is not None:
        shade = Image.new("RGBA", (W, h), (0, 0, 0, 150))
        img = Image.alpha_composite(bg, shade)
        ink, sub, mark = WHITE, (215, 205, 195), GOLD
    elif style == "dark":
        img = base(h)
        ink, sub, mark = WHITE, GREY, GOLD
    else:
        grad = Image.linear_gradient("L").resize((W, h))
        img = Image.composite(Image.new("RGB", (W, h), PEACH_BOTTOM), Image.new("RGB", (W, h), PEACH_TOP), grad).convert("RGBA")
        ink, sub, mark = INK, BROWN, ORANGE_SOFT

    d = ImageDraw.Draw(img)
    if bg is not None:  # золотые уголки, как на остальных карточках
        for (cx, cy, dx, dy) in [(16, 16, 1, 1), (W - 16, 16, -1, 1), (16, h - 16, 1, -1), (W - 16, h - 16, -1, -1)]:
            d.line((cx, cy, cx + dx * 38, cy), fill=GOLD + (210,), width=2)
            d.line((cx, cy, cx, cy + dy * 38), fill=GOLD + (210,), width=2)
    if avatar:
        av = circle_avatar(avatar, 130)
        ay = (h - 130) // 2
        img.paste(av, (60, ay), av)
        if style == "dark" or bg is not None:
            d.ellipse((57, ay - 3, 193, ay + 133), outline=GOLD, width=2)
    d.text((x - 6, 24), "“", font=font(60), fill=mark)

    y = 76
    for line in lines:
        d.text((x, y), line, font=tf, fill=ink)
        y += 58
    d.line((x, h - 74, x + 44, h - 74), fill=mark, width=2)
    sf = font_regular(24)
    d.text((x, h - 62), handle[:50], font=sf, fill=sub)
    return to_buf(img)
