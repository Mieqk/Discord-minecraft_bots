"""Общее для всех модулей: объект бота, права, логи, поиск пользователей и участников."""
import os, time, logging
from logging.handlers import RotatingFileHandler
import discord
from discord import app_commands
from discord.ext import commands

import store
import errsink
from store import gcfg
from kinds import KINDS

log = logging.getLogger("bot")


def setup_logging():
    folder = os.path.join(store.data_dir(), "logs")
    os.makedirs(folder, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(),
              RotatingFileHandler(os.path.join(folder, "bot.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    sink = errsink.SinkHandler()  # ошибки уходят ещё и в канал логов (jobs.flush_errors)
    sink.setFormatter(fmt)
    root.addHandler(sink)


# ====== ПРАВА ======
def is_team(member, cfg):
    """Админка: администратор сервера или роль персонала."""
    role = member.guild.get_role(cfg.get("staff_role_id", 0))
    return member.guild_permissions.administrator or (role is not None and role in member.roles)


def kind_role_id(cfg, kind):
    """Роль, которая обрабатывает этот раздел (для заявок есть запасной старый ключ)."""
    rid = cfg.get(KINDS[kind]["role_key"])
    if not rid and kind == "app":
        rid = cfg.get("whitelist_role_id")
    return rid


def can_handle(member, cfg, kind):
    """Обрабатывать обращения: админка или роль этого раздела."""
    if is_team(member, cfg):
        return True
    role = member.guild.get_role(kind_role_id(cfg, kind) or 0)
    return role is not None and role in member.roles


def handled_kinds(member, cfg):
    return [key for key in KINDS if can_handle(member, cfg, key)]


def team_only():
    async def pred(i: discord.Interaction):
        if i.guild and is_team(i.user, gcfg(i.guild.id)):
            return True
        raise app_commands.CheckFailure("Эта команда только для администрации.")
    return app_commands.check(pred)


def staff_only():
    """Администрация или любая из ролей-обработчиков."""
    async def pred(i: discord.Interaction):
        if i.guild and handled_kinds(i.user, gcfg(i.guild.id)):
            return True
        raise app_commands.CheckFailure("Эта команда только для персонала.")
    return app_commands.check(pred)


# ====== БОТ ======
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True  # нужен для текста переписки в логах (включить в Developer Portal)
        super().__init__(command_prefix="!", intents=intents,
                         max_messages=None,  # кэш сообщений не нужен
                         chunk_guilds_at_startup=False,  # не качать всех участников при старте (см. get_member)
                         allowed_mentions=discord.AllowedMentions(roles=False, everyone=False, users=True, replied_user=False))
        self.started_ts = time.time()

    async def setup_hook(self):
        from tickets import SubmissionView
        from forms import PanelView
        from quotes import Quotes
        from jobs import background
        self.add_view(PanelView(list(KINDS)))
        self.add_view(SubmissionView())
        await self.add_cog(Quotes(self))
        await self.tree.sync()
        background.start()


bot = Bot()


@bot.tree.error
async def on_app_error(i: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Только для администраторов сервера."
    elif isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "Нет доступа."
    else:
        log.error("Ошибка команды", exc_info=(type(error), error, error.__traceback__))
        msg = "Что-то пошло не так, попробуй ещё раз."
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


async def modal_error(i, error):
    """Ошибка в форме: пишем в лог (и в канал логов) и отвечаем игроку, чтобы не висело «думает»."""
    log.error("Ошибка формы", exc_info=(type(error), error, error.__traceback__))
    msg = "Что-то пошло не так, попробуй ещё раз."
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def get_user(uid):
    try:
        return bot.get_user(uid) or await bot.fetch_user(uid)
    except discord.HTTPException:
        return None


async def get_member(guild, uid):
    """Участник из кэша, а если его там нет (список участников мы не грузим целиком) - запросом к Discord."""
    member = guild.get_member(uid)
    if member:
        return member
    try:
        return await guild.fetch_member(uid)
    except discord.HTTPException:
        return None


# ====== ТЕГ РОЛИ ======
async def ping_role(msg, sub, text):
    """Ответ под карточкой, который тегает роль-обработчика этого раздела (и только её)."""
    cfg = gcfg(sub["guild_id"])
    rid = kind_role_id(cfg, sub["kind"]) or cfg.get("staff_role_id")
    if not rid:
        return
    try:
        await msg.reply(f"<@&{rid}> {text}", allowed_mentions=discord.AllowedMentions(everyone=False, users=False, replied_user=False,
                                                                      roles=[discord.Object(rid)]))
    except discord.HTTPException:
        log.exception("Не удалось пингануть роль")
