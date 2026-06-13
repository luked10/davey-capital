"""Persistent seen-handoff id store for the Poke MCP bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from threading import Lock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _default_davey_root() -> Path:
    configured = os.getenv("DAVEY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd().resolve()


class SeenIdsStore:
    """SQLite-backed seen id store with in-memory fallback on DB failures."""

    def __init__(
        self,
        *,
        davey_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        root = Path(davey_root).expanduser().resolve() if davey_root is not None else _default_davey_root()
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else root / "state" / "seen_ids.db"
        )
        self._lock = Lock()
        self._fallback_seen: set[str] = set()
        self._db_available = True
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_db(self) -> None:
        if self._initialized and self._db_available:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seen_ids (
                        handoff_id TEXT PRIMARY KEY,
                        seen_at TEXT NOT NULL
                    )
                    """
                )
            self._db_available = True
            self._initialized = True
        except sqlite3.Error:
            self._db_available = False

    def mark_seen(self, handoff_id: str) -> None:
        clean_id = _clean_text(handoff_id)
        if not clean_id:
            return
        with self._lock:
            self._fallback_seen.add(clean_id)
            if not self._db_available:
                return
            self._ensure_db()
            if not self._db_available:
                return
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO seen_ids (handoff_id, seen_at)
                        VALUES (?, ?)
                        """,
                        (clean_id, _utc_now_iso()),
                    )
            except sqlite3.Error:
                self._db_available = False

    def is_seen(self, handoff_id: str) -> bool:
        clean_id = _clean_text(handoff_id)
        if not clean_id:
            return False
        with self._lock:
            if clean_id in self._fallback_seen:
                return True
            if not self._db_available:
                return False
            self._ensure_db()
            if not self._db_available:
                return clean_id in self._fallback_seen
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT 1 FROM seen_ids WHERE handoff_id = ? LIMIT 1",
                        (clean_id,),
                    ).fetchone()
                return row is not None
            except sqlite3.Error:
                self._db_available = False
                return clean_id in self._fallback_seen

