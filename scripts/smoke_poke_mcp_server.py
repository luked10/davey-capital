#!/usr/bin/env python3
"""Deterministic smoke for the Poke MCP server core.

This does not start the HTTP/SSE server and does not call Sonnet. It verifies
the local queue reader and proceed=False triage logging path with temp files.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "mcp_server" / "server.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.bridge_contract import ExecutionIntent


def _load_server_module():
    spec = importlib.util.spec_from_file_location("poke_mcp_server_smoke", str(SERVER_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["poke_mcp_server_smoke"] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    server = _load_server_module()

    with tempfile.TemporaryDirectory(prefix="poke-mcp-smoke-") as tmp:
        root = Path(tmp)
        service = server.PokeBridgeService(repo_root=root)

        assert service.get_pending_candidates() == []

        queue_path = root / "logs" / "overnight" / "smoke-session" / "poke_bridge_queue.jsonl"
        _write_jsonl(
            queue_path,
            [
                {
                    "handoff_id": "handoff-0001",
                    "run_id": "run-0001",
                    "created_at": "2026-06-12T00:00:00Z",
                    "candidate_event_id": "candidate-0001",
                    "destination": "poke_bridge_local_queue",
                    "dry_run": True,
                    "metadata": {
                        "symbol": "NVDA",
                        "side": "buy",
                        "confidence": 0.85,
                    },
                }
            ],
        )

        pending = [candidate.model_dump() for candidate in service.get_pending_candidates()]
        assert pending == [
            {
                "handoff_id": "handoff-0001",
                "symbol": "NVDA",
                "side": "buy",
                "confidence": 0.85,
                "created_at": "2026-06-12T00:00:00Z",
                "dry_run": True,
                "metadata": {
                    "symbol": "NVDA",
                    "side": "buy",
                    "confidence": 0.85,
                    "session_id": "smoke-session",
                    "run_id": "run-0001",
                    "candidate_event_id": "candidate-0001",
                },
            }
        ]
        assert service.get_pending_candidates() == [], "seen handoffs should not repeat"

        validation_error = service.submit_triage_decision(
            handoff_id="handoff-0001",
            proceed="false",  # type: ignore[arg-type]
            reason="bad bool",
        )
        assert validation_error["needs_human"] is True
        assert validation_error["status"] == "validation_error"
        assert "proceed" in validation_error["error"]

        result = service.submit_triage_decision(
            handoff_id="handoff-0001",
            proceed=False,
            reason="manual smoke reject",
        )
        assert result["status"] == "rejected_by_triage"
        assert result["candidate"]["symbol"] == "NVDA"

        triage_path = root / "logs" / "audit" / "smoke-session" / "triage_decisions.jsonl"
        rows = [
            json.loads(line)
            for line in triage_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["artifact_kind"] == "triage_decision"
        assert rows[0]["handoff_id"] == "handoff-0001"
        assert rows[0]["proceed"] is False
        assert rows[0]["reason"] == "manual smoke reject"

        _write_jsonl(
            queue_path,
            [
                {
                    "handoff_id": "handoff-0002",
                    "run_id": "run-0002",
                    "created_at": "2026-06-12T00:01:00Z",
                    "candidate_event_id": "candidate-0002",
                    "destination": "poke_bridge_local_queue",
                    "dry_run": True,
                    "metadata": {
                        "symbol": "NVDA",
                        "side": "buy",
                        "confidence": 0.9,
                    },
                }
            ],
        )
        (root / "circuit_breaker_config.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "max_consecutive_losses": 0,
                    "max_daily_loss_pct": 0.02,
                    "max_open_trades": 5,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        blocked = service.submit_triage_decision(
            handoff_id="handoff-0002",
            proceed=True,
            reason="manual smoke proceed",
        )
        assert blocked["status"] == "proposal_needs_human"
        assert blocked["needs_human"] is True
        assert blocked["proposal"]["intent"] is None
        assert blocked["proposal"]["circuit_breaker"]["blocked"] is True

        cb_path = root / "logs" / "audit" / "smoke-session" / "decision-circuit-breaker-handoff-0002.json"
        cb_payload = json.loads(cb_path.read_text(encoding="utf-8"))
        assert cb_payload["decision"] == "blocked"
        assert cb_payload["context"]["circuit_breaker"]["blocked"] is True

        status = service.get_system_status()
        assert status["active_broker"] == "paper"
        assert status["dry_run"] is True
        assert status["live_mode"] is False
        assert status["last_error"] == ""

        today = datetime.now(timezone.utc).date().isoformat()
        daily_root = root / "daily-report-root"
        daily_service = server.PokeBridgeService(repo_root=daily_root)
        _write_jsonl(
            daily_root / "logs" / "overnight" / "today-session" / "candidate_events.jsonl",
            [
                {
                    "event_id": "candidate-today-0001",
                    "run_id": "run-today",
                    "created_at": f"{today}T13:30:00Z",
                    "symbol": "NVDA",
                    "side": "buy",
                    "confidence": 0.88,
                    "strategy": "smoke",
                    "dry_run": True,
                }
            ],
        )
        daily_report = daily_service.get_daily_report()
        assert "Today: 1 candidate, 0 proposals, 0 approved, 0 rejected, 0 blocks" in daily_report
        assert "candidate-today-0001" in daily_report

        _write_jsonl(
            queue_path,
            [
                {
                    "handoff_id": "handoff-0003",
                    "run_id": "run-0003",
                    "created_at": "2026-06-12T00:02:00Z",
                    "candidate_event_id": "candidate-0003",
                    "destination": "poke_bridge_local_queue",
                    "dry_run": True,
                    "metadata": {
                        "symbol": "NVDA",
                        "side": "buy",
                        "confidence": 0.91,
                    },
                }
            ],
        )
        previous_live_mode = os.environ.pop("DAVEY_LIVE_MODE", None)
        try:
            service.proposals_by_handoff["handoff-0003"] = {
                "intent": ExecutionIntent(
                    intent_id="intent-live-forced-dry-run",
                    signal_id="candidate-0003",
                    broker="alpaca",
                    symbol="NVDA",
                    side="buy",
                    quantity=1.0,
                    created_at="2026-06-12T00:02:01Z",
                    dry_run=False,
                    approved=False,
                    metadata={"estimated_price": 100.0},
                ),
                "session_id": "smoke-session",
                "candidate": {},
                "proposal": {},
            }
            approval = service.record_approval_decision(
                handoff_id="handoff-0003",
                approved=True,
                approved_by="smoke-test",
            )
        finally:
            if previous_live_mode is not None:
                os.environ["DAVEY_LIVE_MODE"] = previous_live_mode
        assert "dry-run approved intent artifact" in approval
        forced_path = (
            root
            / "logs"
            / "audit"
            / "smoke-session"
            / "decision-live-mode-forced-dry-run-handoff-0003.json"
        )
        assert forced_path.exists()
        intent_path = (
            root
            / "logs"
            / "audit"
            / "smoke-session"
            / "intent-intent-live-forced-dry-run.json"
        )
        intent_payload = json.loads(intent_path.read_text(encoding="utf-8"))
        assert intent_payload["intent"]["dry_run"] is True

    source = SERVER_PATH.read_text(encoding="utf-8")
    assert "from autohedge.brokers" not in source
    assert "place_order" not in source

    print("poke mcp server smoke: ok")


if __name__ == "__main__":
    main()
