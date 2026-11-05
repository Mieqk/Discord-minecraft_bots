"""Данные бота без Discord: база SQLite, настройки серверов, запросы статистики, резервные копии.

Всё лежит в папке data/ (её можно сменить переменной окружения DATA_DIR), чтобы код и данные не смешивались.
"""
import os
import re
import json
import time
import shutil
import sqlite3
import logging
import datetime

log = logging.getLogger("bot")

DATA_DIR = os.environ.get("DATA_DIR", "data")
BACKUP_KEEP = 7                 # сколько суточных копий базы хранить
TRANSCRIPT_LIMIT = 200_000      # максимум символов переписки в базе

_state = {"conn": None, "dir": DATA_DIR, "cfg": {}}


class _Conn:
    """Прокси: `from store import db` работает и до init(), а запросы идут в актуальное соединение."""

    def __getattr__(self, name):
        conn = _state["conn"]
        if conn is None:
            raise RuntimeError("store.init() не вызывали")
        return getattr(conn, name)


db = _Conn()


def data_dir():
    return _state["dir"]


def backup_dir():
    return os.path.join(_state["dir"], "backups")


# ====== ИНИЦИАЛИЗАЦИЯ ======
def _ensure_column(table, column, decl):
    if column not in [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init(directory=None, legacy_dir=None):
    """Открывает базу. legacy_dir - где раньше лежали data.db и config.json: их копируем (не двигаем),
    чтобы старые данные не потерялись при переходе на папку data/."""
    d = directory or DATA_DIR
    os.makedirs(d, exist_ok=True)
    _state["dir"], _state["cfg"] = d, {}
    path = os.path.join(d, "data.db")
    if legacy_dir and not os.path.exists(path):
        old = os.path.join(legacy_dir, "data.db")
        if os.path.exists(old):
            shutil.copy2(old, path)
            log.info("База скопирована из %s в %s", old, path)
    if _state["conn"] is not None:
        _state["conn"].close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _state["conn"] = conn

    db.execute("""CREATE TABLE IF NOT EXISTS subs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, kind TEXT, author_id INTEGER,
        topic TEXT, fields TEXT, status TEXT DEFAULT 'new', handler_id INTEGER, reason TEXT,
        channel_id INTEGER, message_id INTEGER, ticket_channel_id INTEGER, ticket_msg_id INTEGER,
        created_ts INTEGER, closed_ts INTEGER, pinged INTEGER DEFAULT 0, num INTEGER)""")
    _ensure_column("subs", "num", "INTEGER")
    _ensure_column("subs", "priority", "INTEGER DEFAULT 0")
    _ensure_column("subs", "transcript", "TEXT")
    db.execute("UPDATE subs SET num=(SELECT COUNT(*) FROM subs s2 WHERE s2.guild_id=subs.guild_id "
               "AND s2.kind=subs.kind AND s2.id<=subs.id) WHERE num IS NULL")
    db.execute("CREATE TABLE IF NOT EXISTS guild_cfg(guild_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS quotes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER UNIQUE,
        author_id INTEGER, text TEXT, signature TEXT, style TEXT, created_ts INTEGER)""")
    db.execute("CREATE TABLE IF NOT EXISTS quote_favs(user_id INTEGER, quote_id INTEGER, PRIMARY KEY(user_id, quote_id))")
    db.commit()
    _migrate_config(legacy_dir or d)


def _migrate_config(directory):
    """Один раз переносит старый config.json в базу."""
    if db.execute("SELECT COUNT(*) FROM guild_cfg").fetchone()[0]:
        return
    for folder in dict.fromkeys([directory, _state["dir"]]):
        path = os.path.join(folder, "config.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            log.exception("Не удалось прочитать %s", path)
            continue
        for gid, cfg in old.items():
            db.execute("INSERT OR REPLACE INTO guild_cfg(guild_id, data) VALUES(?,?)",
                       (int(gid), json.dumps(cfg, ensure_ascii=False)))
        db.commit()
        log.info("Настройки перенесены из %s в базу", path)
        return


# ====== НАСТРОЙКИ СЕРВЕРОВ ======
def gcfg(gid):
    gid = int(gid)
    cache = _state["cfg"]
    if gid not in cache:
        row = db.execute("SELECT data FROM guild_cfg WHERE guild_id=?", (gid,)).fetchone()
        cache[gid] = json.loads(row["data"]) if row else {}
    return dict(cache[gid])


def set_gcfg(gid, **kw):
    gid = int(gid)
    cfg = gcfg(gid)
    cfg.update(kw)
    db.execute("INSERT INTO guild_cfg(guild_id, data) VALUES(?,?) "
               "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
               (gid, json.dumps(cfg, ensure_ascii=False)))
    db.commit()
    _state["cfg"][gid] = cfg


# ====== ПОМОЩНИКИ ======
def now():
    return int(time.time())


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{max(m, 1)} мин"


def make_nick(dname, mc):
    """Формат: ник дискорда // ник майнкрафта (без @), максимум 32 символа."""
    sep = " // "
    mc = mc.replace("@", "").strip()
    mc = mc[:32 - len(sep) - 1]
    room = 32 - len(sep) - len(mc)
    return f"{dname.strip()[:room]}{sep}{mc}"


_MC_NICK = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def mc_nick_valid(nick):
    """Ник Minecraft Java: 3-16 символов, латиница, цифры и _."""
    return bool(_MC_NICK.match(nick or ""))


# ====== ОБРАЩЕНИЯ ======
def sub_by_id(sid):
    return db.execute("SELECT * FROM subs WHERE id=?", (sid,)).fetchone()


def sub_by_num(gid, kind, num):
    return db.execute("SELECT * FROM subs WHERE guild_id=? AND kind=? AND num=?", (gid, kind, num)).fetchone()


def create_sub(gid, kind, uid, topic, values, ts=None):
    num = db.execute("SELECT COALESCE(MAX(num),0)+1 FROM subs WHERE guild_id=? AND kind=?", (gid, kind)).fetchone()[0]
    cur = db.execute("INSERT INTO subs(guild_id,kind,author_id,topic,fields,created_ts,num) VALUES(?,?,?,?,?,?,?)",
                     (gid, kind, uid, topic, json.dumps(values, ensure_ascii=False), ts or now(), num))
    db.commit()
    return cur.lastrowid


def active_count(gid, uid, kind):
    return db.execute("SELECT COUNT(*) FROM subs WHERE guild_id=? AND author_id=? AND kind=? "
                      "AND status IN ('new','work')", (gid, uid, kind)).fetchone()[0]


def recent_count(gid, uid, since):
    """Сколько обращений пользователь создал с момента since (защита от спама)."""
    return db.execute("SELECT COUNT(*) FROM subs WHERE guild_id=? AND author_id=? AND created_ts>=?",
                      (gid, uid, since)).fetchone()[0]


def last_rejected_ts(gid, uid, kind):
    return db.execute("SELECT MAX(closed_ts) FROM subs WHERE guild_id=? AND author_id=? AND kind=? "
                      "AND status='rejected'", (gid, uid, kind)).fetchone()[0]


def nick_taken(gid, nick, exclude_uid):
    """Указан ли этот ник Minecraft в чужой живой или принятой заявке."""
    low = nick.strip().lower()
    rows = db.execute("SELECT fields FROM subs WHERE guild_id=? AND kind='app' AND author_id!=? "
                      "AND status IN ('new','work','approved')", (gid, exclude_uid)).fetchall()
    return any(json.loads(r["fields"]).get("nick", "").strip().lower() == low for r in rows)


def save_transcript(sid, text):
    db.execute("UPDATE subs SET transcript=? WHERE id=?", (text[:TRANSCRIPT_LIMIT], sid))
    db.commit()


def open_queue(gid, kinds=None, urgent_only=False, limit=15):
    q = "SELECT * FROM subs WHERE guild_id=? AND status IN ('new','work')"
    args = [gid]
    if kinds is not None:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if urgent_only:
        q += " AND priority=1"
    q += " ORDER BY priority DESC, created_ts ASC LIMIT ?"
    return db.execute(q, args + [limit]).fetchall()


# ====== СТАТИСТИКА ======
def leaderboard(gid, kind=None, since=0, limit=8):
    q = ("SELECT handler_id, COUNT(*) total, SUM(status='approved') ok, AVG(closed_ts-created_ts) avg "
         "FROM subs WHERE guild_id=? AND status IN ('approved','rejected') AND handler_id IS NOT NULL "
         "AND closed_ts>=?")
    args = [gid, since]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " GROUP BY handler_id ORDER BY total DESC, avg ASC LIMIT ?"
    return db.execute(q, args + [limit]).fetchall()


def totals(gid, kind=None, since=0):
    w, a = "guild_id=?", [gid]
    if kind:
        w += " AND kind=?"
        a.append(kind)
    created = db.execute(f"SELECT COUNT(*) FROM subs WHERE {w} AND created_ts>=?", a + [since]).fetchone()[0]
    h = db.execute(f"SELECT COUNT(*), SUM(status='approved'), AVG(closed_ts-created_ts) FROM subs "
                   f"WHERE {w} AND status IN ('approved','rejected') AND closed_ts>=?", a + [since]).fetchone()
    open_ = db.execute(f"SELECT COUNT(*) FROM subs WHERE {w} AND status IN ('new','work')", a).fetchone()[0]
    return dict(created=created, handled=h[0], approved=h[1] or 0, avg=h[2], open=open_)


def handler_rows(gid, uid):
    return db.execute("SELECT kind, status, closed_ts-created_ts AS dur FROM subs WHERE guild_id=? AND handler_id=?",
                      (gid, uid)).fetchall()


def open_counts(gid=None):
    """{'new': n, 'work': m} по всем серверам или по одному."""
    q, a = "SELECT status, COUNT(*) FROM subs WHERE status IN ('new','work')", []
    if gid:
        q += " AND guild_id=?"
        a.append(gid)
    out = {"new": 0, "work": 0}
    out.update({r[0]: r[1] for r in db.execute(q + " GROUP BY status", a)})
    return out


def db_size():
    path = os.path.join(_state["dir"], "data.db")
    return os.path.getsize(path) if os.path.exists(path) else 0


# ====== РЕЗЕРВНЫЕ КОПИИ ======
def backup_files():
    d = backup_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.startswith("data-") and f.endswith(".db"))


