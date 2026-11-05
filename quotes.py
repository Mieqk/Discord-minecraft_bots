"""Цитаты: картинка от имени автора (вебхук), избранное по реакции ⭐, фон-картинка."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import store
from cards import make_quote

log = logging.getLogger("bot")

QUOTE_HOOK = "Цитаты"
NO_MENTIONS = discord.AllowedMentions.none()
HEART, STAR = "❤️", "⭐"
MAX_BG_BYTES = 4 * 1024 * 1024

STYLE_CHOICES = [app_commands.Choice(name="Светлая (как в примерах)", value="cream"),
                 app_commands.Choice(name="Тёмная (красно-золотая)", value="dark")]


def quote_names(user):
    """(имя для шапки, подпись под цитатой): «ник | юзернейм»."""
    same = user.display_name == user.name
    return (user.name if same else f"{user.display_name} | {user.name}",
            f"@{user.name}" if same else f"@{user.display_name} | {user.name}")


def link(q):
    return f"https://discord.com/channels/{q['guild_id']}/{q['channel_id']}/{q['message_id']}"


class Quotes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.menus = [
            app_commands.ContextMenu(name="Цитата", callback=self.quote_light),
            app_commands.ContextMenu(name="Цитата (тёмная)", callback=self.quote_dark),
        ]
        for m in self.menus:
            m.guild_only = True
            bot.tree.add_command(m)

    async def cog_unload(self):
        for m in self.menus:
            self.bot.tree.remove_command(m.name, type=m.type)

    # ----- отправка -----
    async def post_quote(self, channel, user, text, signature=None, style="cream", background=None):
        try:
            avatar = await user.display_avatar.replace(size=256, format="png").read()
        except discord.HTTPException:
            avatar = None
        name, handle = quote_names(user)
        buf = await asyncio.to_thread(make_quote, text, signature or handle, avatar, style, background)
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
        store.add_quote(channel.guild.id, channel.id, msg.id, user.id, text, signature or handle,
                        "photo" if background else style)
        for emoji in (HEART, STAR):
            try:
                await msg.add_reaction(emoji)
            except discord.HTTPException:
                pass

    # ----- команды -----
    @app_commands.command(name="quote", description="Оформить цитату картинкой")
    @app_commands.describe(text="Текст цитаты", user="Чья цитата (по умолчанию - ты)",
                           signature="Своя подпись вместо @ника", style="Оформление",
                           background="Картинка-фон (необязательно, до 4 МБ)")
    @app_commands.choices(style=STYLE_CHOICES)
    @app_commands.guild_only()
    async def quote(self, i: discord.Interaction, text: app_commands.Range[str, 1, 400],
                    user: discord.Member | None = None, signature: app_commands.Range[str, 1, 50] | None = None,
                    style: app_commands.Choice[str] | None = None, background: discord.Attachment | None = None):
        data = None
        if background:
            if not (background.content_type or "").startswith("image/") or background.size > MAX_BG_BYTES:
                return await i.response.send_message("Фон: нужна картинка до 4 МБ.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        if background:
            data = await background.read()
        await self.post_quote(i.channel, user or i.user, text, signature, style.value if style else "cream", data)
        await i.followup.send("Цитата отправлена. Поставь ⭐ на цитату, чтобы сохранить её в избранное.", ephemeral=True)

    async def _quote_message(self, i, message, style):
        text = message.clean_content.strip()
        if not text:
            return await i.response.send_message("В сообщении нет текста.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await self.post_quote(i.channel, message.author, text[:400], style=style)
        await i.followup.send("Цитата отправлена.", ephemeral=True)

    async def quote_light(self, i: discord.Interaction, message: discord.Message):
        await self._quote_message(i, message, "cream")

    async def quote_dark(self, i: discord.Interaction, message: discord.Message):
        await self._quote_message(i, message, "dark")

    quotes = app_commands.Group(name="quotes", description="Цитаты сервера", guild_only=True)

    @quotes.command(name="favorites", description="Мои избранные цитаты (поставь ⭐ на цитату)")
    async def favorites(self, i: discord.Interaction):
        rows = store.favorites(i.user.id, i.guild.id)
        if not rows:
            return await i.response.send_message("Пока пусто. Поставь ⭐ на любую цитату.", ephemeral=True)
        lines = [f"• [{r['text'][:60]}{'…' if len(r['text']) > 60 else ''}]({link(r)}) - {r['signature']}" for r in rows]
        await i.response.send_message(embed=discord.Embed(title="⭐ Избранное", description="\n".join(lines),
                                                          color=discord.Color.gold()), ephemeral=True)

    @quotes.command(name="random", description="Случайная цитата сервера")
    async def random(self, i: discord.Interaction):
        q = store.random_quote(i.guild.id)
        if not q:
            return await i.response.send_message("Цитат пока нет.", ephemeral=True)
        await i.response.send_message(f"{q['signature']}: {q['text']}\n{link(q)}", allowed_mentions=NO_MENTIONS)

    # ----- избранное по реакции -----
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or str(payload.emoji) != STAR:
            return
        q = store.quote_by_message(payload.message_id)
        if q:
            store.add_fav(payload.user_id, q["id"])

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != STAR:
            return
        q = store.quote_by_message(payload.message_id)
        if q:
            store.remove_fav(payload.user_id, q["id"])
