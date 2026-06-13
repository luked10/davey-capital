#!/usr/bin/env python3
"""Deterministic smoke for the Poke MCP server core.

This does not start the HTTP/SSE server and does not call Sonnet. It verifies
the local queue reader and proceed=False triage logging path with temp files.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "mcp_server" / "server.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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

        pending = service.get_pending_candidates()
        assert pending == [
            {
                "handoff_id": "handoff-0001",
                "symbol": "NVDA",
                "side": "buy",
                "confidence": 0.85,
                "created_at": "2026-06-12T00:00:00Z",
            }
        ]
        assert service.get_pending_candidates() == [], "seen handoffs should not repeat"
        restarted_service = server.PokeBridgeService(repo_root=root)
        assert (
            restarted_service.get_pending_candidates() == []
        ), "persistent seen ids should survive service restart"

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

    source = SERVER_PATH.read_text(encoding="utf-8")
    assert "execution_intent_to_broker_order" not in source
    assert "from autohedge.brokers" not in source
    assert "submit_order" not in source
    assert "place_order" not in source

    print("poke mcp server smoke: ok")


if __name__ == "__main__":
    main()
