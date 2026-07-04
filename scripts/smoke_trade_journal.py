#!/usr/bin/env python3
"""Offline smoke for the immutable append-only trade journal."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Thread
import types

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
JOURNAL_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "audit" / "trade_journal.py"
SERVER_MODULE = REPO_ROOT / "mcp_server" / "server.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stub_mcp() -> None:
    if "mcp" in sys.modules:
        return
    mcp_stub = types.ModuleType("mcp")
    mcp_stub.server = types.ModuleType("mcp.server")
    mcp_stub.server.fastmcp = types.ModuleType("mcp.server.fastmcp")
    sys.modules["mcp"] = mcp_stub
    sys.modules["mcp.server"] = mcp_stub.server
    sys.modules["mcp.server.fastmcp"] = mcp_stub.server.fastmcp


def main() -> None:
    tj = _load_module("trade_journal_smoke", JOURNAL_MODULE)

    # The module is append-only by construction: no UPDATE/DELETE SQL anywhere.
    source = JOURNAL_MODULE.read_text(encoding="utf-8").upper()
    assert "UPDATE TRADE_EVENTS" not in source, "journal must never UPDATE"
    assert "DELETE FROM" not in source, "journal must never DELETE"
    assert "DROP TABLE" not in source, "journal must never DROP"
    for forbidden in ("update_event", "delete_event", "remove_event"):
        assert not hasattr(tj.TradeJournal, forbidden)

    with tempfile.TemporaryDirectory(prefix="trade-journal-smoke-") as tmp:
        db_path = Path(tmp) / "state" / "trade_events.db"
        journal = tj.TradeJournal(db_path=db_path)
        assert journal.using_fallback is False

        # --- One event of every type; sha256 populated on every insert ---
        rows = []
        for i, event_type in enumerate(sorted(tj.TRADE_EVENT_TYPES)):
            row = journal.record_event(
                event_type=event_type,
                symbol="NVDA",
                side="buy" if i % 2 == 0 else "sell",
                payload={"seq": i, "event_type": event_type},
                ts=f"2026-07-01T00:00:{i:02d}Z",
            )
            assert row["id"] is not None
            assert row["sha256"] and len(row["sha256"]) == 64
            rows.append(row)

        # sha256 = hash(ts + event_type + symbol + payload_json), verifiable.
        sample = rows[0]
        expected = hashlib.sha256(
            (
                sample["ts"]
                + sample["event_type"]
                + sample["symbol"]
                + sample["payload_json"]
            ).encode("utf-8")
        ).hexdigest()
        assert sample["sha256"] == expected
        assert journal.verify_event(sample) is True
        tampered = dict(sample)
        tampered["payload_json"] = json.dumps({"seq": 999})
        assert journal.verify_event(tampered) is False

        # Every persisted row has a valid hash.
        stored = journal.get_events(limit=100)
        assert len(stored) == len(tj.TRADE_EVENT_TYPES)
        assert all(row["sha256"] for row in stored)
        assert all(journal.verify_event(row) for row in stored)

        # --- Filtering, ordering, limit ---
        journal.record_event(
            event_type="candidate_injected",
            symbol="AMD",
            side="buy",
            payload={"note": "second symbol"},
        )
        amd_only = journal.get_events(symbol="amd", limit=10)
        assert len(amd_only) == 1
        assert amd_only[0]["symbol"] == "AMD"
        newest_first = journal.get_events(limit=3)
        assert len(newest_first) == 3
        assert newest_first[0]["id"] > newest_first[-1]["id"]

        # --- parent_event_id chains ---
        parent = journal.record_event(
            event_type="order_submitted", symbol="MU", side="buy", payload={}
        )
        child = journal.record_event(
            event_type="filled",
            symbol="MU",
            side="buy",
            payload={},
            parent_event_id=parent["id"],
        )
        assert child["parent_event_id"] == parent["id"]

        # --- Validation fails closed ---
        for bad_kwargs in (
            {"event_type": "made_up_event", "symbol": "NVDA"},
            {"event_type": "filled", "symbol": ""},
            {"event_type": "filled", "symbol": "NVDA", "side": "hold"},
        ):
            try:
                journal.record_event(payload={}, **bad_kwargs)
                raise AssertionError(f"record_event accepted {bad_kwargs!r}")
            except ValueError:
                pass

        # --- Thread-safety: concurrent appends all land ---
        before = len(journal.get_events(limit=500))
        threads = [
            Thread(
                target=lambda n=n: journal.record_event(
                    event_type="regime_checked",
                    symbol="SPY",
                    payload={"worker": n},
                )
            )
            for n in range(16)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        after = journal.get_events(limit=500)
        assert len(after) == before + 16
        assert len({row["id"] for row in after}) == len(after)

        # --- Schema matches the doctrine exactly ---
        conn = sqlite3.connect(str(db_path))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(trade_events)")]
        finally:
            conn.close()
        assert columns == [
            "id", "ts", "event_type", "symbol", "side",
            "payload_json", "sha256", "parent_event_id",
        ]

    # --- MCP surface: get_trade_journal + journal wiring on injection ---
    _stub_mcp()
    server = _load_module("davey_trade_journal_smoke_server", SERVER_MODULE)
    with tempfile.TemporaryDirectory(prefix="trade-journal-mcp-smoke-") as tmp:
        root = Path(tmp)
        service = server.PokeBridgeService(repo_root=root)
        assert service.trade_journal is not None

        res = service.inject_researched_candidate(
            symbol="NVDA",
            thesis="journal smoke thesis",
            confidence=0.9,
            trigger_reason="journal smoke",
            direction="buy",
        )
        assert res["queued"] is True, res

        journal_result = service.get_trade_journal(symbol="NVDA", limit=10)
        events = journal_result["events"]
        types_seen = {event["event_type"] for event in events}
        assert "candidate_injected" in types_seen
        assert all(event["sha256"] for event in events)
        injected = next(
            event for event in events if event["event_type"] == "candidate_injected"
        )
        payload = json.loads(injected["payload_json"])
        assert payload["handoff_id"] == res["handoff_id"]

        # regime_checked is journaled for the injection gate too (SPY row).
        gate_events = service.get_trade_journal(limit=50)["events"]
        assert any(e["event_type"] == "regime_checked" for e in gate_events)

        # get_open_positions MCP surface returns the tracked-positions shape.
        open_positions = service.get_open_positions()
        assert open_positions["open_positions"] == 0
        assert open_positions["positions"] == []

    print("trade journal smoke: ok")


if __name__ == "__main__":
    main()
