import os, io, json, time, asyncio, datetime, logging, platform
from logging.handlers import RotatingFileHandler
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import store
import mojang
from store import db, gcfg, set_gcfg, now, fmt_dur, make_nick, sub_by_id
from kinds import KINDS, KIND_CHOICES, PARA
from quotes import Quotes
from cards import (make_card, make_banner, make_leaderboard,
                   GOLD, ORANGE, GREEN, RED_LIGHT)

load_dotenv()
log = logging.getLogger("bot")

REMIND_AFTER = 24 * 3600        # через сколько без ответа тегать вайтлистеров
CLEANUP_AFTER = 3600            # через сколько после решения удалять канал тикета
NICK_FROM_USERNAME = True       # True: ник из юзернейма (carburadiator), False: из отображаемого имени
SPAM_LIMIT, SPAM_WINDOW = 5, 3600   # не больше 5 обращений в час от одного человека
REAPPLY_DAYS = 3                # через сколько дней можно подать заявку после отказа (/settings reapply_days)


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
        super().__init__(command_prefix="!", intents=intents,
                         max_messages=None,  # кэш сообщений не нужен
                         chunk_guilds_at_startup=False,  # не качать всех участников при старте (см. get_member)
                         allowed_mentions=discord.AllowedMentions(roles=False, everyone=False, users=True, replied_user=False))
        self.started_ts = time.time()

    async def setup_hook(self):
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


# ====== КАРТОЧКА ЗАЯВКИ ======
def status_info(sub):
    k = KINDS[sub["kind"]]
    return {"new": ("Новая", GOLD), "work": ("В обработке", ORANGE),
            "approved": (k["done"], GREEN), "rejected": ("Отклонена", RED_LIGHT)}[sub["status"]]


def discord_fields(uid, user):
    ts = int(discord.utils.snowflake_time(uid).timestamp())
    who = f"{user.display_name} (@{user.name})" if user else f"ID {uid}"
    return [("Discord", who, True), ("Аккаунт создан", f"<t:{ts}:D> (<t:{ts}:R>)", True)]


def mojang_field(values):
    mj = values.get("_mojang")
    if mj == "missing":
        return [("Ник в Mojang", "⚠️ не найден (возможно, без лицензии)", True)]
    if mj == "found":
        return [("Ник в Mojang", "✅ найден", True)]
    return []


def brief_fields(sub, user):
    """Коротко: ник в дискорде, дата создания аккаунта + пара ключевых полей."""
    values = json.loads(sub["fields"])
    out = discord_fields(sub["author_id"], user)
    for key, name in KINDS[sub["kind"]]["brief"]:
        if values.get(key):
            out.append((name, values[key][:200], True))
    return out + mojang_field(values)


def full_embed(sub, user):
    k, values = KINDS[sub["kind"]], json.loads(sub["fields"])
    e = discord.Embed(title=f"{k['emoji']} {k['label']} #{sub['num']}", description=f"**{sub['topic']}**",
                      color=discord.Color.from_rgb(*GOLD))
    for name, val, inline in discord_fields(sub["author_id"], user):
        e.add_field(name=name, value=val, inline=inline)
    for key, label, *_ in k["fields"]:
        if values.get(key):
            e.add_field(name=label, value=values[key][:1024], inline=False)
    for name, val, inline in mojang_field(values):
        e.add_field(name=name, value=val, inline=inline)
    return e


def details_embed(sub, user):
    """Что видит персонал по кнопке «Подробнее» и в /ticket: до взятия в работу только кратко."""
    if sub["status"] != "new":
        return full_embed(sub, user)
    k = KINDS[sub["kind"]]
    e = discord.Embed(title=f"{k['emoji']} {k['label']} #{sub['num']}", description=f"**{sub['topic']}**\n\n"
                      "Полные ответы откроются, когда возьмёшь заявку в работу.",
                      color=discord.Color.from_rgb(*GOLD))
    for n, v, inline in brief_fields(sub, user):
        e.add_field(name=n, value=v, inline=inline)
    return e


async def render(sub):
    k = KINDS[sub["kind"]]
    text, color = status_info(sub)
    user = await get_user(sub["author_id"])
    name = user.display_name if user else f"ID {sub['author_id']}"
    avatar = None
    if user:
        try:
            avatar = await user.display_avatar.replace(size=256, format="png").read()
        except discord.HTTPException:
            pass
    label = k["label"] + (" • срочно" if sub["priority"] else "")
    buf = await asyncio.to_thread(make_card, label, sub["topic"], text, color, name, avatar, f"#{sub['num']}")
    file = discord.File(buf, "card.png")

    e = discord.Embed(color=discord.Color.from_rgb(*color))
    for n, v, inline in brief_fields(sub, user):
        e.add_field(name=n, value=v, inline=inline)
    e.add_field(name="Статус", value=text)
    if sub["priority"]:
        e.add_field(name="Приоритет", value="🔥 Срочно")
    if sub["handler_id"]:
        e.add_field(name="Обрабатывает" if sub["status"] == "work" else "Обработал", value=f"<@{sub['handler_id']}>")
    if sub["status"] == "work" and sub["ticket_channel_id"]:
        e.add_field(name="Тикет", value=f"<#{sub['ticket_channel_id']}>")
    if sub["closed_ts"]:
        e.add_field(name="Обработана за", value=fmt_dur(sub["closed_ts"] - sub["created_ts"]))
    if sub["reason"]:
        e.add_field(name="Причина", value=sub["reason"][:1024], inline=False)
    e.set_image(url="attachment://card.png")
    e.set_footer(text="Полные ответы откроются после «Взять в работу»" if sub["status"] == "new"
                 else "Нажми «Подробнее», чтобы прочитать полностью")
    return e, file, SubmissionView(sub["kind"], sub["status"], sub["priority"])


