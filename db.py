"""SQLite-хранилище: клиенты из всех каналов, история, заявки, переписка с менеджером.

Клиент определяется ключом «канал:id»:
  tg:123456789        — Telegram chat id
  wa:77011234567      — номер WhatsApp (без «+»)
  ig:1789...          — Instagram-scoped ID
"""
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH, HUMAN_TIMEOUT_HOURS


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                client_key  TEXT PRIMARY KEY,
                channel     TEXT NOT NULL,              -- tg | wa | ig
                ext_id      TEXT NOT NULL,
                name        TEXT,
                username    TEXT,
                mode        TEXT NOT NULL DEFAULT 'ai', -- ai | human (с клиентом говорит менеджер)
                mode_since  INTEGER,
                reset_after INTEGER NOT NULL DEFAULT 0, -- /reset: сообщения с id <= этого ИИ не видит
                first_seen  INTEGER NOT NULL,
                last_seen   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                client_key TEXT NOT NULL,
                role       TEXT NOT NULL,                -- user | assistant
                content    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_client ON messages(client_key, id);
            CREATE TABLE IF NOT EXISTS leads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                client_key TEXT NOT NULL,
                channel    TEXT NOT NULL,
                name       TEXT,
                username   TEXT,
                details    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            -- какое сообщение у админа относится к какому клиенту:
            -- админ отвечает (Reply) на карточку — ответ уходит этому клиенту
            CREATE TABLE IF NOT EXISTS relay (
                chat_id    INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                client_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            -- служебные значения: например, продлённый токен Instagram
            CREATE TABLE IF NOT EXISTS kv (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> int:
    return int(time.time())


# ---------- клиенты ----------

def upsert_user(client_key: str, channel: str, ext_id: str,
                name: str = "", username: str = "") -> None:
    now = _now()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO users (client_key, channel, ext_id, name, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_key) DO UPDATE SET
                name      = COALESCE(NULLIF(excluded.name, ''), users.name),
                username  = COALESCE(NULLIF(excluded.username, ''), users.username),
                last_seen = excluded.last_seen
            """,
            (client_key, channel, ext_id, name or "", username or "", now, now),
        )


def get_user(client_key: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE client_key = ?", (client_key,)).fetchone()


def get_mode(client_key: str) -> str:
    row = get_user(client_key)
    if not row or row["mode"] != "human":
        return "ai"
    # менеджер забыл вернуть клиента ИИ — возвращаем сами, чтобы клиент не завис
    if HUMAN_TIMEOUT_HOURS and _now() - (row["mode_since"] or 0) > HUMAN_TIMEOUT_HOURS * 3600:
        set_mode(client_key, "ai")
        return "ai"
    return "human"


def set_mode(client_key: str, mode: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET mode = ?, mode_since = ? WHERE client_key = ?",
                  (mode, _now(), client_key))


def reset_history(client_key: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET reset_after = (SELECT COALESCE(MAX(id), 0) FROM messages) "
            "WHERE client_key = ?",
            (client_key,),
        )


# ---------- история ----------

def add_message(client_key: str, role: str, content: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO messages (client_key, role, content, created_at) VALUES (?, ?, ?, ?)",
            (client_key, role, content, _now()),
        )


def get_history(client_key: str, limit: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT m.role, m.content FROM messages m
            JOIN users u ON u.client_key = m.client_key
            WHERE m.client_key = ? AND m.id > u.reset_after
            ORDER BY m.id DESC LIMIT ?
            """,
            (client_key, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def user_questions_last_24h(client_key: str) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM messages WHERE client_key = ? AND role = 'user' AND created_at > ?",
            (client_key, _now() - 86400),
        ).fetchone()[0]


def last_inbound_at(client_key: str) -> int | None:
    """Когда клиент писал последний раз. Нужно для окна 24 часа в WhatsApp и Instagram."""
    with _conn() as c:
        return c.execute(
            "SELECT MAX(created_at) FROM messages WHERE client_key = ? AND role = 'user'",
            (client_key,),
        ).fetchone()[0]


# ---------- заявки ----------

def add_lead(client_key: str, channel: str, name: str, username: str, details: str) -> bool:
    """False — такая же заявка от этого клиента уже была за последний час."""
    with _conn() as c:
        dup = c.execute(
            "SELECT 1 FROM leads WHERE client_key = ? AND details = ? AND created_at > ?",
            (client_key, details, _now() - 3600),
        ).fetchone()
        if dup:
            return False
        c.execute(
            "INSERT INTO leads (client_key, channel, name, username, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (client_key, channel, name or "", username or "", details, _now()),
        )
        return True


def lead_exists_since(client_key: str, since: int) -> bool:
    """Была ли заявка от клиента после момента since — для проверки пропущенных заявок."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM leads WHERE client_key = ? AND created_at >= ?", (client_key, since)
        ).fetchone() is not None


def all_leads() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()


# ---------- переписка через менеджера ----------

def save_relay(chat_id: int, message_id: int, client_key: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO relay (chat_id, message_id, client_key, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, message_id, client_key, _now()),
        )


def find_relay(chat_id: int, message_id: int) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT client_key FROM relay WHERE chat_id = ? AND message_id = ?",
                        (chat_id, message_id)).fetchone()
    return row["client_key"] if row else None


# ---------- служебные значения ----------

def kv_get(key: str) -> tuple[str, int] | None:
    with _conn() as c:
        row = c.execute("SELECT value, updated_at FROM kv WHERE key = ?", (key,)).fetchone()
    return (row["value"], row["updated_at"]) if row else None


def kv_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                  (key, value, _now()))


# ---------- статистика ----------

def stats() -> dict:
    day, week = _now() - 86400, _now() - 7 * 86400
    with _conn() as c:
        def q(sql, *a):
            return c.execute(sql, a).fetchone()[0]
        by_channel = {r["channel"]: r["n"] for r in c.execute(
            "SELECT channel, COUNT(*) AS n FROM users GROUP BY channel")}
        return {
            "users": q("SELECT COUNT(*) FROM users"),
            "by_channel": by_channel,
            "users_day": q("SELECT COUNT(*) FROM users WHERE last_seen > ?", day),
            "questions_day": q("SELECT COUNT(*) FROM messages WHERE role='user' AND created_at > ?", day),
            "leads": q("SELECT COUNT(*) FROM leads"),
            "leads_week": q("SELECT COUNT(*) FROM leads WHERE created_at > ?", week),
            "with_manager": q("SELECT COUNT(*) FROM users WHERE mode='human'"),
        }
