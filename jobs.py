"""Фоновые задачи: напоминания, уборка тикетов, недельный отчёт, резервные копии."""
import datetime
import discord
from discord.ext import tasks

import store
from store import db, gcfg, set_gcfg, now
from core import bot, log, ping_role
from stats import post_weekly

REMIND_AFTER = 24 * 3600        # через сколько без ответа тегать вайтлистеров
CLEANUP_AFTER = 3600            # через сколько после решения удалять канал тикета


async def remind_handlers():
    """Заявка в работе, а в тикете тихо больше суток: пингуем в самом тикете того, кто её взял.
    Сообщение бота тоже считается последним, поэтому повтор будет не чаще раза в сутки."""
    rows = db.execute("SELECT * FROM subs WHERE status='work' AND ticket_channel_id IS NOT NULL "
                      "AND handler_id IS NOT NULL").fetchall()
    for sub in rows:
        ch = bot.get_channel(sub["ticket_channel_id"])
        if not ch:
            continue
        last = (discord.utils.snowflake_time(ch.last_message_id).timestamp()
                if ch.last_message_id else sub["created_ts"])
        if now() - last < REMIND_AFTER:
            continue
        try:
            await ch.send(f"<@{sub['handler_id']}>, по обращению **#{sub['num']}** тихо больше суток. "
                          f"Ответь игроку или закрой обращение.")
        except discord.HTTPException:
            log.exception("remind_handlers")


# ====== ФОНОВЫЕ ЗАДАЧИ ======
async def remind_unanswered():
    """Обращение так никто и не взял сутки: ещё раз тегаем роль-обработчика под карточкой."""
    rows = db.execute("SELECT * FROM subs WHERE status='new' AND pinged=0 AND created_ts<?",
                      (now() - REMIND_AFTER,)).fetchall()
    for sub in rows:
        try:
            ch = bot.get_channel(sub["channel_id"])
            msg = await ch.fetch_message(sub["message_id"])
            await ping_role(msg, sub, f"⏰ Обращение **#{sub['num']}** без ответа уже больше суток!")
        except Exception:
            log.exception("remind_unanswered")
        db.execute("UPDATE subs SET pinged=1 WHERE id=?", (sub["id"],))
        db.commit()


async def cleanup_tickets():
    rows = db.execute("SELECT * FROM subs WHERE status IN ('approved','rejected') "
                      "AND ticket_channel_id IS NOT NULL AND closed_ts<?", (now() - CLEANUP_AFTER,)).fetchall()
    for sub in rows:
        ch = bot.get_channel(sub["ticket_channel_id"])
        if ch:
            try:
                await ch.delete()
            except discord.HTTPException:
                log.exception("Не удалось удалить тикет %s", sub["ticket_channel_id"])
        db.execute("UPDATE subs SET ticket_channel_id=NULL WHERE id=?", (sub["id"],))
        db.commit()


async def weekly_check():
    dt = datetime.datetime.now(datetime.timezone.utc)
    if (dt.weekday(), dt.hour) < (0, 9):  # не раньше понедельника 09:00 UTC
        return
    iso = dt.isocalendar()
    key = f"{iso.year}-{iso.week}"
    for guild in bot.guilds:
        cfg = gcfg(guild.id)
        if cfg.get("stats_channel_id") and cfg.get("last_weekly") != key:
            set_gcfg(guild.id, last_weekly=key)
            await post_weekly(guild)


async def daily_backup():
    path = store.backup_db()
    if path:
        log.info("Резервная копия базы: %s", path)


@tasks.loop(minutes=5)
async def background():
    for job in (remind_unanswered, remind_handlers, cleanup_tickets, weekly_check, daily_backup):
        try:
            await job()
        except Exception:
            log.exception("Фоновая задача %s", job.__name__)


@background.before_loop
async def _before():
    await bot.wait_until_ready()
