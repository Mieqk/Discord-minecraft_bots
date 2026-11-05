import json, os, sqlite3, datetime


def test_make_nick_limits(store):
    assert store.make_nick("carburadiator", "Steve") == "carburadiator // Steve"
    nick = store.make_nick("a" * 40, "b" * 40)
    assert len(nick) <= 32 and " // " in nick
    assert "@" not in store.make_nick("x", "@Alex")


def test_fmt_dur(store):
    assert store.fmt_dur(None) == "-"
    assert store.fmt_dur(10) == "1 мин"
    assert store.fmt_dur(3600 + 5 * 60) == "1 ч 5 мин"
    assert store.fmt_dur(2 * 86400 + 3 * 3600) == "2 д 3 ч"


def test_mc_nick_valid(store):
    assert store.mc_nick_valid("Steve_1")
    assert not store.mc_nick_valid("ab")
    assert not store.mc_nick_valid("с кириллицей")
    assert not store.mc_nick_valid("x" * 17)


def test_config_roundtrip_and_cache(store):
    assert store.gcfg(1) == {}
    store.set_gcfg(1, a=1)
    store.set_gcfg(1, b=2)
    assert store.gcfg(1) == {"a": 1, "b": 2}
    store._state["cfg"].clear()  # читаем уже из базы
    assert store.gcfg(1) == {"a": 1, "b": 2}


def test_legacy_migration(tmp_path):
    import store as s
    legacy = tmp_path / "old"
    legacy.mkdir()
    (legacy / "config.json").write_text(json.dumps({"42": {"staff_role_id": 7}}), encoding="utf-8")
    c = sqlite3.connect(legacy / "data.db")
    c.execute("CREATE TABLE subs(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, kind TEXT, author_id INTEGER, "
              "topic TEXT, fields TEXT, status TEXT DEFAULT 'new', handler_id INTEGER, reason TEXT, channel_id INTEGER, "
              "message_id INTEGER, ticket_channel_id INTEGER, ticket_msg_id INTEGER, created_ts INTEGER, closed_ts INTEGER, "
              "pinged INTEGER DEFAULT 0)")
    c.execute("INSERT INTO subs(guild_id,kind,author_id,topic,fields,created_ts) VALUES(42,'app',1,'t','{}',1)")
    c.commit(); c.close()
    s.init(str(tmp_path / "data"), legacy_dir=str(legacy))
    assert s.gcfg(42) == {"staff_role_id": 7}
    row = s.db.execute("SELECT num, priority FROM subs").fetchone()
    assert row["num"] == 1 and row["priority"] == 0       # старая база получила новые столбцы
    assert (legacy / "data.db").exists()                  # исходные файлы не тронуты


def test_create_sub_numbers_and_limits(store):
    a = store.create_sub(1, "app", 10, "t", {"nick": "Steve", "age": "20"})
    b = store.create_sub(1, "app", 11, "t", {"nick": "Alex", "age": "20"})
    c = store.create_sub(1, "bug", 10, "t", {})
    assert [store.sub_by_id(i)["num"] for i in (a, b, c)] == [1, 2, 1]
    assert store.active_count(1, 10, "app") == 1
    assert store.recent_count(1, 10, 0) == 2
    assert store.nick_taken(1, "STEVE", 11) is True       # без учёта регистра
    assert store.nick_taken(1, "Steve", 10) is False      # свой же ник не считается


def test_reapply_and_queue(store):
    sid = store.create_sub(1, "app", 10, "t", {})
    store.db.execute("UPDATE subs SET status='rejected', closed_ts=500 WHERE id=?", (sid,))
    assert store.last_rejected_ts(1, 10, "app") == 500
    u = store.create_sub(1, "bug", 11, "urgent", {})
    store.create_sub(1, "report", 12, "normal", {})
    store.db.execute("UPDATE subs SET priority=1 WHERE id=?", (u,))
    rows = store.open_queue(1)
    assert [r["topic"] for r in rows] == ["urgent", "normal"]  # срочные первыми, закрытых нет
    assert [r["topic"] for r in store.open_queue(1, urgent_only=True)] == ["urgent"]
    assert [r["topic"] for r in store.open_queue(1, kinds=["report"])] == ["normal"]


def test_stats(store):
    for dur, status in ((60, "approved"), (120, "rejected")):
        sid = store.create_sub(1, "app", 10, "t", {}, ts=1000)
        store.db.execute("UPDATE subs SET status=?, handler_id=5, closed_ts=? WHERE id=?", (status, 1000 + dur, sid))
    store.db.commit()
    board = store.leaderboard(1)
    assert board[0]["handler_id"] == 5 and board[0]["total"] == 2 and board[0]["ok"] == 1
    t = store.totals(1)
    assert t["handled"] == 2 and t["approved"] == 1 and t["avg"] == 90


def test_transcript_saved_and_truncated(store):
    sid = store.create_sub(1, "app", 10, "t", {})
    store.save_transcript(sid, "x" * (store.TRANSCRIPT_LIMIT + 50))
    assert len(store.sub_by_id(sid)["transcript"]) == store.TRANSCRIPT_LIMIT


def test_backup_daily_and_rotation(store):
    day = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    store.create_sub(1, "app", 10, "t", {})
    first = store.backup_db(day)
    assert first and os.path.exists(first)
    assert store.backup_db(day) is None                   # за этот день копия уже есть
    for n in range(1, 10):
        store.backup_db(day + datetime.timedelta(days=n))
    assert len(store.backup_files()) == store.BACKUP_KEEP
    copy = sqlite3.connect(os.path.join(store.backup_dir(), store.backup_files()[-1]))
    assert copy.execute("SELECT COUNT(*) FROM subs").fetchone()[0] == 1   # копия читается и не пустая
    copy.close()


def test_quotes_and_favorites(store):
    store.add_quote(1, 2, 3, 4, "текст", "@x", "cream")
    q = store.quote_by_message(3)
    store.add_fav(99, q["id"]); store.add_fav(99, q["id"])
    assert len(store.favorites(99, 1)) == 1
    store.remove_fav(99, q["id"])
    assert store.favorites(99, 1) == []
    assert store.random_quote(1)["text"] == "текст"