async def refresh_message(sid):
    sub = sub_by_id(sid)
    try:
        ch = bot.get_channel(sub["channel_id"]) or await bot.fetch_channel(sub["channel_id"])
        msg = await ch.fetch_message(sub["message_id"])
        e, f, v = await render(sub)
        await msg.edit(embed=e, attachments=[f], view=v)
    except discord.HTTPException:
        log.exception("Не удалось обновить карточку #%s", sub["num"])
    if sub["ticket_channel_id"] and sub["ticket_msg_id"]:
        try:
            tch = bot.get_channel(sub["ticket_channel_id"])
            if tch:
                tm = await tch.fetch_message(sub["ticket_msg_id"])
                await tm.edit(view=SubmissionView(sub["kind"], sub["status"], sub["priority"]))
        except discord.HTTPException:
            pass


async def notify_author(sub, text, color):
    try:
        user = await get_user(sub["author_id"])
        k = KINDS[sub["kind"]]
        await user.send(embed=discord.Embed(
            title=f"{k['emoji']} {k['label']} #{sub['num']}", description=f"**{sub['topic']}**\n\n{text}",
            color=discord.Color.from_rgb(*color)))
    except Exception:
        pass


# ====== КАНАЛ ТИКЕТА ======
async def open_ticket_channel(guild, sub, handler):
    cfg = gcfg(guild.id)
    P = discord.PermissionOverwrite
    base = dict(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
    overwrites = {
        guild.default_role: P(view_channel=False),
        guild.me: P(**base, embed_links=True, manage_channels=True),
        handler: P(**base),
    }
    # Тикет виден только взявшему, автору и администраторам сервера. Роли персонала его не видят,
    # пока их не отметят в тикете (см. on_message).
    author = await get_member(guild, sub["author_id"])
    if author:
        overwrites[author] = P(**base)
    category = guild.get_channel(cfg.get("ticket_category_id", 0))
    return await guild.create_text_channel(
        name=f"{KINDS[sub['kind']]['prefix']}-{sub['num']:04d}", category=category, overwrites=overwrites,
        topic=f"{KINDS[sub['kind']]['label']} #{sub['num']} • {sub['topic']}")


async def set_nickname(sub):
    """Меняет ник игрока на сервере после принятия заявки, возвращает текст-примечание."""
    guild = bot.get_guild(sub["guild_id"])
    member = await get_member(guild, sub["author_id"]) if guild else None
    if not member:
        return "\nИгрок не на сервере, ник не менялся."
    mc = json.loads(sub["fields"]).get("nick", "")
    if not mc.replace("@", "").strip():
        return ""
    base = member.name if NICK_FROM_USERNAME else (member.global_name or member.name)
    new = make_nick(base, mc)
    try:
        await member.edit(nick=new, reason=f"Заявка #{sub['num']} принята")
    except discord.Forbidden:
        if member.id == guild.owner_id:
            why = "Discord не позволяет менять ник владельца сервера, смени его вручную."
        elif guild.me.top_role <= member.top_role:
            why = "роль бота ниже роли игрока или на одном уровне, подними роль бота выше в настройках ролей."
        elif not guild.me.guild_permissions.manage_nicknames:
            why = "у бота нет права «Управлять никнеймами»."
        else:
            why = "Discord отклонил смену ника."
        return f"\nНе смог сменить ник на `{new}`: {why}"
    except discord.HTTPException:
        return "\nНе удалось сменить ник."
    return f"\nНик на сервере: `{new}`"


async def lock_ticket(sub):
    """Сразу закрываем тикет: писать туда больше никто не может."""
    ch = bot.get_channel(sub["ticket_channel_id"]) if sub["ticket_channel_id"] else None
    if not ch:
        return
    for target, ow in list(ch.overwrites.items()):
        if target == ch.guild.me or target == ch.guild.default_role:
            continue
        ow.send_messages = False
        ow.add_reactions = False
        try:
            await ch.set_permissions(target, overwrite=ow)
        except discord.HTTPException:
            log.exception("Не удалось закрыть тикет %s", ch.id)


def transcript_name(sub):
    return f"{KINDS[sub['kind']]['prefix']}-{sub['num']:04d}.txt"


async def collect_transcript(sub):
    """Текст переписки тикета (без сообщений бота) или None, если канала нет."""
    ch = bot.get_channel(sub["ticket_channel_id"]) if sub["ticket_channel_id"] else None
    if not ch:
        return None
    lines = []
    async for m in ch.history(limit=None, oldest_first=True):
        if m.author.id == bot.user.id:
            continue
        files = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M} UTC] {m.author.display_name}: {m.clean_content} {files}".strip())
    return "\n".join(lines) or "(сообщений нет)"


