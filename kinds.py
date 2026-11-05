"""Виды обращений: заявка, жалоба, баг."""
import discord
from discord import app_commands

TEXT, PARA = discord.TextStyle.short, discord.TextStyle.paragraph

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
                   ("category", "Категория: сервер / бот / плагин / другое", TEXT, False, 40),
                   ("details", "Описание бага", PARA, True, 1500),
                   ("steps", "Как повторить (необязательно)", PARA, False, 800)],
        "brief": [("category", "Категория")],
        "topic": lambda v, u: v["topic"],
        "ok_btn": "Исправлено", "done": "Исправлен", "max_active": 3, "role_key": "bug_role_id", "prefix": "bug", "acc": "баг", "section": "Баги",
    },
}
KIND_CHOICES = [app_commands.Choice(name=k["section"], value=key) for key, k in KINDS.items()]
