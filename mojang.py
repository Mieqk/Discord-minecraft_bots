"""Проверка ника Minecraft (Java) через API Mojang. При любой сетевой ошибке возвращает None, а не блокирует заявку."""
import aiohttp

URL = "https://api.mojang.com/users/profiles/minecraft/{}"


async def exists(nick, timeout=4):
    """True - ник существует, False - нет, None - проверить не удалось."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(URL.format(nick)) as r:
                if r.status == 200:
                    return True
                if r.status in (204, 404):
                    return False
                return None
    except Exception:
        return None
