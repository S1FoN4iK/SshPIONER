from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class State:

    def __init__(self, path: str) -> None:
        self._path = str(path)
        self._lock = asyncio.Lock()
        self._init_db()
        self._maybe_migrate_json()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        parent = Path(self._path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    username     TEXT PRIMARY KEY,
                    bootstrapped INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS seen (
                    username  TEXT NOT NULL,
                    video_id  TEXT NOT NULL,
                    seen_at   INTEGER NOT NULL,
                    PRIMARY KEY (username, video_id)
                );
                """
            )

    def _maybe_migrate_json(self) -> None:
        """Import an old state.json sitting next to the db, but only into an empty db."""
        legacy = Path(self._path).with_suffix(".json")
        if not legacy.exists():
            return
        with closing(self._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            if count:
                return
            try:
                data = json.loads(legacy.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            profiles = (data or {}).get("profiles") or {}
            if not profiles:
                return
            now = int(time.time())
            with conn:
                for username, bucket in profiles.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO profiles(username, bootstrapped) "
                        "VALUES(?, ?)",
                        (username, 1 if (bucket or {}).get("bootstrapped") else 0),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO seen(username, video_id, seen_at) "
                        "VALUES(?, ?, ?)",
                        [(username, vid, now) for vid in (bucket or {}).get("seen", [])],
                    )

    @staticmethod
    def _touch_profile(conn: sqlite3.Connection, username: str) -> None:
        conn.execute(
            "INSERT INTO profiles(username, bootstrapped) VALUES(?, 1) "
            "ON CONFLICT(username) DO UPDATE SET bootstrapped = 1",
            (username,),
        )

    async def is_bootstrapped(self, profile: str) -> bool:
        def _q() -> bool:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT bootstrapped FROM profiles WHERE username = ?",
                    (profile,),
                ).fetchone()
                return bool(row and row[0])

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def seed(self, profile: str, ids: list[str]) -> None:
        now = int(time.time())

        def _w() -> None:
            with closing(self._connect()) as conn, conn:
                self._touch_profile(conn, profile)
                conn.executemany(
                    "INSERT OR IGNORE INTO seen(username, video_id, seen_at) "
                    "VALUES(?, ?, ?)",
                    [(profile, vid, now) for vid in ids],
                )

        async with self._lock:
            await asyncio.to_thread(_w)

    async def is_seen(self, profile: str, video_id: str) -> bool:
        def _q() -> bool:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT 1 FROM seen WHERE username = ? AND video_id = ?",
                    (profile, video_id),
                ).fetchone()
                return row is not None

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def mark_seen(self, profile: str, video_id: str) -> None:
        now = int(time.time())

        def _w() -> None:
            with closing(self._connect()) as conn, conn:
                self._touch_profile(conn, profile)
                conn.execute(
                    "INSERT OR IGNORE INTO seen(username, video_id, seen_at) "
                    "VALUES(?, ?, ?)",
                    (profile, video_id, now),
                )

        async with self._lock:
            await asyncio.to_thread(_w)