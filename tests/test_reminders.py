import asyncio
import datetime
from types import SimpleNamespace as NS

import discord
import bot as b


class FakeChannel:
    def __init__(self, last_age):
        ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=last_age)
        self.last_message_id = discord.utils.time_snowflake(ts)
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def work_sub(store, handler=77):
    sid = store.create_sub(1, "app", 10, "t", {})
    store.db.execute("UPDATE subs SET status='work', handler_id=?, ticket_channel_id=500, created_ts=? WHERE id=?",
                     (handler, store.now() - 10 * 86400, sid))
    store.db.commit()


def test_handler_pinged_in_ticket_after_silence(store, monkeypatch):
    work_sub(store)
    quiet, lively = FakeChannel(2 * 86400), FakeChannel(3600)
    monkeypatch.setattr(b.bot, "get_channel", lambda cid: quiet)
    asyncio.run(b.remind_handlers())
    assert len(quiet.sent) == 1 and "<@77>" in quiet.sent[0]       # тегаем взявшего, а не роль
    monkeypatch.setattr(b.bot, "get_channel", lambda cid: lively)
    asyncio.run(b.remind_handlers())
    assert lively.sent == []                                        # в тикете недавно писали


def test_closed_and_new_are_not_pinged(store, monkeypatch):
    sid = store.create_sub(1, "app", 10, "t", {})                  # новая, тикета нет
    store.db.execute("UPDATE subs SET status='approved', handler_id=1, ticket_channel_id=500 WHERE id=?", (sid,))
    ch = FakeChannel(5 * 86400)
    monkeypatch.setattr(b.bot, "get_channel", lambda cid: ch)
    asyncio.run(b.remind_handlers())
    assert ch.sent == []


def test_role_ping_mentions_only_that_role(store):
    store.set_gcfg(1, app_role_id=123)
    calls = []

    class Msg:
        async def reply(self, text, allowed_mentions=None):
            calls.append((text, allowed_mentions))

    sub = {"guild_id": 1, "kind": "app"}
    asyncio.run(b.ping_role(Msg(), sub, "привет"))
    text, allowed = calls[0]
    assert text.startswith("<@&123>")
    assert allowed.everyone is False and [r.id for r in allowed.roles] == [123]
