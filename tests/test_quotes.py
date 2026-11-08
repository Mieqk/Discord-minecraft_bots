import asyncio
import time
from types import SimpleNamespace as NS

import quotes


def run(coro):
    return asyncio.run(coro)


def test_split_signature():
    assert quotes.split_signature("Привет, мир") == ("Привет, мир", None)
    assert quotes.split_signature("Привет, мир\n— Трикси") == ("Привет, мир", "Трикси")
    assert quotes.split_signature("Строка 1\nстрока 2\n-- Фаля") == ("Строка 1\nстрока 2", "Фаля")
    assert quotes.split_signature("— просто тире") == ("— просто тире", None)     # подпись без текста не считается
    assert quotes.split_signature("- список\n- пункт") == ("- список\n- пункт", None)  # обычный дефис не подпись
    assert quotes.split_signature("  \n текст \n— " + "я" * 80)[1] == "я" * 50


class FakeMessage:
    def __init__(self, text, channel_id=5, bot=False, attachments=()):
        self.author = NS(id=10, bot=bot, mention="<@10>")
        self.guild = NS(id=1)
        self.channel = NS(id=channel_id, sent=[])
        self.clean_content, self.attachments, self.deleted = text, list(attachments), False

        async def send(text, **kw):
            self.channel.sent.append(text)
        self.channel.send = send

    async def delete(self):
        self.deleted = True


def cog():
    posted = []

    async def post_quote(channel, user, text, signature, style, background):
        posted.append((text, signature, style, background))
    return NS(_last={}, post_quote=post_quote), posted


def test_auto_quote_in_quote_channel(store):
    store.set_gcfg(1, quote_channel_id=5)
    c, posted = cog()
    m = FakeMessage("Вот фера даст\n— Трикси")
    run(quotes.Quotes.on_message(c, m))
    assert posted == [("Вот фера даст", "Трикси", "dark", None)]   # тёмная палитра по умолчанию
    assert m.deleted is True                                       # исходное сообщение заменено цитатой


def test_other_channels_and_bots_are_ignored(store):
    store.set_gcfg(1, quote_channel_id=5)
    c, posted = cog()
    for m in (FakeMessage("текст", channel_id=6), FakeMessage("текст", bot=True)):
        run(quotes.Quotes.on_message(c, m))
        assert not m.deleted
    store.set_gcfg(1, quote_channel_id=0)                          # выключено
    m = FakeMessage("текст", channel_id=0)
    run(quotes.Quotes.on_message(c, m))
    assert posted == [] and not m.deleted


def test_long_text_and_cooldown(store):
    store.set_gcfg(1, quote_channel_id=5)
    c, posted = cog()
    long = FakeMessage("а" * 401)
    run(quotes.Quotes.on_message(c, long))
    assert posted == [] and not long.deleted and "400" in long.channel.sent[0]   # ничего не удаляем
    first, second = FakeMessage("раз"), FakeMessage("два")
    run(quotes.Quotes.on_message(c, first))
    run(quotes.Quotes.on_message(c, second))
    assert [p[0] for p in posted] == ["раз"] and not second.deleted               # второе слишком быстро


def test_image_attachment_becomes_background(store):
    store.set_gcfg(1, quote_channel_id=5)
    c, posted = cog()

    class Att:
        content_type, size = "image/png", 1000
        async def read(self):
            return b"PNGDATA"
    run(quotes.Quotes.on_message(c, FakeMessage("с фоном", attachments=[Att()])))
    assert posted[0][3] == b"PNGDATA"


def test_message_not_deleted_if_posting_fails(store):
    store.set_gcfg(1, quote_channel_id=5)
    c, _ = cog()

    async def boom(*a, **k):
        raise RuntimeError("нет вебхука")
    c.post_quote = boom
    m = FakeMessage("текст")
    run(quotes.Quotes.on_message(c, m))
    assert not m.deleted                                           # исходный текст не теряем


def test_panel_button_and_form(store):
    assert [c.custom_id for c in quotes.QuotePanelView().children] == ["panel:quote"]
    posted = []

    async def post_quote(channel, user, text, signature, style, background=None):
        posted.append((text, signature, style))
    cog_obj = NS(_last={}, post_quote=post_quote)
    sent = []

    class Resp:
        async def defer(self, ephemeral=False):
            pass

    class Follow:
        async def send(self, text, ephemeral=False):
            sent.append(text)
    inter = NS(client=NS(get_cog=lambda name: cog_obj), user=NS(id=10), channel=NS(), response=Resp(), followup=Follow())

    def submit(text, signature=""):
        modal = quotes.QuoteModal()
        modal.text._value, modal.signature._value = text, signature
        run(modal.on_submit(inter))
    submit("  Привет  ", "Трикси")
    assert posted == [("Привет", "Трикси", "dark")] and "отправлена" in sent[-1]
    submit("Без подписи")                      # пустая подпись -> None; но это второй вызов подряд: пауза
    assert len(posted) == 1 and "быстро" in sent[-1]
    cog_obj._last.clear()
    submit("Без подписи")
    assert posted[-1] == ("Без подписи", None, "dark")


def test_panel_command_sets_quote_channel(store):
    channel_sent, replies = [], []

    class Chan:
        id = 777
        async def send(self, **kw):
            channel_sent.append(kw)
        def permissions_for(self, me):
            return NS(manage_webhooks=True, manage_messages=False, add_reactions=True)

    class Resp:
        async def defer(self, ephemeral=False):
            pass

    class Follow:
        async def send(self, text, ephemeral=False):
            replies.append(text)
    inter = NS(guild=NS(id=1, name="KDA", me=object()), channel=Chan(), response=Resp(), followup=Follow())
    run(quotes.Quotes.panel_quote.callback(NS(), inter))
    assert store.gcfg(1)["quote_channel_id"] == 777            # этот канал стал каналом цитат
    assert channel_sent and channel_sent[0]["view"].children[0].custom_id == "panel:quote"
    assert "управлять сообщениями" in replies[0]              # бот сказал, какого права не хватает


def test_image_without_text_gets_a_hint(store):
    store.set_gcfg(1, quote_channel_id=5)
    c, posted = cog()

    class Att:
        content_type, size = "image/png", 10
    m = FakeMessage("   ", attachments=[Att()])
    run(quotes.Quotes.on_message(c, m))
    assert posted == [] and not m.deleted and "добавь" in m.channel.sent[0]
