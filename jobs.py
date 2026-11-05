"""Фоновые задачи: напоминания, уборка тикетов, недельный отчёт, резервные копии."""
import os, datetime
import discord
from discord.ext import tasks

import store
from store import db, gcfg, set_gcfg, now
import errsink
from core import bot, log, ping_role
from policy import silence_action
from stats import post_weekly

REMIND_AFTER = 24 * 3600        # через сколько без ответа тегать вайтлистеров
CLEANUP_AFTER = 3600            # через сколько после решения удалять канал тикета


async def remind_handlers():
    """Тикет в работе тихий больше суток: напоминаем тому, кого ждём (взявшему или игроку),
    а если включено /settings auto_close_days, то закрываем обращение, когда игрок долго молчит.
    Сообщение бота тоже считается последним, поэтому повтор будет не чаще раза в сутки."""
    from tickets import finish
    rows = db.execute("SELECT * FROM subs WHERE status='work' AND ticket_channel_id IS NOT NULL "
                      "AND handler_id IS NOT NULL").fetchall()
    for sub in rows:
        ch = bot.get_channel(sub["ticket_channel_id"])
        if not ch:
            continue
        days = gcfg(sub["guild_id"]).get("auto_close_days", 0)
        last_any = (discord.utils.snowflake_time(ch.last_message_id).timestamp()
                    if ch.last_message_id else sub["created_ts"])
        action = silence_action(sub["last_human_id"], sub["author_id"], last_any, sub["last_human_ts"],
                                now(), REMIND_AFTER, days * 86400)
        try:
            if action == "close":
                await finish(sub["id"], "rejected", sub["handler_id"], f"Нет ответа от игрока {days} дн.")
            elif action == "ping_player":
                tail = f" Если ответа не будет, обращение закроют через {days} дн." if days else ""
                await ch.send(f"<@{sub['author_id']}>, по обращению **#{sub['num']}** ждём твой ответ.{tail}")
            elif action == "ping_handler":
                await ch.send(f"<@{sub['handler_id']}>, по обращению **#{sub['num']}** тихо больше суток. "
                              f"Ответь игроку или закрой обращение.")
        except discord.HTTPException:
            log.exception("remind_handlers")


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


MAX_BACKUP_UPLOAD = 8 * 1024 * 1024  # больше не отправляем, чтобы не упереться в лимит вложений


async def daily_backup():
    path = store.backup_db()
    if not path:
        return
    log.info("Резервная копия базы: %s", path)
    if os.path.getsize(path) > MAX_BACKUP_UPLOAD:
        log.warning("Копия слишком большая для отправки в Discord")
        return
    for guild in bot.guilds:  # копия вне хоста: в закрытый канал логов, если включено /settings backup_discord
        cfg = gcfg(guild.id)
        ch = bot.get_channel(cfg.get("log_channel_id", 0))
        if ch and cfg.get("backup_discord"):
            try:
                await ch.send("🗄️ Резервная копия базы", file=discord.File(path))
            except discord.HTTPException:
                log.warning("Не удалось отправить копию в канал логов", exc_info=True)


async def flush_errors():
    """Ошибки бота из bot.log дублируются в канал логов (выключается /settings error_log)."""
    texts = errsink.drain()
    if not texts:
        return
    for guild in bot.guilds:
        cfg = gcfg(guild.id)
        ch = bot.get_channel(cfg.get("log_channel_id", 0))
        if ch and cfg.get("error_log", True):
            for text in texts:
                try:
                    await ch.send(f"⚠️ Ошибка бота:\n```\n{text}\n```", allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning("Не удалось отправить ошибку в канал логов", exc_info=True)


@tasks.loop(minutes=5)
async def background():
    for job in (remind_unanswered, remind_handlers, cleanup_tickets, weekly_check, daily_backup, flush_errors):
        try:
            await job()
        except Exception:
            log.exception("Фоновая задача %s", job.__name__)


@background.before_loop
async def _before():
    await bot.wait_until_ready()
