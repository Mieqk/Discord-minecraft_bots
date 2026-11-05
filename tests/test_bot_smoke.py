"""Бот собирается без подключения к Discord: команды, меню и кнопки зарегистрированы."""
import asyncio


def test_commands_register(store):
    import bot as b
    from quotes import Quotes

    async def go():
        await b.bot.add_cog(Quotes(b.bot))
        names = {c.name for c in b.bot.tree.get_commands()}
        assert {"setup", "setup_roles", "settings", "panel_app", "panel_report", "panel_bug", "status", "stats",
                "stats_user", "weekly_post", "queue", "ticket", "health", "quote", "quotes"} <= names
        menus = {c.name for c in b.bot.tree.get_commands(type=__import__("discord").AppCommandType.message)}
        assert menus == {"Цитата", "Цитата (тёмная)"}
        for c in b.bot.tree.get_commands():
            c.to_dict(b.bot.tree)  # описание команды валидно для Discord
        await b.bot.close()

    asyncio.run(go())


def test_submission_view_has_five_custom_ids():
    import bot as b
    ids = {c.custom_id for c in b.SubmissionView().children}
    assert ids == {"sub:details", "sub:take", "sub:ok", "sub:no", "sub:prio"}
    closed = b.SubmissionView("app", "approved", 1)
    assert all(c.disabled for c in closed.children if c.custom_id != "sub:details")


def test_modals_fit_discord_limits():
    import bot as b
    from kinds import KINDS
    for kind, k in KINDS.items():
        assert len(k["fields"]) <= 5, kind                      # в форме Discord максимум 5 полей
        assert all(len(f[1]) <= 45 for f in k["fields"]), kind  # подписи полей до 45 символов
        b.FormModal(kind)


def test_missing_token_message(monkeypatch, tmp_path):
    import pytest
    import bot as b
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr(b.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(b, "setup_logging", lambda: None)
    with pytest.raises(SystemExit, match="DISCORD_TOKEN"):
        b.main()
