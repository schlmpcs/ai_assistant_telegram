"""This bot's own state: business connections, message history, owner
settings (style guide + few-shot examples + auto-reply), pending approval
drafts, manual-takeover tracking, and the corpus of learned style samples
ingested from the owner's Telegram export. Stored in a small SQLite file."""

from __future__ import annotations

import time
from typing import Optional

import aiosqlite

_db: Optional[aiosqlite.Connection] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id          TEXT PRIMARY KEY,         -- business_connection_id
    owner_id    INTEGER NOT NULL,
    is_enabled  INTEGER NOT NULL DEFAULT 1,
    can_reply   INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,         -- the contact's chat (= user id)
    role        TEXT NOT NULL,            -- 'user' | 'assistant'
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS owner_settings (
    owner_id        INTEGER PRIMARY KEY,
    style_prompt    TEXT,                 -- the active style guide
    style_examples  TEXT,                 -- few-shot example block injected into the prompt
    auto_reply      INTEGER               -- NULL = use global default
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    conn_id     TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,            -- 'reply'
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_activity (
    chat_id     INTEGER PRIMARY KEY,      -- the contact's chat the owner typed into
    last_at     INTEGER NOT NULL          -- unix seconds of their last manual message
);

CREATE TABLE IF NOT EXISTS style_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    incoming    TEXT,                 -- the message the owner was replying to (may be NULL)
    reply       TEXT NOT NULL,        -- the owner's own message
    length      INTEGER NOT NULL,
    has_emoji   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_samples_owner ON style_samples(owner_id);
"""


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(__path())
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()


def __path() -> str:
    from config import settings
    return settings.local_db_path


async def close() -> None:
    if _db:
        await _db.close()


def _now() -> int:
    return int(time.time())


# ── connections ────────────────────────────────────────────────────────────
async def upsert_connection(conn_id: str, owner_id: int, is_enabled: bool,
                            can_reply: bool) -> None:
    await _db.execute(
        """INSERT INTO connections (id, owner_id, is_enabled, can_reply, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             is_enabled=excluded.is_enabled,
             can_reply=excluded.can_reply,
             updated_at=excluded.updated_at""",
        (conn_id, owner_id, int(is_enabled), int(can_reply), _now()),
    )
    await _db.commit()


async def get_connection(conn_id: str) -> Optional[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM connections WHERE id=?", (conn_id,))
    return await cur.fetchone()


async def get_owner_connection(owner_id: int) -> Optional[aiosqlite.Row]:
    """The active connection for an owner."""
    cur = await _db.execute(
        "SELECT * FROM connections WHERE owner_id=? AND is_enabled=1 AND can_reply=1 "
        "ORDER BY updated_at DESC LIMIT 1",
        (owner_id,),
    )
    return await cur.fetchone()


# ── message history ────────────────────────────────────────────────────────
async def add_message(chat_id: int, role: str, text: str) -> None:
    await _db.execute(
        "INSERT INTO messages (chat_id, role, text, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, text, _now()),
    )
    await _db.commit()


async def get_history(chat_id: int, limit: int = 10) -> list[dict]:
    cur = await _db.execute(
        "SELECT role, text FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]


async def count_recent_user_messages(
    chat_id: int, minute_cutoff: int, hour_cutoff: int
) -> tuple[int, int]:
    """(# contact messages in the last minute, # in the last hour) — abuse guard.

    `minute_cutoff` / `hour_cutoff` are unix-second thresholds (now-60, now-3600).
    """
    cur = await _db.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN created_at > ? THEN 1 ELSE 0 END), 0) AS per_min,
             COUNT(*) AS per_hour
           FROM messages
           WHERE chat_id=? AND role='user' AND created_at > ?""",
        (minute_cutoff, chat_id, hour_cutoff),
    )
    row = await cur.fetchone()
    return int(row["per_min"]), int(row["per_hour"])


# ── owner settings ─────────────────────────────────────────────────────────
async def get_owner(owner_id: int) -> Optional[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM owner_settings WHERE owner_id=?", (owner_id,)
    )
    return await cur.fetchone()


async def set_style_prompt(owner_id: int, prompt: str) -> None:
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, style_prompt) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET style_prompt=excluded.style_prompt""",
        (owner_id, prompt),
    )
    await _db.commit()


async def seed_style_prompt(owner_id: int, prompt: str) -> None:
    """Set the owner's style ONLY if they haven't got one yet.

    Lets us ship a sensible default voice as the *active* per-owner style on
    first run, without clobbering a style learned/set later.
    """
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, style_prompt) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET style_prompt=excluded.style_prompt
           WHERE owner_settings.style_prompt IS NULL""",
        (owner_id, prompt),
    )
    await _db.commit()


