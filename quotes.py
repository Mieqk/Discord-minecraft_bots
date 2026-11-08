"""Цитаты: картинка от имени автора (вебхук), избранное по реакции ⭐, фон-картинка."""
import time
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import store
from cards import make_quote, make_banner, GOLD
from core import team_only

log = logging.getLogger("bot")

QUOTE_HOOK = "Цитаты"
NO_MENTIONS = discord.AllowedMentions.none()
HEART, STAR = "❤️", "⭐"
MAX_BG_BYTES = 4 * 1024 * 1024

STYLE_CHOICES = [app_commands.Choice(name="Тёмная, красно-золотая (по умолчанию)", value="dark"),
                 app_commands.Choice(name="Светлая", value="cream")]
AUTO_COOLDOWN = 5  # секунд между автоцитатами одного человека


def split_signature(text):
    """«Текст\n— Подпись»: последняя строка с тире становится подписью. Возвращает (текст, подпись или None)."""
    lines = text.strip().splitlines()
    if len(lines) > 1 and lines[-1].lstrip().startswith(("—", "–", "--")):
        sig = lines[-1].lstrip("—–- ").strip()[:50]
        body = "\n".join(lines[:-1]).strip()
        if sig and body:
            return body, sig
    return text.strip(), None


def quote_names(user):
    """(имя для шапки, подпись под цитатой): «ник | юзернейм»."""
    same = user.display_name == user.name
    return (user.name if same else f"{user.display_name} | {user.name}",
            f"@{user.name}" if same else f"@{user.display_name} | {user.name}")


def cooldown_ok(last, uid):
    """Не чаще одной цитаты в AUTO_COOLDOWN секунд от одного человека. last - словарь {id: время}."""
    t = time.monotonic()
    if t - last.get(uid, -AUTO_COOLDOWN) < AUTO_COOLDOWN:
        return False
    last[uid] = t
    return True


class QuoteModal(discord.ui.Modal, title="Цитата"):
    text = discord.ui.TextInput(label="Текст цитаты", style=discord.TextStyle.paragraph, max_length=400)
    signature = discord.ui.TextInput(label="Подпись (необязательно)", required=False, max_length=50)

    async def on_submit(self, i: discord.Interaction):
        cog = i.client.get_cog("Quotes")
        await i.response.defer(ephemeral=True)
        if not cooldown_ok(cog._last, i.user.id):
            return await i.followup.send("Не так быстро, подожди пару секунд.", ephemeral=True)
        await cog.post_quote(i.channel, i.user, self.text.value.strip(), self.signature.value.strip() or None, "dark")
        await i.followup.send("Цитата отправлена.", ephemeral=True)


class QuotePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Оставить цитату", emoji="💬", style=discord.ButtonStyle.secondary,
                       custom_id="panel:quote")
    async def leave(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(QuoteModal())


def link(q):
    return f"https://discord.com/channels/{q['guild_id']}/{q['channel_id']}/{q['message_id']}"


class Quotes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.menus = [
            app_commands.ContextMenu(name="Цитата", callback=self.quote_dark),
            app_commands.ContextMenu(name="Цитата (светлая)", callback=self.quote_light),
        ]
        self._last = {}
        for m in self.menus:
            m.guild_only = True
            bot.tree.add_command(m)

    async def cog_load(self):
        self.bot.add_view(QuotePanelView())

    async def cog_unload(self):
        for m in self.menus:
            self.bot.tree.remove_command(m.name, type=m.type)

    # ----- отправка -----
    async def post_quote(self, channel, user, text, signature=None, style="dark", background=None):
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
        await self.post_quote(i.channel, user or i.user, text, signature, style.value if style else "dark", data)
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

    @app_commands.command(name="panel_quote", description="Панель цитат: канал, где сообщения сами становятся цитатами")
    @app_commands.guild_only()
    @team_only()
    async def panel_quote(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        banner = await asyncio.to_thread(make_banner, i.guild.name, "Цитаты")
        e = discord.Embed(title="💬 Цитаты", color=discord.Color.from_rgb(*GOLD), description=(
            "Нажми кнопку или просто напиши сообщение в этом канале: бот сам заменит его цитатой от твоего имени.\n"
            "• Подпись: последняя строка, начинающаяся с «—».\n"
            "• Картинка во вложении станет фоном цитаты."))
        e.set_image(url="attachment://panel.png")
        await i.channel.send(embed=e, file=discord.File(banner, "panel.png"), view=QuotePanelView())
        store.set_gcfg(i.guild.id, quote_channel_id=i.channel.id)
        perms = i.channel.permissions_for(i.guild.me)
        missing = [t for a, t in (("manage_webhooks", "управлять вебхуками"), ("manage_messages", "управлять сообщениями"),
                                  ("add_reactions", "ставить реакции")) if not getattr(perms, a)]
        warn = ("\n⚠️ У бота в этом канале нет права: " + "; ".join(missing)) if missing else ""
        await i.followup.send("Панель отправлена, этот канал стал каналом цитат." + warn, ephemeral=True)

    @app_commands.command(name="quote_channel", description="Канал, где сообщения сами становятся цитатами (админка)")
    @app_commands.describe(channel="Канал цитат (не указывай, чтобы выключить)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def quote_channel(self, i: discord.Interaction, channel: discord.TextChannel | None = None):
        store.set_gcfg(i.guild.id, quote_channel_id=channel.id if channel else 0)
        if not channel:
            return await i.response.send_message("Автоцитаты выключены.", ephemeral=True)
        perms = channel.permissions_for(i.guild.me)
        need = {"manage_webhooks": "управлять вебхуками (цитата от имени автора)",
                "manage_messages": "управлять сообщениями (удалять исходное сообщение)",
                "send_messages": "писать в канал", "attach_files": "прикреплять файлы",
                "add_reactions": "ставить реакции"}
        missing = [text for attr, text in need.items() if not getattr(perms, attr)]
        warn = ("\n⚠️ У бота в этом канале нет права: " + "; ".join(missing)) if missing else ""
        await i.response.send_message(
            f"Теперь любое сообщение в {channel.mention} станет цитатой. Подпись: последняя строка, начинающаяся "
            f"с «—». Картинка во вложении станет фоном.{warn}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """В канале цитат сообщение автоматически превращается в цитату от имени автора."""
        if message.author.bot or not message.guild:
            return
        quote_channel = store.gcfg(message.guild.id).get("quote_channel_id")
        if not quote_channel or message.channel.id != quote_channel:
            return
        text, signature = split_signature(message.clean_content)
        if not text:
            if message.attachments:
                await message.channel.send(f"{message.author.mention}, добавь к картинке текст цитаты.", delete_after=8,
                                           allowed_mentions=discord.AllowedMentions(users=[message.author]))
            return
        if len(text) > 400:
            return await message.channel.send(f"{message.author.mention}, цитата длиннее 400 символов.",
                                              delete_after=8, allowed_mentions=discord.AllowedMentions(users=[message.author]))
        if not cooldown_ok(self._last, message.author.id):
            return await message.channel.send(f"{message.author.mention}, не так быстро.", delete_after=5,
                                              allowed_mentions=discord.AllowedMentions(users=[message.author]))
        background = None
        for a in message.attachments:
            if (a.content_type or "").startswith("image/") and a.size <= MAX_BG_BYTES:
                try:
                    background = await a.read()
                except discord.HTTPException:
                    pass
                break
        try:
            await self.post_quote(message.channel, message.author, text, signature, "dark", background)
        except Exception:
            log.exception("Автоцитата не отправилась")
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.warning("Не удалось удалить исходное сообщение в канале цитат (нужно право «Управлять сообщениями»)")

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
