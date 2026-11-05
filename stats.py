"""Статистика: картинка-лидерборд и недельный отчёт."""
import asyncio
import discord

import store
from store import gcfg, now, fmt_dur
from cards import make_leaderboard, GOLD
from core import get_member


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
