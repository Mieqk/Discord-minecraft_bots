"""Тикеты: карточка обращения, кнопки под ней, канал тикета, закрытие."""
import io, json, asyncio
import discord

import store
from store import db, gcfg, now, fmt_dur, make_nick, sub_by_id
from kinds import KINDS, PARA
from cards import make_card, GOLD, ORANGE, GREEN, RED_LIGHT
from core import bot, log, can_handle, get_user, get_member

NICK_FROM_USERNAME = True       # True: ник из юзернейма (carburadiator), False: из отображаемого имени


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
