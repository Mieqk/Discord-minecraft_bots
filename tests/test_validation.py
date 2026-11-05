import asyncio
from types import SimpleNamespace as NS

import bot as b
import mojang


def fake_interaction(uid=10, admin=False):
    guild = NS(id=1, get_role=lambda rid: None)
    user = NS(id=uid, guild=guild, guild_permissions=NS(administrator=admin), roles=[])
    return NS(guild=guild, user=user)


APP = {"age": "20", "rp": "5", "source": "x", "goal": "y", "nick": "Steve"}


def run(coro):
    return asyncio.run(coro)


def test_valid_app_passes_without_check(store):
    assert run(b.validate_submission(fake_interaction(), "app", APP, {"mc_check": False})) is None


def test_age_must_be_number(store):
    assert "числом" in run(b.validate_submission(fake_interaction(), "app", {**APP, "age": "двадцать"}, {"mc_check": False}))


def test_active_limit(store):
    store.create_sub(1, "app", 10, "t", APP)
    assert "необработанные" in run(b.validate_submission(fake_interaction(), "app", APP, {"mc_check": False}))


def test_spam_limit(store):
    for _ in range(b.SPAM_LIMIT):
        store.create_sub(1, "bug", 10, "t", {})
    # проверяем жалобу: активных жалоб нет, она упирается именно в общий часовой лимит
    err = run(b.validate_submission(fake_interaction(), "report", {"topic": "a", "details": "b"}, {}))
    assert "Слишком много" in err


def test_reapply_after_reject_and_admin_exempt(store):
    sid = store.create_sub(1, "app", 10, "t", APP)
    store.db.execute("UPDATE subs SET status='rejected', closed_ts=? WHERE id=?", (store.now(), sid))
    cfg = {"mc_check": False}
    assert "Новую можно подать" in run(b.validate_submission(fake_interaction(), "app", APP, cfg))
    assert run(b.validate_submission(fake_interaction(), "app", APP, {**cfg, "reapply_days": 0})) is None
    assert run(b.validate_submission(fake_interaction(admin=True), "app", APP, cfg)) is None


def test_nick_checks(store):
    cfg = {}
    ask = lambda nick, uid=10: run(b.validate_submission(fake_interaction(uid), "app", {**APP, "nick": nick}, cfg))
    assert ask("Steve") is None
    assert "3-16" in ask("п")
    store.create_sub(1, "app", 11, "t", {**APP, "nick": "Steve"})
    assert "уже указан" in ask("steve")


def test_mojang_is_only_a_note(store, monkeypatch):
    """Ника нет у Mojang - заявку не отклоняем (пиратки), а помечаем."""
    values = {**APP, "nick": "Propley"}
    for answer, expected in ((True, "found"), (False, "missing"), (None, None)):
        monkeypatch.setattr(b.mojang, "exists", lambda n, a=answer: asyncio.sleep(0, result=a))
        assert run(b.mojang_note(values, {})) == expected
        assert run(b.validate_submission(fake_interaction(), "app", values, {})) is None  # не блокирует
    assert run(b.mojang_note(values, {"mc_check": False})) is None


def test_mojang_note_shown_on_card():
    assert b.mojang_field({"_mojang": "missing"})[0][1].startswith("⚠️")
    assert b.mojang_field({"_mojang": "found"})[0][1].startswith("✅")
    assert b.mojang_field({}) == []


def test_mojang_status_mapping():
    from aiohttp import web

    async def go():
        app = web.Application()
        app.add_routes([web.get("/ok/{n}", lambda r: web.json_response({"id": "1"})),
                        web.get("/none/{n}", lambda r: web.Response(status=404)),
                        web.get("/err/{n}", lambda r: web.Response(status=500))])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            res = []
            for prefix in ("ok", "none", "err"):
                mojang.URL = f"http://127.0.0.1:{port}/{prefix}/{{}}"
                res.append(await mojang.exists("Steve"))
            mojang.URL = "http://127.0.0.1:1/{}"       # порт закрыт
            res.append(await mojang.exists("Steve", timeout=1))
            return res
        finally:
            await runner.cleanup()

    assert run(go()) == [True, False, None, None]