def transcript_file(sub, text):
    return discord.File(io.BytesIO(text.encode("utf-8")), transcript_name(sub)) if text is not None else None


async def send_log(sub, nick_note, dur, transcript):
    """Лог закрытого обращения: заявка + переписка одним сообщением в канал логов."""
    cfg = gcfg(sub["guild_id"])
    ch = bot.get_channel(cfg.get("log_channel_id", 0)) or bot.get_channel(sub["channel_id"])
    if not ch:
        return
    k, ok = KINDS[sub["kind"]], sub["status"] == "approved"
    user = await get_user(sub["author_id"])
    e = full_embed(sub, user)
    e.title = f"{k['emoji']} {k['label']} #{sub['num']} - {k['done'].lower() if ok else 'отклонена'}"
    e.color = discord.Color.from_rgb(*(GREEN if ok else RED_LIGHT))
    e.add_field(name="Обработал", value=f"<@{sub['handler_id']}>")
    e.add_field(name="Время обработки", value=dur)
    if sub["reason"]:
        e.add_field(name="Причина", value=sub["reason"][:1024], inline=False)
    if nick_note.strip():
        e.add_field(name="Ник", value=nick_note.strip(), inline=False)
    file = transcript_file(sub, transcript)
    await ch.send(embed=e, file=file) if file else await ch.send(embed=e)


async def finish(sid, status, handler_id, reason=None):
    """Закрывает обращение. Возвращает короткий текст, который увидит только нажавший модератор."""
    db.execute("UPDATE subs SET status=?, handler_id=?, reason=?, closed_ts=? WHERE id=?",
               (status, handler_id, reason, now(), sid))
    db.commit()
    await refresh_message(sid)
    sub = sub_by_id(sid)
    k, ok = KINDS[sub["kind"]], status == "approved"
    dur = fmt_dur(sub["closed_ts"] - sub["created_ts"])
    nick_note = await set_nickname(sub) if ok and sub["kind"] == "app" else ""
    try:
        await lock_ticket(sub)  # сначала закрыть, потом собирать переписку
    except Exception:
        log.exception("lock_ticket")
    try:
        transcript = await collect_transcript(sub)
        if transcript is not None:
            store.save_transcript(sid, transcript)  # копия в базе: /ticket покажет её, даже когда канал удалён
        await send_log(sub, nick_note, dur, transcript)
    except Exception:
        log.exception("send_log")
    if ok:
        text, color = f"Статус: **{k['done']}** ✅", GREEN
    else:
        text, color = "Статус: **отклонено** ❌" + (f"\nПричина: {reason}" if reason else ""), RED_LIGHT
    await notify_author(sub, text, color)
    return (f"{'✅' if ok else '❌'} {k['label']} #{sub['num']}: {k['done'].lower() if ok else 'отклонена'}. "
            f"Время обработки: {dur}.{nick_note}")


# ====== КНОПКИ ПОД ЗАЯВКОЙ ======
def find_sub(i):
    """Заявку находим по сообщению-карточке или по каналу тикета."""
    return (db.execute("SELECT * FROM subs WHERE message_id=?", (i.message.id,)).fetchone()
            or db.execute("SELECT * FROM subs WHERE ticket_channel_id=?", (i.channel.id,)).fetchone())


def check_handle(i, sub):
    if not can_handle(i.user, gcfg(i.guild.id), sub["kind"]):
        return "Нет доступа."
    if sub["status"] in ("approved", "rejected"):
        return "Заявка уже закрыта."
    return None


def check_decide(i, sub):
    err = check_handle(i, sub)
    if err:
        return err
    if sub["status"] == "new":
        return "Сначала нажми «Взять в работу»."
    if sub["handler_id"] != i.user.id and not i.user.guild_permissions.administrator:
        return f"Этим уже занимается <@{sub['handler_id']}>."
    return None


class ReasonModal(discord.ui.Modal, title="Отклонить"):
    reason = discord.ui.TextInput(label="Причина", style=PARA, required=False, max_length=500)

    def __init__(self, sid):
        super().__init__()
        self.sid = sid

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        summary = await finish(self.sid, "rejected", i.user.id, self.reason.value or None)
        await i.followup.send(summary, ephemeral=True)


