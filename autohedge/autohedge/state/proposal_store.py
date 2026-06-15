"""Persistent proposal store for the Poke MCP bridge.

Sonnet-generated ExecutionIntent proposals must survive a machine restart
between ``submit_triage_decision`` (write) and ``record_approval_decision``
(read). Keeping them only in a process-memory dict loses them whenever Fly
restarts the machine on deploy or idle, which then blocks the approval with
"no proposal found for handoff_id". This store backs the proposals on the Fly
volume (``DAVEY_ROOT/state/proposals.db``) so the repo/volume stays the source
of truth and nothing important lives only in RAM.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _default_davey_root() -> Path:
    configured = os.getenv("DAVEY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if any(os.getenv(name) for name in ("FLY_APP_NAME", "FLY_MACHINE_ID", "FLY_REGION")):
        return Path("/app")
    return Path.cwd().resolve()


class ProposalStore:
    """SQLite-backed store for triage proposals keyed by handoff_id."""

    def __init__(
        self,
        *,
        davey_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        root = (
            Path(davey_root).expanduser().resolve()
            if davey_root is not None
            else _default_davey_root()
        )
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else root / "state" / "proposals.db"
        )
        self._lock = Lock()
        self._initialized = False
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_db(self) -> None:
        if self._initialized:
            return
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    handoff_id TEXT PRIMARY KEY,
                    intent_json TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                )
                """
            )
        self._initialized = True

    def save_proposal(
        self,
        handoff_id: str,
        intent_json: dict[str, Any],
        rationale: str,
    ) -> None:
        clean_id = _clean_text(handoff_id)
        print(
            f"ProposalStore.save_proposal: handoff_id={clean_id!r} db={self.db_path}",
            flush=True,
        )
        if not clean_id:
            print(
                "ProposalStore.save_proposal: skipped (empty handoff_id)",
                flush=True,
            )
            return
        payload = json.dumps(intent_json or {}, ensure_ascii=False, sort_keys=True)
        rationale_text = _clean_text(rationale)
        with self._lock:
            self._ensure_db()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO proposals (handoff_id, intent_json, rationale, saved_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(handoff_id) DO UPDATE SET
                        intent_json=excluded.intent_json,
                        rationale=excluded.rationale,
                        saved_at=excluded.saved_at
                    """,
                    (clean_id, payload, rationale_text, _utc_now_iso()),
                )
                row_count = conn.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()[0]
        print(
            f"ProposalStore.save_proposal: committed handoff_id={clean_id} "
            f"total_rows={row_count}",
            flush=True,
        )

    def get_proposal(self, handoff_id: str) -> dict[str, Any] | None:
        clean_id = _clean_text(handoff_id)
        print(
            f"ProposalStore.get_proposal: handoff_id={clean_id!r} db={self.db_path}",
            flush=True,
        )
        if not clean_id:
            return None
        with self._lock:
            self._ensure_db()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT intent_json, rationale, saved_at FROM proposals "
                    "WHERE handoff_id = ? LIMIT 1",
                    (clean_id,),
                ).fetchone()
        if row is None:
            print(
                f"ProposalStore.get_proposal: miss handoff_id={clean_id}",
                flush=True,
            )
            return None
        # A corrupt intent_json must not be silently dropped — it would look
        # identical to "not found" and mask a real persistence bug. Log and
        # raise so the caller and the operator can see what happened.
        try:
            intent = json.loads(row[0])
        except (json.JSONDecodeError, TypeError) as exc:
            print(
                f"ProposalStore.get_proposal: corrupt intent_json for "
                f"handoff_id={clean_id}: {exc}",
                flush=True,
            )
            raise
        print(
            f"ProposalStore.get_proposal: hit handoff_id={clean_id} "
            f"intent_empty={not bool(intent)}",
            flush=True,
        )
        return {
            "handoff_id": clean_id,
            "intent": intent,
            "rationale": row[1],
            "saved_at": row[2],
        }

    def delete_proposal(self, handoff_id: str) -> None:
        clean_id = _clean_text(handoff_id)
        print(
            f"ProposalStore.delete_proposal: handoff_id={clean_id!r} db={self.db_path}",
            flush=True,
        )
        if not clean_id:
            return
        with self._lock:
            self._ensure_db()
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM proposals WHERE handoff_id = ?",
                    (clean_id,),
                )

    def count_proposals(self) -> int:
        """Total rows in the proposals table (for startup diagnostics)."""
        with self._lock:
            self._ensure_db()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()
        return int(row[0]) if row is not None else 0
