"""Shared persistent memory (SQLite): history, facts/context, devices."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    device_id TEXT,
    device_name TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT,
    agent TEXT,
    last_seen REAL NOT NULL
);
"""


class Memory:
    """Thread-safe SQLite access; async methods delegate to a worker thread."""

    def __init__(self, db_path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- sync primitives ----------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ---------- messages ----------
    def _add_message(self, session_id: str, role: str, content: str, device_id: str | None, device_name: str | None) -> dict:
        now = time.time()
        self._exec(
            "INSERT INTO sessions(id, created, updated) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET updated=excluded.updated",
            (session_id, now, now),
        )
        cur = self._exec(
            "INSERT INTO messages(session_id, role, content, device_id, device_name, created) VALUES(?,?,?,?,?,?)",
            (session_id, role, content, device_id, device_name, now),
        )
        return {
            "id": cur.lastrowid, "session_id": session_id, "role": role, "content": content,
            "device_id": device_id, "device_name": device_name, "created": now,
        }

    async def add_message(self, session_id: str, role: str, content: str,
                          device_id: str | None = None, device_name: str | None = None) -> dict:
        return await self._run(self._add_message, session_id, role, content, device_id, device_name)

    async def history(self, session_id: str, limit: int = 40) -> list[dict]:
        rows = await self._run(
            self._query,
            "SELECT * FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (session_id, limit),
        )
        return rows

    async def clear_session(self, session_id: str) -> None:
        await self._run(self._exec, "DELETE FROM messages WHERE session_id=?", (session_id,))

    # ---------- preferences (e.g. active language), shared by every device ----------
    def get_pref(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM prefs WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    async def set_pref(self, key: str, value: str) -> None:
        await self._run(
            self._exec,
            "INSERT INTO prefs(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- facts / context variables (global, shared across sessions) ----------
    async def set_fact(self, key: str, value: str) -> None:
        await self._run(
            self._exec,
            "INSERT INTO facts(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, value, time.time()),
        )

    async def delete_fact(self, key: str) -> bool:
        cur = await self._run(self._exec, "DELETE FROM facts WHERE key=?", (key,))
        return cur.rowcount > 0

    async def facts(self) -> dict[str, str]:
        rows = await self._run(self._query, "SELECT key, value FROM facts ORDER BY key", ())
        return {r["key"]: r["value"] for r in rows}

    # ---------- devices ----------
    async def upsert_device(self, device_id: str, name: str, platform: str, agent: str) -> None:
        await self._run(
            self._exec,
            "INSERT INTO devices(device_id, name, platform, agent, last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET name=excluded.name, platform=excluded.platform, "
            "agent=excluded.agent, last_seen=excluded.last_seen",
            (device_id, name, platform, agent, time.time()),
        )

    async def touch_device(self, device_id: str) -> None:
        await self._run(self._exec, "UPDATE devices SET last_seen=? WHERE device_id=?", (time.time(), device_id))

    async def devices(self) -> list[dict]:
        return await self._run(self._query, "SELECT * FROM devices ORDER BY last_seen DESC", ())
