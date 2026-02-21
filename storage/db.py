from __future__ import annotations

import aiosqlite
import json
import logging
import time
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    task_type TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    plan TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    token_usage TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    completed_at TEXT DEFAULT ''
)
"""

_CREATE_CONVERSATION_CONTEXT = """
CREATE TABLE IF NOT EXISTS conversation_context (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
)
"""

_CREATE_CONVERSATION_HISTORY = """
CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL
)
"""


async def init_db():
    """Create tables if they don't exist. Enables WAL mode for concurrent write safety."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(_CREATE_TASKS)
        await db.execute(_CREATE_CONVERSATION_CONTEXT)
        await db.execute(_CREATE_CONVERSATION_HISTORY)
        await db.commit()
    logger.info("Database initialized at %s (WAL mode)", config.DB_PATH)


async def create_task(task_id: str, user_id: int, message: str) -> dict:
    """Insert a new task record."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(
            "INSERT INTO tasks (id, user_id, message, created_at) VALUES (?, ?, ?, ?)",
            (task_id, user_id, message, now),
        )
        await db.commit()
    logger.info("Created task %s for user %d", task_id, user_id)
    return {"id": task_id, "user_id": user_id, "message": message, "status": "pending"}


async def update_task(task_id: str, **fields):
    """Update task fields. Valid fields: task_type, status, plan, result, error, token_usage, completed_at."""
    valid = {"task_type", "status", "plan", "result", "error", "token_usage", "completed_at"}
    updates = {k: v for k, v in fields.items() if k in valid}
    if not updates:
        return

    if "token_usage" in updates and isinstance(updates["token_usage"], dict):
        updates["token_usage"] = json.dumps(updates["token_usage"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]

    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        await db.commit()
    logger.info("Updated task %s: %s", task_id, list(updates.keys()))


async def get_task(task_id: str) -> dict | None:
    """Fetch a single task by ID."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_tasks(user_id: int, limit: int = 10) -> list[dict]:
    """Fetch recent tasks for a user."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ── Conversation context (key-value per user) ────────────────────────

async def set_context(user_id: int, key: str, value: str):
    """Upsert a conversation context key for a user."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(
            "INSERT INTO conversation_context (user_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (user_id, key, value, now),
        )
        await db.commit()


async def get_context(user_id: int, key: str) -> str | None:
    """Fetch a single context value."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        async with db.execute(
            "SELECT value FROM conversation_context WHERE user_id = ? AND key = ?",
            (user_id, key),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_all_context(user_id: int) -> dict[str, str]:
    """Fetch all context key-value pairs for a user."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        async with db.execute(
            "SELECT key, value FROM conversation_context WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}


async def delete_context(user_id: int, key: str):
    """Delete a single context key."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(
            "DELETE FROM conversation_context WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        await db.commit()


async def clear_context(user_id: int):
    """Delete all context for a user."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(
            "DELETE FROM conversation_context WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def clear_history(user_id: int):
    """Delete all conversation history for a user."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(
            "DELETE FROM conversation_history WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


# ── Conversation history (message log per user) ──────────────────────

async def add_history(user_id: int, role: str, content: str, task_id: str | None = None):
    """Append a message to conversation history."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        await db.execute(
            "INSERT INTO conversation_history (user_id, role, content, task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, role, content[:5000], task_id, now),
        )
        await db.commit()


async def get_recent_history(user_id: int, limit: int = 10) -> list[dict]:
    """Fetch recent conversation history for building context."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, task_id, created_at FROM conversation_history "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in reversed(rows)]


async def build_conversation_context(user_id: int, limit: int = 6) -> str:
    """Build a conversation context string from recent history.

    Returns a formatted string of recent exchanges for injecting into planner prompts.
    """
    history = await get_recent_history(user_id, limit=limit)
    if not history:
        return ""

    lines = []
    for msg in history:
        role_label = "User" if msg["role"] == "user" else "Agent"
        content = msg["content"][:500]
        lines.append(f"{role_label}: {content}")

    return "\n".join(lines)


# ── Crash recovery ───────────────────────────────────────────────────

async def recover_stale_tasks():
    """Reset tasks stuck in 'running' or 'pending' to 'crashed' on startup.

    After a kill -9 or unexpected termination, tasks may be left with
    status='running' even though no pipeline is active. This marks them
    as 'crashed' so /history shows the real reason.
    """
    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        cursor = await db.execute(
            "UPDATE tasks SET status = 'crashed', error = 'Process terminated before completion' "
            "WHERE status IN ('running', 'pending')"
        )
        await db.commit()
        if cursor.rowcount > 0:
            logger.info("Recovered %d stale task(s) from previous crash", cursor.rowcount)


# ── Storage auto-cleanup ──────────────────────────────────────────────

async def prune_old_data(history_days: int = 30, usage_days: int = 90):
    """Prune old conversation history and API usage records.

    Prevents unbounded storage growth on the Mac Mini SSD.
    """
    from datetime import timedelta
    history_cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).isoformat()
    usage_cutoff = time.time() - (usage_days * 86400)

    async with aiosqlite.connect(config.DB_PATH, timeout=20.0) as db:
        cursor = await db.execute(
            "DELETE FROM conversation_history WHERE created_at < ?", (history_cutoff,)
        )
        history_deleted = cursor.rowcount

        # api_usage table is in the same DB (created by claude_client.py)
        try:
            cursor = await db.execute(
                "DELETE FROM api_usage WHERE timestamp < ?", (usage_cutoff,)
            )
            usage_deleted = cursor.rowcount
        except Exception:
            usage_deleted = 0  # Table may not exist yet

        await db.commit()

    if history_deleted or usage_deleted:
        logger.info(
            "Storage cleanup: pruned %d history records (>%dd), %d usage records (>%dd)",
            history_deleted, history_days, usage_deleted, usage_days,
        )


def cleanup_workspace_files(max_age_days: int = 7):
    """Remove output and upload files older than max_age_days.

    Synchronous — called from bot startup or scheduled cleanup.
    """
    import time
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0

    for directory in [config.OUTPUTS_DIR, config.UPLOADS_DIR]:
        if not directory.exists():
            continue
        for f in directory.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass

    if removed:
        logger.info("Workspace cleanup: removed %d files older than %d days", removed, max_age_days)
