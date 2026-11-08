"""Тесты на найденные при проверке ошибки: каждая падала до исправления."""
import asyncio
import io
from types import SimpleNamespace as NS

import pytest
from PIL import Image, ImageDraw

import cards
import forms
import tickets


def run(coro):
    return asyncio.run(coro)


# ---------- картинки ----------
def test_wrap_breaks_words_wider_than_the_line():
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = cards.font(40)
    lines = cards.wrap(d, "а" * 80 + " ну и слово", f, 600, 6)
    assert all(d.textlength(line, font=f) <= 600 for line in lines)
    assert "".join(lines).replace(" ", "").startswith("а" * 20)     # текст не потерян
    assert cards.wrap(d, "обычный текст", f, 600, 2) == ["обычный текст"]


def test_oversized_background_is_not_decoded(monkeypatch):
    """Маленький файл мог распаковаться в сотни МБ и убить бота на хосте с 256 МБ памяти."""
    big = io.BytesIO()
    Image.new("RGB", (400, 400), (200, 0, 0)).save(big, "PNG")
    monkeypatch.setattr(cards, "MAX_BG_PIXELS", 100 * 100)          # 400x400 = больше лимита
    with_bg = cards.make_quote("текст", "@x", None, "dark", big.getvalue()).getvalue()
    without = cards.make_quote("текст", "@x", None, "dark", None).getvalue()
    assert with_bg == without                                         # картинка просто проигнорирована
    monkeypatch.setattr(cards, "MAX_BG_PIXELS", 1000 * 1000)
    assert cards.make_quote("текст", "@x", None, "dark", big.getvalue()).getvalue() != without


def test_long_word_quote_keeps_text_inside_canvas():
    img = Image.open(cards.make_quote("а" * 120, "@x")).convert("RGB")
    right_edge = img.crop((img.width - 55, 60, img.width - 5, img.height - 70))   # поле справа, где текста быть не должно
    assert max(sum(px) for px in right_edge.getdata()) < 300


# ---------- закрытие обращения ----------
def test_finish_is_atomic(store, monkeypatch):
    sid = store.create_sub(1, "report", 10, "t", {"topic": "a", "details": "b"})
    store.db.execute("UPDATE subs SET status='work', handler_id=77 WHERE id=?", (sid,))
    store.db.commit()
    calls = {"log": 0, "dm": 0}

    async def noop(*a, **k):
        return None

    async def count_log(*a, **k):
        calls["log"] += 1

    async def count_dm(*a, **k):
        calls["dm"] += 1
    for name in ("refresh_message", "set_nickname", "whitelist_note", "lock_ticket", "collect_transcript"):
        monkeypatch.setattr(tickets, name, noop)
    monkeypatch.setattr(tickets, "send_log", count_log)
    monkeypatch.setattr(tickets, "notify_author", count_dm)

    async def twice():
        return await asyncio.gather(tickets.finish(sid, "approved", 77), tickets.finish(sid, "rejected", 88, "поздно"))
    first, second = run(twice())
    assert "уже закрыто" in second and "уже закрыто" not in first
    assert calls == {"log": 1, "dm": 1}                               # лог и ЛС не задвоились
    row = store.sub_by_id(sid)
    assert row["status"] == "approved" and row["handler_id"] == 77 and row["reason"] is None


def test_finish_survives_broken_card(store, monkeypatch):
    sid = store.create_sub(1, "report", 10, "t", {"topic": "a", "details": "b"})
    store.db.execute("UPDATE subs SET status='work', handler_id=77 WHERE id=?", (sid,))
    store.db.commit()

    async def boom(*a, **k):
        raise TypeError("карточка удалена")

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(tickets, "refresh_message", boom)
    for name in ("set_nickname", "whitelist_note", "lock_ticket", "collect_transcript", "send_log", "notify_author"):
        monkeypatch.setattr(tickets, name, noop)
    assert "Время обработки" in run(tickets.finish(sid, "approved", 77))   # доходит до конца, а не падает посередине


# ---------- форма ----------
def form_interaction(channel):
    sent = []

    class Resp:
        async def defer(self, ephemeral=False):
            pass

    class Follow:
        async def send(self, text, ephemeral=False):
            sent.append(text)
    guild = NS(id=1, get_channel=lambda cid: channel, get_role=lambda r: None)
    user = NS(id=10, display_name="Игрок", guild=guild, guild_permissions=NS(administrator=False), roles=[])
    return NS(guild=guild, user=user, response=Resp(), followup=Follow()), sent


def fill(modal, **values):
    for key, ti in modal.inputs.items():
        ti._value = values.get(key, "x")


REPORT = dict(topic="Тема", details="Описание")


def test_failed_send_does_not_lock_the_player_out(store, monkeypatch):
    store.set_gcfg(1, report_channel_id=5)

    async def fake_render(sub):
        return None, None, None

    class Chan:
        id = 5
        async def send(self, **kw):
            raise RuntimeError("у бота нет доступа")
    monkeypatch.setattr(forms, "render", fake_render)
    inter, sent = form_interaction(Chan())
    modal = forms.FormModal("report")
    fill(modal, **REPORT)
    run(modal.on_submit(inter))
    assert store.active_count(1, 10, "report") == 0                  # запись убрана, игрок не заблокирован
    assert "Не получилось отправить" in sent[-1]


def test_second_submission_during_slow_check_is_refused(store, monkeypatch):
    """Пока шла проверка ника, игрок успел подать вторую заявку: вторая не должна пройти."""
    store.set_gcfg(1, app_channel_id=5, mc_check=True)
    posted = []

    async def fake_render(sub):
        return None, None, None

    class Chan:
        id = 5
        async def send(self, **kw):
            posted.append(kw)
            return NS(id=len(posted))

    async def slow_note(values, cfg):
        store.create_sub(1, "app", 10, "t", values)                  # «параллельная» заявка того же игрока
        return None

    async def no_ping(*a, **k):
        return None
    monkeypatch.setattr(forms, "render", fake_render)
    monkeypatch.setattr(forms, "mojang_note", slow_note)
    monkeypatch.setattr(forms, "ping_role", no_ping)
    inter, sent = form_interaction(Chan())
    modal = forms.FormModal("app")
    fill(modal, age="20", nick="Steve")
    run(modal.on_submit(inter))
    assert posted == [] and "необработанные" in sent[-1]
    assert store.active_count(1, 10, "app") == 1


def test_modal_error_replies_instead_of_hanging():
    import core
    replies = []

    class Resp:
        def is_done(self):
            return True

    class Follow:
        async def send(self, text, ephemeral=False):
            replies.append(text)
    run(core.modal_error(NS(response=Resp(), followup=Follow()), RuntimeError("x")))
    assert replies and "не так" in replies[0]