class SubmissionView(discord.ui.View):
    def __init__(self, kind=None, status="new", priority=0):
        super().__init__(timeout=None)
        if kind:
            self.ok.label = KINDS[kind]["ok_btn"]
        if priority:
            self.prio.label = "Снять срочность"
        if status in ("approved", "rejected"):
            self.take.disabled = self.ok.disabled = self.no.disabled = self.prio.disabled = True
        elif status == "work":
            self.take.disabled = True

    @discord.ui.button(label="Подробнее", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="sub:details")
    async def details(self, i: discord.Interaction, b: discord.ui.Button):
        sub = find_sub(i)
        if not sub:
            return await i.response.send_message("Заявка не найдена в базе.", ephemeral=True)
        if not can_handle(i.user, gcfg(i.guild.id), sub["kind"]):
            return await i.response.send_message("Нет доступа.", ephemeral=True)
        user = await get_user(sub["author_id"])
        await i.response.send_message(embed=details_embed(sub, user), ephemeral=True)

    @discord.ui.button(label="Взять в работу", emoji="🛠️", style=discord.ButtonStyle.primary, custom_id="sub:take")
    async def take(self, i: discord.Interaction, b: discord.ui.Button):
        sub = find_sub(i)
        if not sub:
            return await i.response.send_message("Заявка не найдена.", ephemeral=True)
        err = check_handle(i, sub)
        if err:
            return await i.response.send_message(err, ephemeral=True)
        if sub["status"] == "work":
            return await i.response.send_message(f"Этим уже занимается <@{sub['handler_id']}>.", ephemeral=True)
        # атомарно занимаем, чтобы двое не взяли одновременно
        cur = db.execute("UPDATE subs SET status='work', handler_id=? WHERE id=? AND status='new'",
                         (i.user.id, sub["id"]))
        db.commit()
        if cur.rowcount == 0:
            return await i.response.send_message("Эту заявку только что взяли.", ephemeral=True)
        await i.response.defer()
        try:
            channel = await open_ticket_channel(i.guild, sub, i.user)
        except discord.HTTPException:
            log.exception("Не удалось создать канал тикета")
            db.execute("UPDATE subs SET status='new', handler_id=NULL WHERE id=?", (sub["id"],))
            db.commit()
            return await i.followup.send("Не смог создать канал тикета. Дай боту право «Управлять каналами» "
                                         "и доступ к категории тикетов.", ephemeral=True)
        db.execute("UPDATE subs SET ticket_channel_id=? WHERE id=?", (channel.id, sub["id"]))
        db.commit()
        sub = sub_by_id(sub["id"])
        author = await get_user(sub["author_id"])
        msg = await channel.send(
            content=f"<@{sub['author_id']}>, {KINDS[sub['kind']]['acc']} взял в работу {i.user.mention}",
            embed=full_embed(sub, author), view=SubmissionView(sub["kind"], "work", sub["priority"]))
        db.execute("UPDATE subs SET ticket_msg_id=? WHERE id=?", (msg.id, sub["id"]))
        db.commit()
        await refresh_message(sub["id"])
        link = discord.ui.View()
        link.add_item(discord.ui.Button(label="Открыть тикет", emoji="📂", url=channel.jump_url))
        await i.followup.send(f"Ты взял в работу: {channel.mention}", view=link, ephemeral=True)

    @discord.ui.button(label="Принять", emoji="✅", style=discord.ButtonStyle.success, custom_id="sub:ok")
    async def ok(self, i: discord.Interaction, b: discord.ui.Button):
        sub = find_sub(i)
        err = check_decide(i, sub) if sub else "Заявка не найдена."
        if err:
            return await i.response.send_message(err, ephemeral=True)
        await i.response.defer()
        summary = await finish(sub["id"], "approved", i.user.id)
        await i.followup.send(summary, ephemeral=True)

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger, custom_id="sub:no")
    async def no(self, i: discord.Interaction, b: discord.ui.Button):
        sub = find_sub(i)
        err = check_decide(i, sub) if sub else "Заявка не найдена."
        if err:
            return await i.response.send_message(err, ephemeral=True)
        await i.response.send_modal(ReasonModal(sub["id"]))

    @discord.ui.button(label="Срочно", emoji="🔥", style=discord.ButtonStyle.secondary, custom_id="sub:prio")
    async def prio(self, i: discord.Interaction, b: discord.ui.Button):
        sub = find_sub(i)
        if not sub:
            return await i.response.send_message("Заявка не найдена.", ephemeral=True)
        err = check_handle(i, sub)
        if err:
            return await i.response.send_message(err, ephemeral=True)
        db.execute("UPDATE subs SET priority=? WHERE id=?", (0 if sub["priority"] else 1, sub["id"]))
        db.commit()
        await i.response.defer()
        await refresh_message(sub["id"])