async def set_style_examples(owner_id: int, examples_text: str) -> None:
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, style_examples) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET style_examples=excluded.style_examples""",
        (owner_id, examples_text),
    )
    await _db.commit()


async def set_style_profile(owner_id: int, guide: str, examples: str) -> None:
    """Upsert both the learned style guide and the few-shot example block."""
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, style_prompt, style_examples)
           VALUES (?, ?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET
             style_prompt=excluded.style_prompt,
             style_examples=excluded.style_examples""",
        (owner_id, guide, examples),
    )
    await _db.commit()


async def set_auto_reply(owner_id: int, value: bool) -> None:
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, auto_reply) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET auto_reply=excluded.auto_reply""",
        (owner_id, int(value)),
    )
    await _db.commit()


# ── drafts (approval flow) ─────────────────────────────────────────────────
async def create_draft(owner_id: int, conn_id: str, chat_id: int,
                       kind: str, text: str) -> int:
    cur = await _db.execute(
        """INSERT INTO drafts (owner_id, conn_id, chat_id, kind, text, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (owner_id, conn_id, chat_id, kind, text, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def get_draft(draft_id: int) -> Optional[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,))
    return await cur.fetchone()


async def update_draft_text(draft_id: int, text: str) -> None:
    await _db.execute("UPDATE drafts SET text=? WHERE id=?", (text, draft_id))
    await _db.commit()


async def delete_draft(draft_id: int) -> None:
    await _db.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
    await _db.commit()


# ── manual takeover ────────────────────────────────────────────────────────
async def record_manager_activity(chat_id: int) -> None:
    """Note that the owner just messaged this contact by hand — starts (or
    resets) the quiet window during which the bot stays out of the chat."""
    await _db.execute(
        """INSERT INTO manager_activity (chat_id, last_at) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET last_at=excluded.last_at""",
        (chat_id, _now()),
    )
    await _db.commit()


async def manager_active_within(chat_id: int, hours: int) -> bool:
    """True if the owner hand-messaged this contact within the last `hours`."""
    cutoff = _now() - hours * 3600
    cur = await _db.execute(
        "SELECT 1 FROM manager_activity WHERE chat_id=? AND last_at>? LIMIT 1",
        (chat_id, cutoff),
    )
    return await cur.fetchone() is not None


# ── style samples (ingested from the Telegram export) ──────────────────────
async def clear_style_samples(owner_id: int) -> None:
    await _db.execute("DELETE FROM style_samples WHERE owner_id=?", (owner_id,))
    await _db.commit()


async def add_style_sample(owner_id: int, incoming: Optional[str], reply: str,
                           length: int, has_emoji: bool) -> None:
    await _db.execute(
        """INSERT INTO style_samples (owner_id, incoming, reply, length, has_emoji)
           VALUES (?, ?, ?, ?, ?)""",
        (owner_id, incoming, reply, int(length), int(bool(has_emoji))),
    )
    await _db.commit()


async def count_style_samples(owner_id: int) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) AS n FROM style_samples WHERE owner_id=?", (owner_id,)
    )
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def get_style_samples(owner_id: int, limit: Optional[int] = None) -> list[dict]:
    if limit is not None:
        cur = await _db.execute(
            "SELECT incoming, reply, length, has_emoji FROM style_samples "
            "WHERE owner_id=? ORDER BY id LIMIT ?",
            (owner_id, limit),
        )
    else:
        cur = await _db.execute(
            "SELECT incoming, reply, length, has_emoji FROM style_samples "
            "WHERE owner_id=? ORDER BY id",
            (owner_id,),
        )
    rows = await cur.fetchall()
    return [
        {
            "incoming": r["incoming"],
            "reply": r["reply"],
            "length": r["length"],
            "has_emoji": bool(r["has_emoji"]),
        }
        for r in rows
    ]
