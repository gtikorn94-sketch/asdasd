"""SQLite-хранилище: анкеты, мнения (votes) и батлы."""
import os
import time

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id   INTEGER PRIMARY KEY,
    username  TEXT,
    name      TEXT,
    grp       TEXT,
    course    INTEGER,
    self_type TEXT,
    ai_score  REAL,
    ai_type   TEXT,
    metrics   TEXT,
    photos    TEXT,
    elo       REAL DEFAULT 1000,
    battles   INTEGER DEFAULT 0,
    wins      INTEGER DEFAULT 0,
    created   REAL
);
CREATE TABLE IF NOT EXISTS votes(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    voter     INTEGER,
    target    INTEGER,
    vote_type TEXT,      -- NULL = пропустил
    agree     INTEGER,   -- 1 = согласен с самооценкой
    comment   TEXT,
    created   REAL,
    UNIQUE(voter, target)
);
CREATE TABLE IF NOT EXISTS battles(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    a       INTEGER,
    b       INTEGER,
    winner  INTEGER,
    voter   INTEGER,
    created REAL
);
"""


async def init():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as con:
        await con.executescript(SCHEMA)
        await con.commit()


async def _fetch(sql, args=(), one=False):
    async with aiosqlite.connect(DB_PATH) as con:
        con.row_factory = aiosqlite.Row
        cur = await con.execute(sql, args)
        rows = [dict(r) for r in await cur.fetchall()]
    if one:
        return rows[0] if rows else None
    return rows


async def _exec(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as con:
        await con.execute(sql, args)
        await con.commit()


# ---------- анкеты ----------
async def get_user(user_id):
    return await _fetch("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)


async def save_user(user_id, username, name, grp, course, self_type,
                    ai_score, ai_type, metrics_json, photos_json):
    async with aiosqlite.connect(DB_PATH) as con:
        await con.execute(
            """INSERT INTO users(user_id, username, name, grp, course, self_type,
                                 ai_score, ai_type, metrics, photos, created)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, name=excluded.name, grp=excluded.grp,
                 course=excluded.course, self_type=excluded.self_type,
                 ai_score=excluded.ai_score, ai_type=excluded.ai_type,
                 metrics=excluded.metrics, photos=excluded.photos""",
            (user_id, username, name, grp, course, self_type,
             ai_score, ai_type, metrics_json, photos_json, time.time()),
        )
        # фото/самооценка изменились - старые мнения о нём уже неактуальны
        await con.execute("DELETE FROM votes WHERE target=?", (user_id,))
        await con.commit()


async def delete_user(user_id):
    async with aiosqlite.connect(DB_PATH) as con:
        await con.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await con.execute("DELETE FROM votes WHERE voter=? OR target=?", (user_id, user_id))
        await con.execute("DELETE FROM battles WHERE a=? OR b=? OR voter=?", (user_id, user_id, user_id))
        await con.commit()


# ---------- просмотр анкет и мнения ----------
async def next_profile(viewer):
    return await _fetch(
        """SELECT * FROM users
           WHERE user_id != ?
             AND user_id NOT IN (SELECT target FROM votes WHERE voter = ?)
           ORDER BY RANDOM() LIMIT 1""",
        (viewer, viewer), one=True)


async def save_vote(voter, target, vote_type, agree):
    await _exec(
        """INSERT INTO votes(voter, target, vote_type, agree, created)
           VALUES(?,?,?,?,?)
           ON CONFLICT(voter, target) DO UPDATE SET
             vote_type=excluded.vote_type, agree=excluded.agree, created=excluded.created""",
        (voter, target, vote_type, agree, time.time()))


async def set_comment(voter, target, comment):
    await _exec("UPDATE votes SET comment=? WHERE voter=? AND target=?",
                (comment, voter, target))


async def consensus(target):
    rows = await _fetch(
        """SELECT vote_type, COUNT(*) AS c FROM votes
           WHERE target=? AND vote_type IS NOT NULL
           GROUP BY vote_type ORDER BY c DESC""", (target,))
    agree = await _fetch(
        "SELECT COUNT(*) AS c FROM votes WHERE target=? AND agree=1", (target,), one=True)
    total = sum(r["c"] for r in rows)
    return {"total": total, "agree": agree["c"] if agree else 0,
            "types": [(r["vote_type"], r["c"]) for r in rows]}


async def comments(target, limit=5):
    return await _fetch(
        """SELECT vote_type, agree, comment FROM votes
           WHERE target=? AND comment IS NOT NULL
           ORDER BY created DESC LIMIT ?""", (target, limit))


# ---------- батлы ----------
async def random_pair(voter):
    return await _fetch(
        "SELECT * FROM users WHERE user_id != ? ORDER BY RANDOM() LIMIT 2", (voter,))


async def record_battle(a, b, winner, voter, k=32):
    async with aiosqlite.connect(DB_PATH) as con:
        con.row_factory = aiosqlite.Row
        ra = (await (await con.execute("SELECT elo FROM users WHERE user_id=?", (a,))).fetchone())
        rb = (await (await con.execute("SELECT elo FROM users WHERE user_id=?", (b,))).fetchone())
        if not ra or not rb:
            return None
        ea, eb = ra["elo"], rb["elo"]
        exp_a = 1 / (1 + 10 ** ((eb - ea) / 400))
        sa = 1.0 if winner == a else 0.0
        na = ea + k * (sa - exp_a)
        nb = eb + k * ((1 - sa) - (1 - exp_a))
        await con.execute("UPDATE users SET elo=?, battles=battles+1, wins=wins+? WHERE user_id=?",
                          (na, 1 if winner == a else 0, a))
        await con.execute("UPDATE users SET elo=?, battles=battles+1, wins=wins+? WHERE user_id=?",
                          (nb, 1 if winner == b else 0, b))
        await con.execute("INSERT INTO battles(a,b,winner,voter,created) VALUES(?,?,?,?,?)",
                          (a, b, winner, voter, time.time()))
        await con.commit()
        return round(na), round(nb)


# ---------- топы ----------
async def top_ai(n=10):
    return await _fetch("SELECT * FROM users ORDER BY ai_score DESC LIMIT ?", (n,))


async def top_elo(n=10):
    return await _fetch("SELECT * FROM users WHERE battles > 0 ORDER BY elo DESC LIMIT ?", (n,))


async def count_users():
    r = await _fetch("SELECT COUNT(*) AS c FROM users", one=True)
    return r["c"]
