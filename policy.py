"""Решения по молчащим тикетам в работе (чистая логика, без Discord)."""


def silence_action(last_human_id, player_id, last_any_ts, last_human_ts, now_ts, remind_after, close_after=0):
    """Что делать с тикетом в работе.

    last_human_id - кто последним написал в тикете (не бот), None если никто.
    Ждём игрока, если последним писал не он (ответил взявший или другой сотрудник); иначе ждём взявшего.
    last_any_ts - время последнего сообщения вообще (в том числе напоминания бота: повтор не чаще раза в remind_after).
    close_after - через сколько молчания игрока закрыть обращение (0 - не закрывать).
    Возвращает None, 'ping_handler', 'ping_player' или 'close'.
    """
    waiting_player = last_human_id is not None and last_human_id != player_id
    if waiting_player and close_after and last_human_ts and now_ts - last_human_ts >= close_after:
        return "close"
    if now_ts - last_any_ts >= remind_after:
        return "ping_player" if waiting_player else "ping_handler"
    return None
