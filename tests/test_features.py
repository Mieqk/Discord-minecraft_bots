import asyncio
import logging
import os
import struct

import pytest

import core
import errsink
import jobs
import policy
import rcon
import store as _store


def run(coro):
    return asyncio.run(coro)


# ---------- кого ждём в тикете ----------
H, P, NOW, DAY = 77, 10, 1_000_000, 86400


def act(last_human, last_any_age, last_human_age=None, close=0):
    lh_ts = NOW - (last_human_age if last_human_age is not None else last_any_age)
    return policy.silence_action(last_human, P, NOW - last_any_age, lh_ts, NOW, DAY, close)


def test_policy_ping_matrix():
    assert act(None, 2 * DAY) == "ping_handler"          # в тикете никто не писал - ждём взявшего
    assert act(P, 2 * DAY) == "ping_handler"             # последним писал игрок - ждём взявшего
    assert act(H, 2 * DAY) == "ping_player"              # последним ответил взявший - ждём игрока
    assert act(555, 2 * DAY) == "ping_player"            # ответил другой сотрудник - тоже ждём игрока
    assert act(H, DAY // 2) is None                      # ещё рано
    assert act(None, DAY // 2) is None


def test_policy_close_only_when_player_is_silent():
    assert act(H, 3600, last_human_age=4 * DAY, close=3 * DAY) == "close"   # напоминания бота не мешают закрытию
    assert act(H, 3600, last_human_age=2 * DAY, close=3 * DAY) is None
    assert act(P, 9 * DAY, close=3 * DAY) == "ping_handler"                 # молчит взявший - не закрываем
    assert act(H, 4 * DAY, close=0) == "ping_player"                        # автозакрытие выключено


# ---------- напоминания в тикете ----------
class Chan:
    def __init__(self, age):
        import datetime, discord
        self.last_message_id = discord.utils.time_snowflake(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age))
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def ticket(store, last_human_id=None, last_human_age=None, handler=77, player=10):
    sid = store.create_sub(1, "app", player, "t", {})
    ts = store.now() - last_human_age if last_human_age is not None else None
    store.db.execute("UPDATE subs SET status='work', handler_id=?, ticket_channel_id=500, last_human_id=?, "
                     "last_human_ts=?, created_ts=? WHERE id=?", (handler, last_human_id, ts, store.now() - 20 * DAY, sid))
    store.db.commit()
    return sid


def test_player_is_pinged_when_handler_answered_last(store, monkeypatch):
    ticket(store, last_human_id=77, last_human_age=2 * DAY)
    ch = Chan(2 * DAY)
    monkeypatch.setattr(core.bot, "get_channel", lambda cid: ch)
    run(jobs.remind_handlers())
    assert len(ch.sent) == 1 and "<@10>" in ch.sent[0] and "ждём твой ответ" in ch.sent[0]


def test_silent_player_ticket_is_auto_closed_when_enabled(store, monkeypatch):
    sid = ticket(store, last_human_id=77, last_human_age=5 * DAY)
    store.set_gcfg(1, auto_close_days=3)
    ch, closed = Chan(3600), []
    monkeypatch.setattr(core.bot, "get_channel", lambda cid: ch)

    async def fake_finish(sub_id, status, handler_id, reason=None):
        closed.append((sub_id, status, handler_id, reason))
    import tickets
    monkeypatch.setattr(tickets, "finish", fake_finish)
    run(jobs.remind_handlers())
    assert closed == [(sid, "rejected", 77, "Нет ответа от игрока 3 дн.")]


def test_not_closed_when_setting_is_off(store, monkeypatch):
    ticket(store, last_human_id=77, last_human_age=30 * DAY)
    ch, closed = Chan(3600), []
    monkeypatch.setattr(core.bot, "get_channel", lambda cid: ch)
    import tickets
    monkeypatch.setattr(tickets, "finish", lambda *a, **k: closed.append(a))
    run(jobs.remind_handlers())
    assert closed == [] and ch.sent == []


def test_touch_human_updates_only_open_ticket(store):
    sid = ticket(store)
    store.touch_human(500, 42, 12345)
    row = store.sub_by_id(sid)
    assert (row["last_human_id"], row["last_human_ts"]) == (42, 12345)
    store.touch_human(999, 1, 1)  # чужой канал ничего не меняет
    assert store.sub_by_id(sid)["last_human_id"] == 42


# ---------- чёрный список ----------
def test_block_list_and_validation(store):
    from types import SimpleNamespace as NS
    import forms
    guild = NS(id=1, get_role=lambda r: None)
    inter = NS(guild=guild, user=NS(id=10, guild=guild, guild_permissions=NS(administrator=False), roles=[]))
    app = {"age": "20", "rp": "5", "source": "x", "goal": "y", "nick": "Steve"}
    assert run(forms.validate_submission(inter, "app", app, {"mc_check": False})) is None
    store.block_user(1, 10, "спам", 99)
    assert "закрыта" in run(forms.validate_submission(inter, "app", app, {"mc_check": False}))
    # на жалобы и баги блокировка не распространяется
    assert run(forms.validate_submission(inter, "report", {"topic": "a", "details": "b"}, {})) is None
    assert [r["reason"] for r in store.blocked_list(1)] == ["спам"]
    assert store.unblock_user(1, 10) is True and store.unblock_user(1, 10) is False
    assert run(forms.validate_submission(inter, "app", app, {"mc_check": False})) is None


# ---------- ошибки в канал логов ----------
def test_errsink_collects_only_bot_errors_and_dedupes():
    errsink.drain()  # очистка
    h = errsink.SinkHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("bot")
    lg.addHandler(h)
    try:
        lg.warning("не попадёт")
        logging.getLogger("other").error("чужой логгер не попадёт")
        lg.error("одинаковая")
        lg.error("одинаковая")
        lg.error("с ``` внутри")
        try:
            1 / 0
        except ZeroDivisionError:
            lg.exception("упало")
    finally:
        lg.removeHandler(h)
    out = errsink.drain()
    assert out[0] == "одинаковая" and len(out) == 3            # дубль подряд схлопнут, три разных
    assert "'''" in out[1] and "```" not in out[1]              # блок кода в Discord не ломается
    assert "ZeroDivisionError" in out[2]
    assert errsink.drain() == []


def test_flush_errors_sends_to_log_channel(store, monkeypatch):
    class G:
        id = 1
    sent = []

    class Ch:
        async def send(self, text, **kw):
            sent.append(text)
    store.set_gcfg(1, log_channel_id=5)
    monkeypatch.setattr(core.bot.__class__, "guilds", property(lambda self: [G()]))
    monkeypatch.setattr(core.bot, "get_channel", lambda cid: Ch())
    errsink.drain()
    errsink._queue.append("Traceback: boom")
    run(jobs.flush_errors())
    assert sent and "boom" in sent[0]
    store.set_gcfg(1, error_log=False)
    errsink._queue.append("ещё одна")
    sent.clear()
    run(jobs.flush_errors())
    assert sent == []


# ---------- копия базы в Discord ----------
def test_backup_goes_to_discord_only_when_enabled(store, monkeypatch, tmp_path):
    import datetime
    class G:
        id = 1
    sent = []

    class Ch:
        async def send(self, text, file=None):
            sent.append(file.filename)
    monkeypatch.setattr(core.bot.__class__, "guilds", property(lambda self: [G()]))
    monkeypatch.setattr(core.bot, "get_channel", lambda cid: Ch())
    store.set_gcfg(1, log_channel_id=5)
    run(jobs.daily_backup())
    assert sent == []                                         # по умолчанию выключено
    store.set_gcfg(1, backup_discord=True)
    for f in store.backup_files():
        os.remove(os.path.join(store.backup_dir(), f))        # чтобы за сегодня копия делалась заново
    run(jobs.daily_backup())
    assert len(sent) == 1 and sent[0].startswith("data-")


# ---------- RCON ----------
def pkt(pid, ptype, body=""):
    data = struct.pack("<ii", pid, ptype) + body.encode() + b"\x00\x00"
    return struct.pack("<i", len(data)) + data


class FakeMinecraft:
    """Минимальный сервер RCON: проверяет пароль и отвечает на команды."""
    def __init__(self, password="secret"):
        self.password, self.commands = password, []

    async def handle(self, reader, writer):
        async def read():
            size = struct.unpack("<i", await reader.readexactly(4))[0]
            data = await reader.readexactly(size)
            pid, ptype = struct.unpack("<ii", data[:8])
            return pid, ptype, data[8:-2].decode()
        try:
            pid, ptype, body = await read()
            writer.write(pkt(pid if body == self.password else -1, 2))
            await writer.drain()
            if body != self.password:
                return
            pid, ptype, body = await read()
            self.commands.append(body)
            writer.write(pkt(pid, 0, f"Added {body.split()[-1]} to the whitelist"))
            await writer.drain()
        finally:
            writer.close()


def with_server(fn):
    async def go():
        srv = FakeMinecraft()
        server = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await fn(srv, port)
        finally:
            server.close()
            await server.wait_closed()
    return run(go())


def test_rcon_command_roundtrip():
    async def go(srv, port):
        reply = await rcon.run("127.0.0.1", port, "secret", "whitelist add Steve")
        assert reply == "Added Steve to the whitelist" and srv.commands == ["whitelist add Steve"]
        with pytest.raises(PermissionError):
            await rcon.run("127.0.0.1", port, "wrong", "whitelist add Steve")
    with_server(go)


def test_whitelist_add_uses_env_and_rejects_dangerous_nicks(store, monkeypatch):
    async def go(srv, port):
        monkeypatch.setenv("RCON_HOST", "127.0.0.1")
        monkeypatch.setenv("RCON_PORT", str(port))
        monkeypatch.setenv("RCON_PASSWORD", "secret")
        ok, text = await rcon.whitelist_add("Steve_1")
        assert ok and "Steve_1" in text and srv.commands == ["whitelist add Steve_1"]
        ok, text = await rcon.whitelist_add("Steve\nop Steve")   # ник не должен превратиться в вторую команду
        assert not ok and srv.commands == ["whitelist add Steve_1"]
        monkeypatch.setenv("RCON_PASSWORD", "wrong")
        ok, text = await rcon.whitelist_add("Alex")
        assert not ok and "пароль" in text
    with_server(go)


def test_whitelist_disabled_without_env(store, monkeypatch):
    monkeypatch.delenv("RCON_HOST", raising=False)
    monkeypatch.delenv("RCON_PASSWORD", raising=False)
    assert run(rcon.whitelist_add("Steve")) is None


def test_whitelist_unreachable_server_does_not_raise(store, monkeypatch):
    monkeypatch.setenv("RCON_HOST", "127.0.0.1")
    monkeypatch.setenv("RCON_PORT", "1")
    monkeypatch.setenv("RCON_PASSWORD", "x")
    ok, text = run(rcon.whitelist_add("Steve"))
    assert ok is False and "добавь вручную" in text


def test_whitelist_note_follows_setting_and_result(store, monkeypatch):
    import tickets
    sid = store.create_sub(1, "app", 10, "t", {"nick": " Steve "})
    sub = store.sub_by_id(sid)
    calls = []

    async def fake(nick):
        calls.append(nick)
        return True, "Added"
    monkeypatch.setattr(tickets.rcon, "whitelist_add", fake)
    assert "добавлен" in run(tickets.whitelist_note(sub)) and calls == ["Steve"]
    store.set_gcfg(1, whitelist=False)
    assert run(tickets.whitelist_note(sub)) == "" and calls == ["Steve"]       # выключено в /settings
    store.set_gcfg(1, whitelist=True)
    monkeypatch.setattr(tickets.rcon, "whitelist_add", lambda n: asyncio.sleep(0, result=(False, "нет связи")))
    assert "НЕ добавлен" in run(tickets.whitelist_note(sub))
    monkeypatch.setattr(tickets.rcon, "whitelist_add", lambda n: asyncio.sleep(0, result=None))
    assert run(tickets.whitelist_note(sub)) == ""                               # RCON не настроен - молча
