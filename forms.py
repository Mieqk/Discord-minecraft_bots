"""Форма и панель для игрока: проверки заявки, отправка в канал персонала."""
import discord

import store
import mojang
from store import db, gcfg, now, fmt_dur, sub_by_id
from kinds import KINDS
from core import is_team, ping_role
from tickets import render

SPAM_LIMIT, SPAM_WINDOW = 5, 3600   # не больше 5 обращений в час от одного человека
REAPPLY_DAYS = 3                # через сколько дней можно подать заявку после отказа (/settings reapply_days)


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
