"""Слэш-команды бота и обработчик сообщений в тикетах."""
import time, asyncio, platform, datetime
import discord
from discord import app_commands

import store
import rcon
from store import db, gcfg, set_gcfg, now, fmt_dur
from kinds import KINDS, KIND_CHOICES
from cards import make_banner, GOLD
from core import bot, is_team, can_handle, handled_kinds, team_only, staff_only, get_user
from tickets import details_embed, status_info, transcript_file
from forms import PanelView, REAPPLY_DAYS
from stats import build_stats, post_weekly


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
                       reapply_days="Через сколько дней можно подать заявку после отказа (0 - без ограничения)",
                       auto_close_days="Закрывать обращение, если игрок молчит столько дней (0 - не закрывать)",
                       backup_discord="Присылать суточную копию базы в канал логов",
                       error_log="Присылать ошибки бота в канал логов",
                       whitelist="Добавлять принятых игроков в вайтлист сервера по RCON (если RCON настроен)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def settings(i: discord.Interaction, mc_check: bool | None = None,
                   reapply_days: app_commands.Range[int, 0, 365] | None = None,
                   auto_close_days: app_commands.Range[int, 0, 60] | None = None,
                   backup_discord: bool | None = None, error_log: bool | None = None,
                   whitelist: bool | None = None):
    changes = {k: v for k, v in dict(mc_check=mc_check, reapply_days=reapply_days, auto_close_days=auto_close_days,
                                     backup_discord=backup_discord, error_log=error_log, whitelist=whitelist).items()
               if v is not None}
    if changes:
        set_gcfg(i.guild.id, **changes)
    cfg = gcfg(i.guild.id)
    on = lambda v: "вкл" if v else "выкл"
    days = cfg.get("auto_close_days", 0)
    await i.response.send_message(
        f"Проверка ника Minecraft: **{on(cfg.get('mc_check', True))}**\n"
        f"Повторная заявка после отказа: **{cfg.get('reapply_days', REAPPLY_DAYS)} дн.**\n"
        f"Автозакрытие при молчании игрока: **{str(days) + ' дн.' if days else 'выкл'}**\n"
        f"Копия базы в канал логов: **{on(cfg.get('backup_discord', False))}**\n"
        f"Ошибки бота в канал логов: **{on(cfg.get('error_log', True))}**\n"
        f"Вайтлист по RCON: **{on(cfg.get('whitelist', True))}** "
        f"({'RCON настроен' if rcon.config() else 'RCON не настроен на хосте'})", ephemeral=True)


@bot.tree.command(name="block", description="Закрыть игроку подачу заявок в проект (админка)")
@app_commands.describe(user="Кому закрыть", reason="Причина (видна только персоналу)")
@app_commands.guild_only()
@team_only()
async def block(i: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 0, 200] = ""):
    store.block_user(i.guild.id, user.id, reason, i.user.id)
    await i.response.send_message(f"{user.mention} больше не может подавать заявки в проект.", ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="unblock", description="Снять запрет на подачу заявок (админка)")
@app_commands.describe(user="С кого снять")
@app_commands.guild_only()
@team_only()
async def unblock(i: discord.Interaction, user: discord.User):
    done = store.unblock_user(i.guild.id, user.id)
    await i.response.send_message(f"{user.mention}: запрет снят." if done else f"{user.mention} не был в списке.",
                                  ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="blocked", description="Кому закрыта подача заявок (админка)")
@app_commands.guild_only()
@team_only()
async def blocked(i: discord.Interaction):
    rows = store.blocked_list(i.guild.id)
    if not rows:
        return await i.response.send_message("Список пуст.", ephemeral=True)
    lines = [f"<@{r['user_id']}> - {r['reason'] or 'без причины'} (<@{r['by_id']}>, <t:{r['ts']}:d>)" for r in rows]
    await i.response.send_message(embed=discord.Embed(title="Закрыта подача заявок", description="\n".join(lines),
                                                      color=discord.Color.from_rgb(*GOLD)), ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


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
    if message.author.bot or not message.guild:
        return
    store.touch_human(message.channel.id, message.author.id, int(message.created_at.timestamp()))
    if not (message.mentions or message.role_mentions):
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
