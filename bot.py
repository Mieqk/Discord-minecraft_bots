import os, io, json, time, asyncio, sqlite3, datetime, traceback
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from cards import (make_card, make_banner, make_leaderboard, make_quote,
                   GOLD, ORANGE, GREEN, RED_LIGHT)

load_dotenv()
CONFIG_FILE = "config.json"
REMIND_AFTER = 24 * 3600        # через сколько без ответа тегать вайтлистеров
CLEANUP_AFTER = 3600            # через сколько после решения удалять канал тикета
NICK_FROM_USERNAME = True       # True: ник из юзернейма (carburadiator), False: из отображаемого имени
TEXT, PARA = discord.TextStyle.short, discord.TextStyle.paragraph

# ====== ВИДЫ ОБРАЩЕНИЙ ======
# fields: (ключ, вопрос в форме, стиль, обязательное, макс. длина)
# brief: что показывать КОРОТКО до взятия в работу: (ключ, подпись)
KINDS = {
    "app": {
        "label": "Заявка в проект", "emoji": "📝", "btn": "Подать заявку", "style": discord.ButtonStyle.success,
        "desc": "Хочешь попасть на проект? Ответь на пять вопросов - вайтлистеры рассмотрят заявку.",
        "fields": [("age", "Сколько вам лет?", TEXT, True, 3),
                   ("rp", "Ваш уровень РП (1/10)", TEXT, True, 10),
                   ("source", "Откуда узнали о нас?", TEXT, True, 200),
                   ("goal", "Расскажите о цели на проекте", PARA, True, 1000),
                   ("nick", "Ваш ник майкрафт", TEXT, True, 32)],
        "brief": [("age", "Возраст")],
        "topic": lambda v, u: f"Заявка • {u.display_name}",
        "ok_btn": "Принять", "done": "Принята", "max_active": 1, "role_key": "app_role_id", "prefix": "ticket", "acc": "заявку", "section": "Заявка в проект",
    },
    "report": {
        "label": "Жалоба", "emoji": "⚠️", "btn": "Написать жалобу", "style": discord.ButtonStyle.danger,
        "desc": "Есть жалоба на игрока, сервер или администрацию? Кратко назови тему и подробно опиши проблему.",
        "fields": [("topic", "На что жалоба (коротко)", TEXT, True, 100),
                   ("details", "Подробное описание", PARA, True, 1500)],
        "brief": [],
        "topic": lambda v, u: v["topic"],
        "ok_btn": "Решено", "done": "Решена", "max_active": 3, "role_key": "report_role_id", "prefix": "report", "acc": "жалобу", "section": "Жалобы",
    },
    "bug": {
        "label": "Баг", "emoji": "🐞", "btn": "Сообщить о баге", "style": discord.ButtonStyle.secondary,
        "desc": "Что-то сломалось на сервере или в боте? Расскажи, мы исправим.",
        "fields": [("topic", "Что сломалось (коротко)", TEXT, True, 100),
                   ("details", "Описание бага", PARA, True, 1500),
                   ("steps", "Как повторить (необязательно)", PARA, False, 800)],
        "brief": [],
        "topic": lambda v, u: v["topic"],
        "ok_btn": "Исправлено", "done": "Исправлен", "max_active": 3, "role_key": "bug_role_id", "prefix": "bug", "acc": "баг", "section": "Баги",
    },
}
KIND_CHOICES = [app_commands.Choice(name=k["section"], value=key) for key, k in KINDS.items()]


# ====== КОНФИГ ======
def load_cfg():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def gcfg(gid):
    return load_cfg().get(str(gid), {})


def set_gcfg(gid, **kw):
    cfg = load_cfg()
    cfg.setdefault(str(gid), {}).update(kw)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ====== БАЗА ======
