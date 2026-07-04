"""Immutable, append-only trade journal (SQLite + sha256 integrity hashes).

Every trade lifecycle event is recorded to trade_events.db with a sha256 of
(ts + event_type + symbol + payload_json). The store is append-only by
construction: this module exposes no UPDATE or DELETE path, ever.

Thread-safe writes follow the seen_ids.py pattern exactly: a module Lock, a
fresh connection per operation closed in try/finally, WAL journal mode, and an
in-memory fallback when SQLite is unavailable so callers never crash.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import Lock
from typing import Any


TRADE_JOURNAL_VERSION = "0.1.0"

TRADE_EVENT_TYPES = frozenset(
    {
        "candidate_injected",
        "regime_checked",
        "proposal_created",
        "human_approved",
        "human_rejected",
        "order_submitted",
        "filled",
        "exit_triggered",
        "stop_hit",
        "take_profit",
        "trailing_exit",
        "closed",
    }
)


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


def _warn(message: str) -> None:
    print(f"trade_journal warning: {message}", file=sys.stderr, flush=True)


def event_sha256(*, ts: str, event_type: str, symbol: str, payload_json: str) -> str:
    """sha256(ts + event_type + symbol + payload_json) — the row integrity hash."""
    return hashlib.sha256(
        (ts + event_type + symbol + payload_json).encode("utf-8")
    ).hexdigest()


class TradeJournal:
    """SQLite-backed append-only trade event store (no UPDATE/DELETE, ever)."""

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
        # Match seen_ids.py: prefer the /data volume on Fly when present.
        fly_data = Path("/data")
        if db_path is None and fly_data.exists():
            root = fly_data

        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else root / "state" / "trade_events.db"
        )
        self._lock = Lock()
        self._fallback_events: list[dict[str, Any]] = []
        self._db_available = True
        self._initialized = False
        self._warned_fallback = False
        self._ensure_db()

    @property
    def using_fallback(self) -> bool:
        return not self._db_available

    def _warn_fallback_once(self, reason: str) -> None:
        if self._warned_fallback:
            return
        self._warned_fallback = True
        _warn(f"SQLite unavailable ({reason}); using in-memory fallback at {self.db_path}")

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
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trade_events (
                        id INTEGER PRIMARY KEY,
                        ts TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT,
                        payload_json TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        parent_event_id INTEGER
                    )
                    """
                )
            finally:
                conn.close()
            self._db_available = True
            self._initialized = True
        except sqlite3.Error as exc:
            self._db_available = False
            self._warn_fallback_once(str(exc))

    def record_event(
        self,
        *,
        event_type: str,
        symbol: str,
        payload: dict[str, Any] | None = None,
        side: str | None = None,
        parent_event_id: int | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable event row. Returns the recorded row as a dict."""
        clean_type = _clean_text(event_type)
        if clean_type not in TRADE_EVENT_TYPES:
            raise ValueError(
                f"unknown event_type {event_type!r}; expected one of "
                f"{sorted(TRADE_EVENT_TYPES)}"
            )
        clean_symbol = _clean_text(symbol).upper()
        if not clean_symbol:
            raise ValueError("symbol must be non-empty")
        clean_side = _clean_text(side).lower() or None
        if clean_side is not None and clean_side not in {"buy", "sell"}:
            raise ValueError(f"side must be buy/sell when provided, got {side!r}")
        clean_ts = _clean_text(ts) or _utc_now_iso()
        payload_json = json.dumps(
            dict(payload or {}), ensure_ascii=False, sort_keys=True
        )
        digest = event_sha256(
            ts=clean_ts,
            event_type=clean_type,
            symbol=clean_symbol,
            payload_json=payload_json,
        )

        row: dict[str, Any] = {
            "id": None,
            "ts": clean_ts,
            "event_type": clean_type,
            "symbol": clean_symbol,
            "side": clean_side,
            "payload_json": payload_json,
            "sha256": digest,
            "parent_event_id": parent_event_id,
        }

        with self._lock:
            if self._db_available:
                self._ensure_db()
            if self._db_available:
                try:
                    conn = self._connect()
                    try:
                        cursor = conn.execute(
                            """
                            INSERT INTO trade_events
                                (ts, event_type, symbol, side, payload_json,
                                 sha256, parent_event_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                clean_ts,
                                clean_type,
                                clean_symbol,
                                clean_side,
                                payload_json,
                                digest,
                                parent_event_id,
                            ),
                        )
                        row["id"] = cursor.lastrowid
                    finally:
                        conn.close()
                    return row
                except sqlite3.Error as exc:
                    self._db_available = False
                    self._warn_fallback_once(str(exc))
            row["id"] = len(self._fallback_events) + 1
            self._fallback_events.append(row)
            return row

    def get_events(
        self,
        *,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read recent events (newest first), optionally filtered by symbol."""
        clean_symbol = _clean_text(symbol).upper() or None
        try:
            clean_limit = int(limit)
        except (TypeError, ValueError):
            clean_limit = 50
        clean_limit = max(1, min(clean_limit, 500))

        with self._lock:
            if self._db_available:
                self._ensure_db()
            if self._db_available:
                try:
                    conn = self._connect()
                    try:
                        if clean_symbol:
                            rows = conn.execute(
                                """
                                SELECT id, ts, event_type, symbol, side,
                                       payload_json, sha256, parent_event_id
                                FROM trade_events
                                WHERE symbol = ?
                                ORDER BY id DESC LIMIT ?
                                """,
                                (clean_symbol, clean_limit),
                            ).fetchall()
                        else:
                            rows = conn.execute(
                                """
                                SELECT id, ts, event_type, symbol, side,
                                       payload_json, sha256, parent_event_id
                                FROM trade_events
                                ORDER BY id DESC LIMIT ?
                                """,
                                (clean_limit,),
                            ).fetchall()
                    finally:
                        conn.close()
                    return [
                        {
                            "id": row[0],
                            "ts": row[1],
                            "event_type": row[2],
                            "symbol": row[3],
                            "side": row[4],
                            "payload_json": row[5],
                            "sha256": row[6],
                            "parent_event_id": row[7],
                        }
                        for row in rows
                    ]
                except sqlite3.Error as exc:
                    self._db_available = False
                    self._warn_fallback_once(str(exc))
            events = [
                row
                for row in self._fallback_events
                if clean_symbol is None or row["symbol"] == clean_symbol
            ]
            return list(reversed(events[-clean_limit:]))

    def verify_event(self, row: dict[str, Any]) -> bool:
        """Recompute a row's sha256 and compare (tamper check for audits)."""
        expected = event_sha256(
            ts=str(row.get("ts", "")),
            event_type=str(row.get("event_type", "")),
            symbol=str(row.get("symbol", "")),
            payload_json=str(row.get("payload_json", "")),
        )
        return expected == str(row.get("sha256", ""))