# ====== ФОРМА ДЛЯ ИГРОКА ======
async def validate_submission(i, kind, values, cfg):
    """Возвращает текст ошибки или None, если обращение можно создавать."""
    k = KINDS[kind]
    if kind == "app" and not values["age"].isdigit():
        return "Возраст нужно указать числом."
    if store.active_count(i.guild.id, i.user.id, kind) >= k["max_active"]:
        return "У тебя уже есть необработанные обращения этого типа."
    if store.recent_count(i.guild.id, i.user.id, now() - SPAM_WINDOW) >= SPAM_LIMIT:
        return "Слишком много обращений за последний час. Попробуй позже."
    if kind == "app" and not is_team(i.user, cfg):
        days = cfg.get("reapply_days", REAPPLY_DAYS)
        last = store.last_rejected_ts(i.guild.id, i.user.id, kind)
        if days and last and now() < last + days * 86400:
            return f"Предыдущую заявку отклонили. Новую можно подать через {fmt_dur(last + days * 86400 - now())}."
    if kind == "app" and cfg.get("mc_check", True):
        nick = values["nick"].strip()
        if not store.mc_nick_valid(nick):
            return "Ник Minecraft: 3-16 символов, латиница, цифры и «_»."
        if store.nick_taken(i.guild.id, nick, i.user.id):
            return "Этот ник уже указан в другой заявке."
    return None


async def mojang_note(values, cfg):
    """Мягкая проверка: 'found' / 'missing' / None (не проверяли или Mojang недоступен). Заявку не блокирует:
    на сервере бывают игроки без лицензии, решает вайтлистер."""
    if not cfg.get("mc_check", True):
        return None
    return {True: "found", False: "missing"}.get(await mojang.exists(values["nick"].strip()))


class FormModal(discord.ui.Modal):
    def __init__(self, kind):
        super().__init__(title=KINDS[kind]["label"][:45])
        self.kind, self.inputs = kind, {}
        for key, label, style, required, maxlen in KINDS[kind]["fields"]:
            ti = discord.ui.TextInput(label=label, style=style, required=required, max_length=maxlen)
            self.inputs[key] = ti
            self.add_item(ti)

    async def on_submit(self, i: discord.Interaction):
        k, cfg = KINDS[self.kind], gcfg(i.guild.id)
        await i.response.defer(ephemeral=True)  # проверка ника в Mojang может занять пару секунд
        channel = i.guild.get_channel(cfg.get(f"{self.kind}_channel_id", 0))
        if not channel:
            return await i.followup.send("Раздел не настроен (/setup).", ephemeral=True)
        values = {key: v.value.strip() for key, v in self.inputs.items()}
        err = await validate_submission(i, self.kind, values, cfg)
        if err:
            return await i.followup.send(err, ephemeral=True)

        if self.kind == "app":
            note = await mojang_note(values, cfg)
            if note:
                values["_mojang"] = note
        topic = k["topic"](values, i.user)[:200]
        sid = store.create_sub(i.guild.id, self.kind, i.user.id, topic, values)
        sub = sub_by_id(sid)
        e, f, v = await render(sub)
        msg = await channel.send(embed=e, file=f, view=v)
        db.execute("UPDATE subs SET channel_id=?, message_id=? WHERE id=?", (channel.id, msg.id, sid))
        db.commit()
        await ping_role(msg, sub, f"🔔 Новое обращение **#{sub['num']}**: {k['label'].lower()}, нужен обработчик.")
        await i.followup.send(f"✅ Отправлено! Номер обращения: **#{sub['num']}**. Когда его возьмут в работу, "
                              f"для тебя создадут отдельный канал.", ephemeral=True)


# ====== ПАНЕЛЬ ======
class PanelButton(discord.ui.Button):
    def __init__(self, kind):
        k = KINDS[kind]
        super().__init__(label=k["btn"], emoji=k["emoji"], style=k["style"], custom_id=f"panel:{kind}")
        self.kind = kind

    async def callback(self, i: discord.Interaction):
        await i.response.send_modal(FormModal(self.kind))


class PanelView(discord.ui.View):
    def __init__(self, kinds):
        super().__init__(timeout=None)
        for k in kinds:
            self.add_item(PanelButton(k))


# ====== СТАТИСТИКА ======
_SAME = object()


async def build_stats(guild, title, since=0, kind=None, board_kind=_SAME):
    bk = kind if board_kind is _SAME else board_kind
    board = []
    for r in store.leaderboard(guild.id, bk, since):
        m = await get_member(guild, r["handler_id"])
        board.append((m.display_name if m else f"ID {r['handler_id']}", r["total"], r["ok"] or 0, fmt_dur(r["avg"])))
    t = store.totals(guild.id, kind, since)
    summary = f"Поступило {t['created']}  •  Обработано {t['handled']}  •  Ср. время {fmt_dur(t['avg'])}"
    img = await asyncio.to_thread(make_leaderboard, title, board, summary)
    e = discord.Embed(color=discord.Color.from_rgb(*GOLD))
    e.add_field(name="Поступило", value=str(t["created"]))
    e.add_field(name="Обработано", value=f"{t['handled']} (принято {t['approved']})")
    e.add_field(name="Ждут ответа", value=str(t["open"]))
    e.add_field(name="Среднее время обработки", value=fmt_dur(t["avg"]))
    e.set_image(url="attachment://stats.png")
    return e, discord.File(img, "stats.png")