db = sqlite3.connect("data.db")
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS subs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, kind TEXT, author_id INTEGER,
    topic TEXT, fields TEXT, status TEXT DEFAULT 'new', handler_id INTEGER, reason TEXT,
    channel_id INTEGER, message_id INTEGER, ticket_channel_id INTEGER, ticket_msg_id INTEGER,
    created_ts INTEGER, closed_ts INTEGER, pinged INTEGER DEFAULT 0, num INTEGER)""")
if "num" not in [r["name"] for r in db.execute("PRAGMA table_info(subs)")]:
    db.execute("ALTER TABLE subs ADD COLUMN num INTEGER")
db.execute("UPDATE subs SET num=(SELECT COUNT(*) FROM subs s2 WHERE s2.guild_id=subs.guild_id "
           "AND s2.kind=subs.kind AND s2.id<=subs.id) WHERE num IS NULL")
db.commit()


def now():
    return int(time.time())


def sub_by_id(sid):
    return db.execute("SELECT * FROM subs WHERE id=?", (sid,)).fetchone()


def find_sub(i):
    """Заявку находим по сообщению-карточке или по каналу тикета."""
    return (db.execute("SELECT * FROM subs WHERE message_id=?", (i.message.id,)).fetchone()
            or db.execute("SELECT * FROM subs WHERE ticket_channel_id=?", (i.channel.id,)).fetchone())


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{max(m, 1)} мин"


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


def team_only():
    async def pred(i: discord.Interaction):
        if i.guild and is_team(i.user, gcfg(i.guild.id)):
            return True
        raise app_commands.CheckFailure("Эта команда только для администрации.")
    return app_commands.check(pred)


# ====== БОТ ======
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents,
                         max_messages=None,  # кэш сообщений не нужен
                         allowed_mentions=discord.AllowedMentions(roles=False, everyone=False, users=True, replied_user=False))

    async def setup_hook(self):
        self.add_view(PanelView(list(KINDS)))
        self.add_view(SubmissionView())
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
        traceback.print_exception(type(error), error, error.__traceback__)
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


# ====== КАРТОЧКА ЗАЯВКИ ======
def status_info(sub):
    k = KINDS[sub["kind"]]
    return {"new": ("Новая", GOLD), "work": ("В обработке", ORANGE),
            "approved": (k["done"], GREEN), "rejected": ("Отклонена", RED_LIGHT)}[sub["status"]]


def discord_fields(uid, user):
    ts = int(discord.utils.snowflake_time(uid).timestamp())
    who = f"{user.display_name} (@{user.name})" if user else f"ID {uid}"
    return [("Discord", who, True), ("Аккаунт создан", f"<t:{ts}:D> (<t:{ts}:R>)", True)]


def brief_fields(sub, user):
    """Коротко: ник в дискорде, дата создания аккаунта + пара ключевых полей."""
    values = json.loads(sub["fields"])
    out = discord_fields(sub["author_id"], user)
    for key, name in KINDS[sub["kind"]]["brief"]:
        if values.get(key):
            out.append((name, values[key][:200], True))
    return out


def full_embed(sub, user):
    k, values = KINDS[sub["kind"]], json.loads(sub["fields"])
    e = discord.Embed(title=f"{k['emoji']} {k['label']} #{sub['num']}", description=f"**{sub['topic']}**",
                      color=discord.Color.from_rgb(*GOLD))
    for name, val, inline in discord_fields(sub["author_id"], user):
        e.add_field(name=name, value=val, inline=inline)
    for key, label, *_ in k["fields"]:
        if values.get(key):
            e.add_field(name=label, value=values[key][:1024], inline=False)
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
    buf = await asyncio.to_thread(make_card, k["label"], sub["topic"], text, color, name, avatar, f"#{sub['num']}")
    file = discord.File(buf, "card.png")

    e = discord.Embed(color=discord.Color.from_rgb(*color))
    for n, v, inline in brief_fields(sub, user):
        e.add_field(name=n, value=v, inline=inline)
    e.add_field(name="Статус", value=text)
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
    return e, file, SubmissionView(sub["kind"], sub["status"])


async def refresh_message(sid):
    sub = sub_by_id(sid)
    try:
        ch = bot.get_channel(sub["channel_id"]) or await bot.fetch_channel(sub["channel_id"])
        msg = await ch.fetch_message(sub["message_id"])
        e, f, v = await render(sub)
        await msg.edit(embed=e, attachments=[f], view=v)
    except discord.HTTPException:
        traceback.print_exc()
    if sub["ticket_channel_id"] and sub["ticket_msg_id"]:
        try:
            tch = bot.get_channel(sub["ticket_channel_id"])
            if tch:
                tm = await tch.fetch_message(sub["ticket_msg_id"])
                await tm.edit(view=SubmissionView(sub["kind"], sub["status"]))
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
    author = guild.get_member(sub["author_id"])
    if author:
        overwrites[author] = P(**base)
    # Тикет виден только взявшему, автору и администраторам сервера. Роли персонала его не видят,
    # пока их не отметят в тикете (см. on_message).
    category = guild.get_channel(cfg.get("ticket_category_id", 0))
    return await guild.create_text_channel(
        name=f"{KINDS[sub['kind']]['prefix']}-{sub['num']:04d}", category=category, overwrites=overwrites,
        topic=f"{KINDS[sub['kind']]['label']} #{sub['num']} • {sub['topic']}")


def make_nick(dname, mc):
    """Формат: ник дискорда // ник майнкрафта (без @), максимум 32 символа."""
    sep = " // "
    mc = mc.replace("@", "").strip()
    mc = mc[:32 - len(sep) - 1]
    room = 32 - len(sep) - len(mc)
    return f"{dname.strip()[:room]}{sep}{mc}"


