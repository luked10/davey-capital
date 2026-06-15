"""Persistent proposal store for the Poke MCP bridge.

Sonnet-generated ExecutionIntent proposals must survive a machine restart
between ``submit_triage_decision`` (write) and ``record_approval_decision``
(read). This store backs proposals on the Fly volume
(``DAVEY_ROOT/state/proposals.db``) so nothing load-bearing lives only in RAM.

Schema stores the full candidate and proposal_payload alongside intent_json so
``get_proposal`` returns everything ``record_approval_decision`` needs without
re-reading the watcher queue.
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
    """SQLite-backed store for triage proposals keyed by handoff_id.

    Per-instance fallback dict is populated on every write so that reads
    within the same process are fast even when the DB is temporarily
    unavailable.  Each instance owns its own fallback so test isolation is
    preserved (no class-level shared state).
    """

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
        # On Fly the persistent volume is mounted at /data.  If DAVEY_ROOT was
        # not set explicitly but Fly env vars indicate we are running on the
        # platform and /data is available, prefer it over the /app code root.
        if davey_root is None and db_path is None and not os.getenv("DAVEY_ROOT"):
            fly_data = Path("/data")
            if fly_data.exists() and any(
                os.getenv(n)
                for n in ("FLY_APP_NAME", "FLY_MACHINE_ID", "FLY_REGION")
            ):
                root = fly_data

        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else root / "state" / "proposals.db"
        )
        self._lock = Lock()
        self._db_available = True
        self._initialized = False
        self._fallback: dict[str, dict[str, Any]] = {}
        self._ensure_db()
        print(
            f"ProposalStore: db_path={self.db_path}",
            flush=True,
        )

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
            print(
                f"ProposalStore._ensure_db: SQLite unavailable: {exc}",
                flush=True,
            )

    def save_proposal(
        self,
        handoff_id: str,
        session_id: str,
        candidate: dict[str, Any],
        proposal_payload: dict[str, Any],
        intent_json: str | None = None,
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
        entry: dict[str, Any] = {
            "session_id": session_id,
            "candidate": candidate,
            "proposal_payload": proposal_payload,
            "intent_json": intent_json,
        }
        with self._lock:
            self._fallback[clean_id] = entry
            if not self._db_available:
                print(
                    f"ProposalStore.save_proposal: DB unavailable, "
                    f"stored in fallback only for handoff_id={clean_id}",
                    flush=True,
                )
                return
            self._ensure_db()
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO proposals
                        (handoff_id, session_id, candidate_json,
                         proposal_payload_json, intent_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_id,
                            session_id,
                            json.dumps(candidate, ensure_ascii=False),
                            json.dumps(proposal_payload, ensure_ascii=False),
                            intent_json,
                            _utc_now_iso(),
                        ),
                    )
                    row_count = conn.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0]
                print(
                    f"ProposalStore.save_proposal: committed handoff_id={clean_id} "
                    f"total_rows={row_count}",
                    flush=True,
                )
            except sqlite3.Error as exc:
                self._db_available = False
                print(
                    f"ProposalStore.save_proposal: write failed for "
                    f"handoff_id={clean_id}: {exc} (stored in fallback)",
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
            if self._db_available:
                self._ensure_db()
                try:
                    with self._connect() as conn:
                        row = conn.execute(
                            """
                            SELECT session_id, candidate_json,
                                   proposal_payload_json, intent_json
                            FROM proposals WHERE handoff_id = ? LIMIT 1
                            """,
                            (clean_id,),
                        ).fetchone()
                    if row is not None:
                        result = {
                            "session_id": row[0],
                            "candidate": json.loads(row[1]),
                            "proposal_payload": json.loads(row[2]),
                            "intent_json": row[3],
                        }
                        print(
                            f"ProposalStore.get_proposal: hit handoff_id={clean_id} "
                            f"has_intent={row[3] is not None}",
                            flush=True,
                        )
                        return result
                    print(
                        f"ProposalStore.get_proposal: miss handoff_id={clean_id} in DB",
                        flush=True,
                    )
                except sqlite3.Error as exc:
                    self._db_available = False
                    print(
                        f"ProposalStore.get_proposal: DB read failed for "
                        f"handoff_id={clean_id}: {exc} (trying fallback)",
                        flush=True,
                    )
            fallback = self._fallback.get(clean_id)
            if fallback is not None:
                print(
                    f"ProposalStore.get_proposal: fallback hit handoff_id={clean_id}",
                    flush=True,
                )
            else:
                print(
                    f"ProposalStore.get_proposal: miss handoff_id={clean_id} "
                    "(DB and fallback both empty)",
                    flush=True,
                )
            return fallback

    def delete_proposal(self, handoff_id: str) -> None:
        clean_id = _clean_text(handoff_id)
        print(
            f"ProposalStore.delete_proposal: handoff_id={clean_id!r} db={self.db_path}",
            flush=True,
        )
        if not clean_id:
            return
        with self._lock:
            self._fallback.pop(clean_id, None)
            if not self._db_available:
                return
            self._ensure_db()
            try:
                with self._connect() as conn:
                    conn.execute(
                        "DELETE FROM proposals WHERE handoff_id = ?",
                        (clean_id,),
                    )
            except sqlite3.Error as exc:
                print(
                    f"ProposalStore.delete_proposal: delete failed for "
                    f"handoff_id={clean_id}: {exc}",
                    flush=True,
                )

    def count_proposals(self) -> int:
        """Total rows in the proposals table (startup diagnostics)."""
        with self._lock:
            if not self._db_available:
                return len(self._fallback)
            self._ensure_db()
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()
                return int(row[0]) if row is not None else 0
            except sqlite3.Error:
                return len(self._fallback)
