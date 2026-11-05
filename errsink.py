"""Ошибки бота (уровень ERROR) копятся здесь и раз в несколько минут уходят в канал логов."""
import logging
import collections

_queue = collections.deque(maxlen=20)


class SinkHandler(logging.Handler):
    def emit(self, record):
        if record.name != "bot" or record.levelno < logging.ERROR:
            return
        try:
            _queue.append(self.format(record))
        except Exception:
            pass


def drain(limit=3, size=1800):
    """Забирает накопленные ошибки: без подряд идущих повторов, каждая обрезана до size символов."""
    out, last = [], None
    while _queue and len(out) < limit:
        text = _queue.popleft()
        if text != last:
            out.append(text[-size:].replace("```", "'''"))
        last = text
    rest = len(_queue)
    _queue.clear()
    if rest:
        out.append(f"... и ещё {rest} ошибок, подробности в data/logs/bot.log")
    return out