async def set_nickname(sub):
    """Меняет ник игрока на сервере после принятия заявки, возвращает текст-примечание."""
    guild = bot.get_guild(sub["guild_id"])
    member = guild.get_member(sub["author_id"]) if guild else None
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
            traceback.print_exc()


async def transcript_file(sub):
    ch = bot.get_channel(sub["ticket_channel_id"]) if sub["ticket_channel_id"] else None
    if not ch:
        return None
    lines = []
    async for m in ch.history(limit=None, oldest_first=True):
        if m.author.id == bot.user.id:
            continue
        files = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M} UTC] {m.author.display_name}: {m.clean_content} {files}".strip())
    data = io.BytesIO(("\n".join(lines) or "(сообщений нет)").encode("utf-8"))
    return discord.File(data, f"{KINDS[sub['kind']]['prefix']}-{sub['num']:04d}.txt")


async def send_log(sub, nick_note, dur):
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
    file = await transcript_file(sub)
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
    for job in (lock_ticket(sub), send_log(sub, nick_note, dur)):  # сначала закрыть, потом лог с перепиской
        try:
            await job
        except Exception:
            traceback.print_exc()
    if ok:
        text, color = f"Статус: **{k['done']}** ✅", GREEN
    else:
        text, color = "Статус: **отклонено** ❌" + (f"\nПричина: {reason}" if reason else ""), RED_LIGHT
    await notify_author(sub, text, color)
    return (f"{'✅' if ok else '❌'} {k['label']} #{sub['num']}: {k['done'].lower() if ok else 'отклонена'}. "
            f"Время обработки: {dur}.{nick_note}")


# ====== КНОПКИ ПОД ЗАЯВКОЙ ======
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
    def __init__(self, kind=None, status="new"):
        super().__init__(timeout=None)
        if kind:
            self.ok.label = KINDS[kind]["ok_btn"]
        if status in ("approved", "rejected"):
            self.take.disabled = self.ok.disabled = self.no.disabled = True
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
        if sub["status"] == "new":
            k = KINDS[sub["kind"]]
            e = discord.Embed(title=f"{k['emoji']} {k['label']} #{sub['num']}", description=f"**{sub['topic']}**\n\n"
                              "Полные ответы откроются, когда возьмёшь заявку в работу.",
                              color=discord.Color.from_rgb(*GOLD))
            for n, v, inline in brief_fields(sub, user):
                e.add_field(name=n, value=v, inline=inline)
        else:
            e = full_embed(sub, user)
        await i.response.send_message(embed=e, ephemeral=True)

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
            traceback.print_exc()
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
            embed=full_embed(sub, author), view=SubmissionView(sub["kind"], "work"))
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