async def post_weekly(guild):
    ch = guild.get_channel(gcfg(guild.id).get("stats_channel_id", 0))
    if not ch:
        return
    since = now() - 7 * 86400
    apps = store.leaderboard(guild.id, "app", since)
    e, f = await build_stats(guild, "Вайтлистеры недели", since, kind=None, board_kind="app")
    e.title = "📊 Итоги недели"
    e.description = (f"🏆 **Лучший вайтлистер недели:** <@{apps[0]['handler_id']}> - обработано заявок: {apps[0]['total']}"
                     if apps else "На этой неделе заявки не обрабатывались.")
    await ch.send(embed=e, file=f)


# ====== НАПОМИНАНИЯ ======
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


# ====== КОМАНДЫ ======
@bot.tree.command(name="setup", description="Настроить каналы (только администраторы сервера)")
@app_commands.describe(app_channel="Канал для заявок в проект (скрытый, для персонала)",
                       report_channel="Канал для жалоб", bug_channel="Канал для багов",
                       ticket_category="Категория, где создаются каналы тикетов",
                       stats_channel="Публичный канал статистики",
                       log_channel="Скрытый канал логов: сюда приходят закрытые обращения с перепиской")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def setup(i: discord.Interaction, app_channel: discord.TextChannel, report_channel: discord.TextChannel,
                bug_channel: discord.TextChannel, ticket_category: discord.CategoryChannel,
                stats_channel: discord.TextChannel, log_channel: discord.TextChannel):
    dt = datetime.datetime.now(datetime.timezone.utc).isocalendar()
    set_gcfg(i.guild.id, app_channel_id=app_channel.id, report_channel_id=report_channel.id,
             bug_channel_id=bug_channel.id, ticket_category_id=ticket_category.id,
             stats_channel_id=stats_channel.id, log_channel_id=log_channel.id, last_weekly=f"{dt.year}-{dt.week}")
    await i.response.send_message("✅ Каналы сохранены. Дальше: /setup_roles.", ephemeral=True)


@bot.tree.command(name="setup_roles", description="Настроить роли (только администраторы сервера)")
@app_commands.describe(staff_role="Администрация: все команды и вся статистика",
                       app_role="Вайтлистеры: обрабатывают заявки в проект",
                       report_role="Модераторы: обрабатывают жалобы",
                       bug_role="Тестеры или разработчики: обрабатывают баги")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def setup_roles(i: discord.Interaction, staff_role: discord.Role, app_role: discord.Role,
                      report_role: discord.Role, bug_role: discord.Role):
    set_gcfg(i.guild.id, staff_role_id=staff_role.id, app_role_id=app_role.id,
             report_role_id=report_role.id, bug_role_id=bug_role.id)
    await i.response.send_message("✅ Роли сохранены. Теперь /panel_app, /panel_report и /panel_bug "
                                  "в нужных каналах.", ephemeral=True)


@bot.tree.command(name="settings", description="Дополнительные настройки (только администраторы сервера)")
@app_commands.describe(mc_check="Проверять ник Minecraft в заявках (формат, дубли; пометка, если нет у Mojang)",
                       reapply_days="Через сколько дней можно подать заявку после отказа (0 - без ограничения)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def settings(i: discord.Interaction, mc_check: bool | None = None,
                   reapply_days: app_commands.Range[int, 0, 365] | None = None):
    if mc_check is not None:
        set_gcfg(i.guild.id, mc_check=mc_check)
    if reapply_days is not None:
        set_gcfg(i.guild.id, reapply_days=reapply_days)
    cfg = gcfg(i.guild.id)
    await i.response.send_message(
        f"Проверка ника Minecraft: **{'вкл' if cfg.get('mc_check', True) else 'выкл'}**\n"
        f"Повторная заявка после отказа: **{cfg.get('reapply_days', REAPPLY_DAYS)} дн.**", ephemeral=True)


async def send_panel(i: discord.Interaction, kind: str):
    await i.response.defer(ephemeral=True)
    k = KINDS[kind]
    banner = await asyncio.to_thread(make_banner, i.guild.name, k["section"])
    e = discord.Embed(title=f"{k['emoji']} {k['section']}", description=k["desc"],
                      color=discord.Color.from_rgb(*GOLD))
    e.set_image(url="attachment://panel.png")
    await i.channel.send(embed=e, file=discord.File(banner, "panel.png"), view=PanelView([kind]))
    await i.followup.send("Панель отправлена.", ephemeral=True)


@bot.tree.command(name="panel_app", description="Панель заявок в проект (отправить в этот канал)")
@app_commands.guild_only()
@team_only()
async def panel_app(i: discord.Interaction):
    await send_panel(i, "app")


