"""Persistent proposal store for the Poke MCP bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import Lock
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_davey_root() -> Path:
    configured = os.getenv("DAVEY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if any(os.getenv(name) for name in ("FLY_APP_NAME", "FLY_MACHINE_ID", "FLY_REGION")):
        return Path("/app")
    return Path.cwd().resolve()


def _warn(message: str) -> None:
    print(f"proposal_store warning: {message}", file=sys.stderr, flush=True)


class ProposalStore:
    """SQLite-backed proposal store to persist trade intents across server restarts."""

    _instance = None
    _initialized_global = False
    _fallback_proposals: dict[str, dict[str, Any]] = {}

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ProposalStore, cls).__new__(cls)
        return cls._instance

    def __init__(
        self,
        *,
        davey_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        if ProposalStore._initialized_global:
            return
            
        root = (
            Path(davey_root).expanduser().resolve()
            if davey_root is not None
            else _default_davey_root()
        )
        # Fix: use /data on Fly if available
        fly_data = Path("/data")
        if fly_data.exists():
            root = fly_data

        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else root / "state" / "proposals.db"
        )
        self._lock = Lock()
        self._db_available = True
        self._initialized = False
        self._warned_fallback = False
        self._ensure_db()
        ProposalStore._initialized_global = True

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_db(self) -> None:
        if self._initialized and self._db_available:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS proposals (
                        handoff_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        candidate_json TEXT NOT NULL,
                        proposal_payload_json TEXT NOT NULL,
                        intent_json TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
            self._db_available = True
            self._initialized = True
        except sqlite3.Error as exc:
            self._db_available = False
            _warn(f"SQLite unavailable: {exc}")

    def save_proposal(
        self,
        handoff_id: str,
        session_id: str,
        candidate: dict[str, Any],
        proposal_payload: dict[str, Any],
        intent_json: str | None = None,
    ) -> None:
        with self._lock:
            # Always update global fallback
            ProposalStore._fallback_proposals[handoff_id] = {
                "session_id": session_id,
                "candidate": candidate,
                "proposal_payload": proposal_payload,
                "intent_json": intent_json,
            }
            if not self._db_available:
                return
            self._ensure_db()
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO proposals 
                        (handoff_id, session_id, candidate_json, proposal_payload_json, intent_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            handoff_id,
                            session_id,
                            json.dumps(candidate),
                            json.dumps(proposal_payload),
                            intent_json,
                            _utc_now_iso(),
                        ),
                    )
            except sqlite3.Error as exc:
                self._db_available = False
                _warn(f"Save failed: {exc}")

    def get_proposal(self, handoff_id: str) -> dict[str, Any] | None:
        with self._lock:
            # Check DB first
            if self._db_available:
                self._ensure_db()
                try:
                    with self._connect() as conn:
                        row = conn.execute(
                            """
                            SELECT session_id, candidate_json, proposal_payload_json, intent_json 
                            FROM proposals WHERE handoff_id = ?
                            """,
                            (handoff_id,),
                        ).fetchone()
                    if row:
                        return {
                            "session_id": row[0],
                            "candidate": json.loads(row[1]),
                            "proposal_payload": json.loads(row[2]),
                            "intent_json": row[3],
                        }
                except sqlite3.Error as exc:
                    self._db_available = False
                    _warn(f"Lookup failed: {exc}")
            
            # Fallback to global in-memory
            return ProposalStore._fallback_proposals.get(handoff_id)