def backup_db(when=None):
    """Суточная копия базы в data/backups. Возвращает путь или None, если копия за сегодня уже есть."""
    d = backup_dir()
    os.makedirs(d, exist_ok=True)
    day = (when or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d")
    path = os.path.join(d, f"data-{day}.db")
    if os.path.exists(path):
        return None
    tmp = path + ".tmp"
    dest = sqlite3.connect(tmp)
    try:
        _state["conn"].backup(dest)
    finally:
        dest.close()
    os.replace(tmp, path)
    for old in backup_files()[:-BACKUP_KEEP]:
        os.remove(os.path.join(d, old))
    return path


# ====== ЦИТАТЫ ======
def add_quote(gid, channel_id, message_id, author_id, text, signature, style):
    cur = db.execute("INSERT OR IGNORE INTO quotes(guild_id,channel_id,message_id,author_id,text,signature,style,created_ts) "
                     "VALUES(?,?,?,?,?,?,?,?)", (gid, channel_id, message_id, author_id, text, signature, style, now()))
    db.commit()
    return cur.lastrowid


def quote_by_message(message_id):
    return db.execute("SELECT * FROM quotes WHERE message_id=?", (message_id,)).fetchone()


def add_fav(uid, qid):
    db.execute("INSERT OR IGNORE INTO quote_favs(user_id, quote_id) VALUES(?,?)", (uid, qid))
    db.commit()


def remove_fav(uid, qid):
    db.execute("DELETE FROM quote_favs WHERE user_id=? AND quote_id=?", (uid, qid))
    db.commit()


def favorites(uid, gid, limit=10):
    return db.execute("SELECT q.* FROM quotes q JOIN quote_favs f ON f.quote_id=q.id "
                      "WHERE f.user_id=? AND q.guild_id=? ORDER BY q.id DESC LIMIT ?", (uid, gid, limit)).fetchall()


def random_quote(gid):
    return db.execute("SELECT * FROM quotes WHERE guild_id=? ORDER BY RANDOM() LIMIT 1", (gid,)).fetchone()