@bot.tree.command(name="panel_report", description="Панель жалоб (отправить в этот канал)")
@app_commands.guild_only()
@team_only()
async def panel_report(i: discord.Interaction):
    await send_panel(i, "report")


@bot.tree.command(name="panel_bug", description="Панель багов (отправить в этот канал)")
@app_commands.guild_only()
@team_only()
async def panel_bug(i: discord.Interaction):
    await send_panel(i, "bug")


@bot.tree.command(name="status", description="Статистика проекта (для всех)")
@app_commands.guild_only()
async def status(i: discord.Interaction):
    cfg = gcfg(i.guild.id)
    ch_id = cfg.get("stats_channel_id")
    if ch_id and i.channel.id != ch_id and not is_team(i.user, cfg):
        return await i.response.send_message(f"Статистику можно смотреть в <#{ch_id}>.", ephemeral=True)
    await i.response.defer()
    e, f = await build_stats(i.guild, "Статистика за всё время")
    await i.followup.send(embed=e, file=f)


@bot.tree.command(name="stats", description="Топ сотрудников за всё время (админка)")
@app_commands.describe(kind="Раздел (по умолчанию - все вместе)")
@app_commands.choices(kind=KIND_CHOICES)
@app_commands.guild_only()
@team_only()
async def stats(i: discord.Interaction, kind: app_commands.Choice[str] | None = None):
    await i.response.defer(ephemeral=True)
    e, f = await build_stats(i.guild, "Топ: " + (kind.name if kind else "все обращения"),
                             kind=kind.value if kind else None)
    await i.followup.send(embed=e, file=f)


@bot.tree.command(name="stats_user", description="Статистика одного сотрудника за всё время (админка)")
@app_commands.describe(member="Сотрудник")
@app_commands.guild_only()
@team_only()
async def stats_user(i: discord.Interaction, member: discord.Member):
    rows = store.handler_rows(i.guild.id, member.id)
    done = [r for r in rows if r["status"] in ("approved", "rejected")]
    durs = [r["dur"] for r in done if r["dur"] is not None]
    e = discord.Embed(title=f"Статистика: {member.display_name}", color=discord.Color.from_rgb(*GOLD))
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Обработано", value=str(len(done)))
    e.add_field(name="Принято", value=str(sum(r["status"] == "approved" for r in done)))
    e.add_field(name="Отклонено", value=str(sum(r["status"] == "rejected" for r in done)))
    e.add_field(name="Сейчас в работе", value=str(sum(r["status"] == "work" for r in rows)))
    e.add_field(name="Среднее время", value=fmt_dur(sum(durs) / len(durs) if durs else None))
    e.add_field(name="Самая быстрая", value=fmt_dur(min(durs) if durs else None))
    e.add_field(name="По разделам", inline=False, value="\n".join(
        f"{k['emoji']} {k['section']}: {sum(r['kind'] == key for r in done)}" for key, k in KINDS.items()))
    await i.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="weekly_post", description="Выложить недельный отчёт прямо сейчас (админка)")
