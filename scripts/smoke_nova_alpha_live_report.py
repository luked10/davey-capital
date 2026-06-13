#!/usr/bin/env python3
"""Deterministic smoke for nova-alpha's today-only live report path.

Builds fake local artifacts for the current UTC day plus stale artifacts from
yesterday, renders the today-only report, and asserts counts. No network calls,
no broker calls, and no execution paths are touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    spec = importlib.util.spec_from_file_location(
        "nova_alpha_live_report_smoke",
        str(MODULE_PATH),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["nova_alpha_live_report_smoke"] = module
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
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    today_date = today.isoformat()
    yesterday_date = yesterday.isoformat()

    with tempfile.TemporaryDirectory(prefix="nova-alpha-live-report-smoke-") as tmp:
        root = Path(tmp)
        watcher_root = root / "logs" / "overnight" / "smoke-session"
        audit_root = root / "logs" / "audit" / "smoke-session"

        _write_jsonl(
            watcher_root / "candidate_events.jsonl",
            [
                {
                    "event_id": "cand-today-0001",
                    "created_at": f"{today_date}T13:30:00Z",
                    "symbol": "NVDA",
                    "side": "buy",
                    "confidence": 0.82,
                    "strategy": "momentum",
                    "dry_run": True,
                },
                {
                    "event_id": "cand-today-0002",
                    "created_at": f"{today_date}T13:31:00Z",
                    "symbol": "MSFT",
                    "side": "sell",
                    "confidence": 0.71,
                    "strategy": "reversion",
                    "dry_run": True,
                },
                {
                    "event_id": "cand-today-0003",
                    "created_at": f"{today_date}T13:32:00Z",
                    "symbol": "AAPL",
                    "side": "buy",
                    "confidence": 0.69,
                    "strategy": "breakout",
                    "dry_run": True,
                },
                {
                    "event_id": "cand-yesterday-0001",
                    "created_at": f"{yesterday_date}T13:30:00Z",
                    "symbol": "TSLA",
                    "side": "buy",
                    "confidence": 0.95,
                    "strategy": "stale",
                    "dry_run": True,
                },
            ],
        )
        _write_jsonl(
            audit_root / "triage_decisions.jsonl",
            [
                {
                    "artifact_kind": "triage_decision",
                    "created_at": f"{today_date}T13:33:00Z",
                    "handoff_id": "handoff-today-0001",
                    "proceed": True,
                    "reason": "high confidence smoke candidate",
                    "candidate": {"created_at": f"{today_date}T13:30:00Z"},
                    "proposal": {},
                },
                {
                    "artifact_kind": "triage_decision",
                    "created_at": f"{yesterday_date}T13:33:00Z",
                    "handoff_id": "handoff-yesterday-0001",
                    "proceed": True,
                    "reason": "stale candidate",
                    "candidate": {"created_at": f"{yesterday_date}T13:30:00Z"},
                    "proposal": {},
                },
            ],
        )
        _write_jsonl(
            audit_root / "approval_decisions.jsonl",
            [
                {
                    "artifact_kind": "approval_decision",
                    "created_at": f"{yesterday_date}T14:00:00Z",
                    "handoff_id": "handoff-yesterday-0001",
                    "approved": True,
                    "approved_by": "smoke",
                    "intent_id": "intent-yesterday-0001",
                    "reason": "stale approval",
                }
            ],
        )
        _write_json(
            audit_root / "intent-intent-today-0001.json",
            {
                "artifact_kind": "intent",
                "created_at": f"{today_date}T13:34:00Z",
                "executed": False,
                "intent": {
                    "intent_id": "intent-today-0001",
                    "broker": "alpaca",
                    "symbol": "NVDA",
                    "side": "buy",
                    "quantity": 1.0,
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
        _write_json(
            audit_root / "decision-circuit-breaker-yesterday.json",
            {
                "artifact_kind": "decision",
                "created_at": f"{yesterday_date}T13:35:00Z",
                "decision": "blocked",
                "rationale": "stale block",
                "source": "circuit_breaker",
                "context": {"circuit_breaker": {"blocked": True}},
            },
        )

        artifacts = mod.load_local_artifacts(root)
        report = mod.render_daily_report(
            artifacts,
            report_date=today_date,
            today_only=True,
        )
        assert "Today: 3 candidates, 1 proposal, 0 approved, 0 rejected, 0 blocks" in report
        assert "n_candidates: 3" in report
        assert "n_proceeded: 1" in report
        assert "n_proposals: 1" in report
        assert "n_approved: 0" in report
        assert "n_rejected: 0" in report
        assert "n_circuit_breaker_blocks: 0" in report
        assert "cand-today-0001" in report
        assert "cand-yesterday-0001" not in report
        assert "intent-today-0001" in report
        assert "intent-yesterday-0001" not in report
        assert mod.has_report_activity(
            mod.load_local_artifacts(root, today_only=True, report_date=today_date)
        )

    print("nova-alpha live report smoke: ok")


if __name__ == "__main__":
    main()
