#!/usr/bin/env python3
"""Deterministic smoke for the nova-alpha local report scaffold (Slice F).

Builds fixture artifacts in a temporary directory, generates the markdown
report, and asserts content. No network calls, no scheduler wiring, no live
account or broker reads.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "nova-alpha" / "report_scaffold.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("nova_alpha_report_scaffold", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["nova_alpha_report_scaffold"] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    mod = _load_module()

    with tempfile.TemporaryDirectory(prefix="nova-alpha-report-smoke-") as tmp:
        root = Path(tmp) / "artifacts"

        # Watcher-style fixtures.
        _write_jsonl(
            root / "watcher" / "candidate_events.jsonl",
            [
                {
                    "event_id": "cand-0001",
                    "run_id": "fixture-run",
                    "created_at": "2026-06-10T00:00:00Z",
                    "symbol": "AAPL",
                    "side": "buy",
                    "confidence": 0.75,
                    "strategy": "momentum",
                    "dry_run": True,
                },
                {
                    "event_id": "cand-0002",
                    "run_id": "fixture-run",
                    "created_at": "2026-06-10T00:01:00Z",
                    "symbol": "MSFT",
                    "side": "sell",
                    "confidence": 0.6,
                    "strategy": "reversion",
                    "dry_run": True,
                },
            ],
        )
        _write_jsonl(
            root / "watcher" / "needs_human_events.jsonl",
            [
                {
                    "needs_human_id": "nh-0001",
                    "run_id": "fixture-run",
                    "created_at": "2026-06-10T00:02:00Z",
                    "reason_code": "CANDIDATE_VALIDATION_ERROR",
                    "reason": "symbol is required",
                    "dry_run": True,
                }
            ],
        )
        _write_jsonl(
            root / "watcher" / "poke_bridge_queue.jsonl",
            [
                {
                    "handoff_id": "handoff-0001",
                    "run_id": "fixture-run",
                    "created_at": "2026-06-10T00:00:30Z",
                    "candidate_event_id": "cand-0001",
                    "destination": "poke_bridge_local_queue",
                    "dry_run": True,
                }
            ],
        )

        # Audit-style fixtures (triage decision + proposal).
        _write_json(
            root / "audit" / "decision-dec-0001.json",
            {
                "artifact_kind": "decision",
                "decision": "escalate-to-proposal",
                "rationale": "high confidence candidate",
                "source": "poke-tier1-triage",
                "created_at": "2026-06-10T00:03:00Z",
            },
        )
        _write_json(
            root / "audit" / "intent-intent-0001.json",
            {
                "artifact_kind": "intent",
                "executed": False,
                "intent": {
                    "intent_id": "intent-0001",
                    "broker": "alpaca",
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": 10.0,
                    "dry_run": True,
                    "approved": False,
                },
                "validation": {
                    "allowed": True,
                    "needs_human": False,
                    "status": "ok",
                    "reasons": [],
                },
            },
        )

        # Runtime state with circuit breaker status.
        _write_json(
            root / "runtime_state.json",
            {
                "active_broker": "paper",
                "dry_run": True,
                "live_mode": False,
                "positions_summary": {},
                "latest_signal_ids": [],
                "circuit_breaker_status": {
                    "enabled": False,
                    "allowed": True,
                    "reason": "circuit breaker disabled (default no-op)",
                    "triggered_rules": [],
                },
                "last_error": "",
                "last_health_check": "2026-06-10T00:00:00Z",
                "updated_at": "2026-06-10T00:00:00Z",
            },
        )

        artifacts = mod.load_local_artifacts(root)
        assert len(artifacts["candidates"]) == 2
        assert len(artifacts["needs_human"]) == 1
        assert len(artifacts["poke_queue"]) == 1
        assert len(artifacts["triage_decisions"]) == 1
        assert len(artifacts["proposals"]) == 1
        assert artifacts["circuit_breaker_status"]["enabled"] is False

        report = mod.render_daily_report(artifacts, report_date="2026-06-10")
        assert "# Nova Alpha Local Daily Report - 2026-06-10" in report
        assert "Candidates seen: 2" in report
        assert "| cand-0001 | AAPL | buy | 0.75 | momentum |" in report
        assert "| cand-0002 | MSFT | sell | 0.60 | reversion |" in report
        assert "**escalate-to-proposal** (poke-tier1-triage): high confidence candidate" in report
        assert "`intent-0001` buy 10.0 AAPL via alpaca" in report
        assert "status: ok | allowed: True | executed: False" in report
        assert "`CANDIDATE_VALIDATION_ERROR`: symbol is required" in report
        assert "## Circuit Breaker Status" in report
        assert "Enabled: False" in report
        assert "No live broker data" in report

        # Rendering is deterministic for fixed inputs.
        assert report == mod.render_daily_report(artifacts, report_date="2026-06-10")

        # write_daily_report writes the same content to disk.
        out_path = mod.write_daily_report(
            root,
            Path(tmp) / "reports" / "daily_report_local.md",
            report_date="2026-06-10",
        )
        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == report

        # Empty artifact directory still renders a valid report.
        empty_root = Path(tmp) / "empty"
        empty_root.mkdir()
        empty_report = mod.render_daily_report(
            mod.load_local_artifacts(empty_root), report_date="2026-06-10"
        )
        assert "No candidates recorded." in empty_report
        assert "No NEEDS_HUMAN events recorded." in empty_report
        assert "default: disabled no-op" in empty_report

        # Malformed inputs fail closed.
        try:
            mod.load_local_artifacts(Path(tmp) / "missing-root")
        except ValueError:
            pass
        else:
            raise AssertionError("missing artifact root must fail closed")

        try:
            mod.render_daily_report(artifacts, report_date="")
        except ValueError:
            pass
        else:
            raise AssertionError("empty report_date must fail closed")

    # Module must not import networking, scheduler, or broker machinery.
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "import requests",
        "import httpx",
        "import urllib",
        "import socket",
        "apscheduler",
        "from autohedge.brokers",
    ):
        assert forbidden not in source, f"forbidden reference {forbidden!r} in report scaffold"

    print("nova-alpha report scaffold smoke: ok")


if __name__ == "__main__":
    main()