@app_commands.guild_only()
@team_only()
async def weekly_post(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    await post_weekly(i.guild)
    await i.followup.send("Отчёт отправлен в канал статистики.", ephemeral=True)


@bot.tree.command(name="queue", description="Открытые обращения: срочные первыми (персонал)")
@app_commands.describe(kind="Раздел (по умолчанию - все, которые ты обрабатываешь)", urgent_only="Только срочные")
@app_commands.choices(kind=KIND_CHOICES)
@app_commands.guild_only()
@staff_only()
async def queue(i: discord.Interaction, kind: app_commands.Choice[str] | None = None, urgent_only: bool = False):
    allowed = handled_kinds(i.user, gcfg(i.guild.id))
    kinds = [kind.value] if kind and kind.value in allowed else allowed
    rows = store.open_queue(i.guild.id, kinds, urgent_only)
    if not rows:
        return await i.response.send_message("Открытых обращений нет. 🎉", ephemeral=True)
    lines = []
    for r in rows:
        k = KINDS[r["kind"]]
        where = (f"[карточка](https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']})"
                 if r["message_id"] else "")
        who = f"<@{r['handler_id']}>" if r["handler_id"] else "никто"
        lines.append(f"{'🔥' if r['priority'] else '▫️'} {k['emoji']} **#{r['num']}** {r['topic'][:40]} "
                     f"- {'в работе' if r['status'] == 'work' else 'новая'} ({who}), "
                     f"ждёт {fmt_dur(now() - r['created_ts'])} {where}")
    await i.response.send_message(embed=discord.Embed(title="Открытые обращения", description="\n".join(lines),
                                                      color=discord.Color.from_rgb(*GOLD)), ephemeral=True)


@bot.tree.command(name="ticket", description="Найти обращение по номеру: данные и переписка (персонал)")
@app_commands.describe(kind="Раздел", number="Номер обращения (из заголовка карточки)")
@app_commands.choices(kind=KIND_CHOICES)
@app_commands.guild_only()
@staff_only()
async def ticket(i: discord.Interaction, kind: app_commands.Choice[str], number: int):
    if not can_handle(i.user, gcfg(i.guild.id), kind.value):
        return await i.response.send_message("Нет доступа к этому разделу.", ephemeral=True)
    sub = store.sub_by_num(i.guild.id, kind.value, number)
    if not sub:
        return await i.response.send_message("Такого обращения нет.", ephemeral=True)
    user = await get_user(sub["author_id"])
    e = details_embed(sub, user)
    text, _ = status_info(sub)
    e.add_field(name="Статус", value=text)
    if sub["handler_id"]:
        e.add_field(name="Обработал" if sub["closed_ts"] else "Обрабатывает", value=f"<@{sub['handler_id']}>")
    e.add_field(name="Создано", value=f"<t:{sub['created_ts']}:f>")
    if sub["closed_ts"]:
        e.add_field(name="Время обработки", value=fmt_dur(sub["closed_ts"] - sub["created_ts"]))
    if sub["reason"]:
        e.add_field(name="Причина", value=sub["reason"][:1024], inline=False)
    file = transcript_file(sub, sub["transcript"]) if sub["transcript"] is not None else None
    if file:
        await i.response.send_message(embed=e, file=file, ephemeral=True)
    else:
        await i.response.send_message(embed=e, ephemeral=True)


def roles_with_access(channel):
    """Роли (кроме @everyone и ботов), которые видят канал, и почему: [(роль, причина)]."""
    out = []
    for role in channel.guild.roles:
        if role.is_default() or role.managed:
            continue
        perms = channel.permissions_for(role)
        if perms.view_channel:
            out.append((role, "право «Администратор»" if perms.administrator else "право на просмотр каналов"))
    return out


@bot.tree.command(name="access", description="Кто видит этот канал: роли и причина (админка, вызывать в тикете)")
@app_commands.guild_only()
@team_only()
async def access(i: discord.Interaction):
    rows = roles_with_access(i.channel)
    lines = [f"{r.mention} - {why}" for r, why in rows] or ["Ни одна роль не видит этот канал."]
    e = discord.Embed(title="Кто видит этот канал", description="\n".join(lines),
                      color=discord.Color.from_rgb(*GOLD))
    e.set_footer(text="Роли с правом «Администратор» видят все каналы, Discord этого не скрывает. "
                      "Чтобы канал был скрыт от роли, уберите у неё это право.")
    await i.response.send_message(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


@bot.tree.command(name="health", description="Состояние бота: аптайм, память, база, копии (админка)")
@app_commands.guild_only()
@team_only()
async def health(i: discord.Interaction):
    up = fmt_dur(time.time() - bot.started_ts)
    rss = rss_mb()
    counts = store.open_counts(i.guild.id)
    last = store.backup_files()
    e = discord.Embed(title="🩺 Состояние бота", color=discord.Color.from_rgb(*GOLD))
    e.add_field(name="Аптайм", value=up)
    e.add_field(name="Пинг", value=f"{bot.latency * 1000:.0f} мс")
    e.add_field(name="Память", value=f"{rss:.0f} МБ" if rss else "н/д")
    e.add_field(name="Открытых обращений", value=f"новых {counts['new']}, в работе {counts['work']}")
    e.add_field(name="База", value=f"{store.db_size() / 1024:.0f} КБ")
    e.add_field(name="Последняя копия", value=last[-1][5:-3] if last else "нет")
    e.set_footer(text=f"Python {platform.python_version()} • discord.py {discord.__version__}")
    await i.response.send_message(embed=e, ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    """Если сотрудник в тикете отметил (@) человека, тикет открывается и для него."""
    if message.author.bot or not message.guild or not (message.mentions or message.role_mentions):
        return
    sub = db.execute("SELECT * FROM subs WHERE ticket_channel_id=? AND status='work'",
                     (message.channel.id,)).fetchone()
    if not sub:
        return
    if message.author.id != sub["handler_id"] and not is_team(message.author, gcfg(message.guild.id)):
        return
    added = []
    for target in [*message.mentions, *message.role_mentions]:
        if getattr(target, "bot", False) or target == message.guild.default_role:
            continue
        if message.channel.overwrites_for(target).view_channel:
            continue
        await message.channel.set_permissions(target, view_channel=True, send_messages=True,
                                              attach_files=True, read_message_history=True)
        added.append(target.mention)
    if added:
        await message.channel.send("👋 Доступ к тикету выдан: " + ", ".join(added))


@bot.event
async def on_ready():
    log.info("Вошёл как %s", bot.user)


def main():
    setup_logging()
    store.init(legacy_dir=".")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не найден DISCORD_TOKEN. Добавь его в переменные окружения хоста "
                         "или в файл .env рядом с bot.py (строка DISCORD_TOKEN=токен).")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