# ====== ФОРМА ДЛЯ ИГРОКА ======
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
        channel = i.guild.get_channel(cfg.get(f"{self.kind}_channel_id", 0))
        if not channel:
            return await i.response.send_message("Раздел не настроен (/setup).", ephemeral=True)
        values = {key: v.value.strip() for key, v in self.inputs.items()}
        if self.kind == "app" and not values["age"].isdigit():
            return await i.response.send_message("Возраст нужно указать числом.", ephemeral=True)
        active = db.execute("SELECT COUNT(*) FROM subs WHERE guild_id=? AND author_id=? AND kind=? "
                            "AND status IN ('new','work')", (i.guild.id, i.user.id, self.kind)).fetchone()[0]
        if active >= k["max_active"]:
            return await i.response.send_message("У тебя уже есть необработанные обращения этого типа.", ephemeral=True)

        await i.response.defer(ephemeral=True)
        topic = k["topic"](values, i.user)[:200]
        num = db.execute("SELECT COALESCE(MAX(num),0)+1 FROM subs WHERE guild_id=? AND kind=?",
                         (i.guild.id, self.kind)).fetchone()[0]
        cur = db.execute("INSERT INTO subs(guild_id,kind,author_id,topic,fields,created_ts,num) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (i.guild.id, self.kind, i.user.id, topic, json.dumps(values, ensure_ascii=False), now(), num))
        db.commit()
        sid = cur.lastrowid
        e, f, v = await render(sub_by_id(sid))
        msg = await channel.send(embed=e, file=f, view=v)
        db.execute("UPDATE subs SET channel_id=?, message_id=? WHERE id=?", (channel.id, msg.id, sid))
        db.commit()
        await i.followup.send(f"✅ Отправлено! Номер обращения: **#{num}**. Когда его возьмут в работу, "
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
def leaderboard(gid, kind=None, since=0, limit=8):
    q = ("SELECT handler_id, COUNT(*) total, SUM(status='approved') ok, AVG(closed_ts-created_ts) avg "
         "FROM subs WHERE guild_id=? AND status IN ('approved','rejected') AND handler_id IS NOT NULL "
         "AND closed_ts>=?")
    args = [gid, since]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " GROUP BY handler_id ORDER BY total DESC, avg ASC LIMIT ?"
    return db.execute(q, args + [limit]).fetchall()


def totals(gid, kind=None, since=0):
    w, a = "guild_id=?", [gid]
    if kind:
        w += " AND kind=?"
        a.append(kind)
    created = db.execute(f"SELECT COUNT(*) FROM subs WHERE {w} AND created_ts>=?", a + [since]).fetchone()[0]
    h = db.execute(f"SELECT COUNT(*), SUM(status='approved'), AVG(closed_ts-created_ts) FROM subs "
                   f"WHERE {w} AND status IN ('approved','rejected') AND closed_ts>=?", a + [since]).fetchone()
    open_ = db.execute(f"SELECT COUNT(*) FROM subs WHERE {w} AND status IN ('new','work')", a).fetchone()[0]
    return dict(created=created, handled=h[0], approved=h[1] or 0, avg=h[2], open=open_)


_SAME = object()


async def build_stats(guild, title, since=0, kind=None, board_kind=_SAME):
    bk = kind if board_kind is _SAME else board_kind
    board = []
    for r in leaderboard(guild.id, bk, since):
        m = guild.get_member(r["handler_id"])
        board.append((m.display_name if m else f"ID {r['handler_id']}", r["total"], r["ok"] or 0, fmt_dur(r["avg"])))
    t = totals(guild.id, kind, since)
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
    apps = leaderboard(guild.id, "app", since)
    e, f = await build_stats(guild, "Вайтлистеры недели", since, kind=None, board_kind="app")
    e.title = "📊 Итоги недели"
    e.description = (f"🏆 **Лучший вайтлистер недели:** <@{apps[0]['handler_id']}> - обработано заявок: {apps[0]['total']}"
                     if apps else "На этой неделе заявки не обрабатывались.")
    await ch.send(embed=e, file=f)


# ====== ФОНОВЫЕ ЗАДАЧИ ======
async def remind_unanswered():
    rows = db.execute("SELECT * FROM subs WHERE status='new' AND pinged=0 AND created_ts<?",
                      (now() - REMIND_AFTER,)).fetchall()
    for sub in rows:
        cfg = gcfg(sub["guild_id"])
        rid = kind_role_id(cfg, sub["kind"]) or cfg.get("staff_role_id")
        try:
            ch = bot.get_channel(sub["channel_id"])
            msg = await ch.fetch_message(sub["message_id"])
            await msg.reply(f"{f'<@&{rid}> ' if rid else ''}⏰ Обращение **#{sub['num']}** без ответа уже больше суток!",
                            allowed_mentions=discord.AllowedMentions(roles=True))
        except Exception:
            traceback.print_exc()
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
                traceback.print_exc()
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


@tasks.loop(minutes=5)
async def background():
    for job in (remind_unanswered, cleanup_tickets, weekly_check):
        try:
            await job()
        except Exception:
            traceback.print_exc()


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
    rows = db.execute("SELECT kind, status, closed_ts-created_ts AS dur FROM subs WHERE guild_id=? AND handler_id=?",
                      (i.guild.id, member.id)).fetchall()
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


# ====== ЦИТАТЫ ======
QUOTE_HOOK = "Цитаты"
NO_MENTIONS = discord.AllowedMentions.none()


def quote_names(user):
    """(имя для шапки, подпись под цитатой): «ник | юзернейм»."""
    same = user.display_name == user.name
    return (user.name if same else f"{user.display_name} | {user.name}",
            f"@{user.name}" if same else f"@{user.display_name} | {user.name}")


async def post_quote(channel, user, text, signature=None, style="cream"):
    """Рисует цитату и отправляет от имени автора (вебхук). Без права на вебхуки - обычным сообщением."""
    try:
        avatar = await user.display_avatar.replace(size=256, format="png").read()
    except discord.HTTPException:
        avatar = None
    name, handle = quote_names(user)
    buf = await asyncio.to_thread(make_quote, text, signature or handle, avatar, style)
    is_thread = isinstance(channel, discord.Thread)
    target = channel.parent if is_thread else channel
    msg = None
    try:
        hooks = await target.webhooks()
        hook = (discord.utils.find(lambda h: h.name == QUOTE_HOOK and h.token, hooks)
                or await target.create_webhook(name=QUOTE_HOOK))
        kw = {"thread": channel} if is_thread else {}
        msg = await hook.send(file=discord.File(buf, "quote.png"), username=name[:80],
                              avatar_url=user.display_avatar.url, allowed_mentions=NO_MENTIONS, wait=True, **kw)
    except discord.HTTPException:
        buf.seek(0)
        msg = await channel.send(content=f"**{name}**", file=discord.File(buf, "quote.png"),
                                 allowed_mentions=NO_MENTIONS)
    try:
        await msg.add_reaction("❤️")
    except discord.HTTPException:
        pass


STYLE_CHOICES = [app_commands.Choice(name="Светлая (как в примерах)", value="cream"),
                 app_commands.Choice(name="Тёмная (красно-золотая)", value="dark")]


@bot.tree.command(name="quote", description="Оформить цитату картинкой")
@app_commands.describe(text="Текст цитаты", user="Чья цитата (по умолчанию - ты)",
                       signature="Своя подпись вместо @ника", style="Оформление")
@app_commands.choices(style=STYLE_CHOICES)
@app_commands.guild_only()
async def quote(i: discord.Interaction, text: app_commands.Range[str, 1, 400],
                user: discord.Member | None = None, signature: app_commands.Range[str, 1, 50] | None = None,
                style: app_commands.Choice[str] | None = None):
    await i.response.defer(ephemeral=True)
    await post_quote(i.channel, user or i.user, text, signature, style.value if style else "cream")
    await i.followup.send("Цитата отправлена.", ephemeral=True)


@bot.tree.context_menu(name="Цитата")
@app_commands.guild_only()
async def quote_message(i: discord.Interaction, message: discord.Message):
    if not message.clean_content.strip():
        return await i.response.send_message("В сообщении нет текста.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await post_quote(i.channel, message.author, message.clean_content[:400])
    await i.followup.send("Цитата отправлена.", ephemeral=True)


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
    print(f"Вошёл как {bot.user}")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не найден DISCORD_TOKEN. Добавь его в переменные окружения хоста "
                         "или в файл .env рядом с bot.py (строка DISCORD_TOKEN=токен).")
    bot.run(token)
