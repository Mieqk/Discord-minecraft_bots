"""Автоматический вайтлист: добавление ника на сервер Minecraft по RCON.

Включается переменными окружения (пароль никогда не кладём в репозиторий):
  RCON_HOST, RCON_PASSWORD, RCON_PORT (по умолчанию 25575),
  RCON_WHITELIST_CMD (по умолчанию «whitelist add {nick}»).
Без RCON_HOST и RCON_PASSWORD функция выключена.
"""
import os
import struct
import asyncio

import store

AUTH, EXEC = 3, 2
AUTH_RESPONSE = 2


def _packet(pid, ptype, body):
    data = struct.pack("<ii", pid, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(data)) + data


async def _read(reader):
    size = struct.unpack("<i", await reader.readexactly(4))[0]
    data = await reader.readexactly(size)
    pid, ptype = struct.unpack("<ii", data[:8])
    return pid, ptype, data[8:-2].decode("utf-8", "replace")


async def run(host, port, password, command, timeout=5):
    """Выполняет команду на сервере и возвращает ответ. При любой ошибке бросает исключение."""
    async def go():
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(_packet(1, AUTH, password))
            await writer.drain()
            while True:  # некоторые серверы шлют пустой ответ перед подтверждением входа
                pid, ptype, _ = await _read(reader)
                if ptype == AUTH_RESPONSE:
                    break
            if pid == -1:
                raise PermissionError("неверный пароль RCON")
            writer.write(_packet(2, EXEC, command))
            await writer.drain()
            while True:
                pid, ptype, body = await _read(reader)
                if pid == 2:
                    return body.strip()
        finally:
            writer.close()
    return await asyncio.wait_for(go(), timeout)


def config():
    host, password = os.environ.get("RCON_HOST", "").strip(), os.environ.get("RCON_PASSWORD", "")
    if not host or not password:
        return None
    try:
        port = int(os.environ.get("RCON_PORT", "25575"))
    except ValueError:
        port = 25575
    return dict(host=host, port=port, password=password,
                command=os.environ.get("RCON_WHITELIST_CMD", "whitelist add {nick}"))


async def whitelist_add(nick):
    """None - RCON не настроен; иначе (успех, текст для персонала)."""
    cfg = config()
    if not cfg:
        return None
    if not store.mc_nick_valid(nick):  # строгая проверка: ник попадает в команду сервера
        return False, "ник не подходит (нужно 3-16 символов: латиница, цифры, «_»), добавь вручную"
    try:
        reply = await run(cfg["host"], cfg["port"], cfg["password"], cfg["command"].format(nick=nick))
        return True, reply or "готово"
    except PermissionError as e:
        return False, str(e)
    except Exception as e:
        return False, f"сервер не ответил ({type(e).__name__}), добавь вручную"
