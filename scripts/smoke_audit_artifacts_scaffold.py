#!/usr/bin/env python3
"""Deterministic smoke for the audit artifact writer scaffold (Slice E).

Writes only to a temporary directory. No broker calls, no network calls,
no real fills, no credentials.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "audit" / "artifacts.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.bridge_contract import ExecutionIntent, FillRecord, RiskSummary


def _load_module():
    spec = importlib.util.spec_from_file_location("audit_artifacts", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_artifacts"] = module
    spec.loader.exec_module(module)
    return module


CREATED_AT = "2026-06-10T00:00:00Z"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    mod = _load_module()
    Writer = mod.AuditArtifactWriter

    with tempfile.TemporaryDirectory(prefix="audit-artifacts-smoke-") as tmp:
        writer = Writer(
            session_id="smoke-session",
            artifact_root=Path(tmp) / "logs" / "audit",
            model="fake-local-model",
            provider="fake-local",
        )

        # Decision artifact: deterministic file name and envelope fields.
        decision = writer.write_decision_artifact(
            decision_id="dec-0001",
            decision="skip-candidate",
            rationale="confidence below threshold",
            source="tier1-triage",
            context={"symbol": "AAPL", "confidence": 0.2},
            created_at=CREATED_AT,
        )
        assert decision.ok is True, decision.reasons
        assert decision.path is not None and decision.path.name == "decision-dec-0001.json"
        payload = _read_json(decision.path)
        assert payload["artifact_kind"] == "decision"
        assert payload["created_at"] == CREATED_AT
        assert payload["model"] == "fake-local-model"
        assert payload["provider"] == "fake-local"
        assert payload["dry_run"] is True and payload["network_enabled"] is False
        assert payload["decision"] == "skip-candidate"

        # Malformed decision input fails closed and writes nothing.
        bad_decision = writer.write_decision_artifact(
            decision_id="dec-0002",
            decision="",
            rationale="   ",
            created_at=CREATED_AT,
        )
        assert bad_decision.ok is False and bad_decision.path is None
        assert any("decision must be non-empty" in r for r in bad_decision.reasons)
        assert not (writer.artifact_dir / "decision-dec-0002.json").exists()

        # Unsafe artifact ids (path traversal) fail closed.
        traversal = writer.write_decision_artifact(
            decision_id="../escape",
            decision="x",
            rationale="y",
            created_at=CREATED_AT,
        )
        assert traversal.ok is False, "path traversal artifact id must fail closed"

        # NEEDS_HUMAN / error artifact.
        needs_human = writer.write_needs_human_artifact(
            needs_human_id="nh-0001",
            reason_code="CANDIDATE_VALIDATION_ERROR",
            reason="missing symbol",
            source_event_id="cand-0001",
            context={"payload": {"side": "buy"}},
            created_at=CREATED_AT,
        )
        assert needs_human.ok is True, needs_human.reasons
        assert needs_human.path is not None and needs_human.path.name == "needs_human-nh-0001.json"
        payload = _read_json(needs_human.path)
        assert payload["reason_code"] == "CANDIDATE_VALIDATION_ERROR"
        assert payload["reason"] == "missing symbol"

        bad_needs_human = writer.write_needs_human_artifact(
            needs_human_id="nh-0002",
            reason_code="",
            reason="",
            created_at=CREATED_AT,
        )
        assert bad_needs_human.ok is False and bad_needs_human.path is None

        # Proposed ExecutionIntent artifact records the validation verdict and
        # never marks anything as executed.
        safe_intent = ExecutionIntent(
            intent_id="intent-0001",
            signal_id="sig-0001",
            broker="alpaca",
            symbol="AAPL",
            side="buy",
            quantity=10.0,
            created_at=CREATED_AT,
        )
        intent_result = writer.write_intent_artifact(safe_intent, created_at=CREATED_AT)
        assert intent_result.ok is True, intent_result.reasons
        assert intent_result.path is not None and intent_result.path.name == "intent-intent-0001.json"
        payload = _read_json(intent_result.path)
        assert payload["executed"] is False
        assert payload["validation"]["allowed"] is True
        assert payload["intent"]["dry_run"] is True
        assert payload["intent"]["approved"] is False

        # An unsafe proposed intent is still recordable, but the verdict shows blocked.
        unsafe_intent = ExecutionIntent(
            intent_id="intent-0002",
            signal_id="sig-0002",
            broker="alpaca",
            symbol="AAPL",
            side="buy",
            quantity=10.0,
            created_at=CREATED_AT,
            dry_run=False,
            approved=False,
        )
        unsafe_result = writer.write_intent_artifact(unsafe_intent, created_at=CREATED_AT)
        assert unsafe_result.ok is True, unsafe_result.reasons
        payload = _read_json(unsafe_result.path)
        assert payload["validation"]["allowed"] is False
        assert payload["executed"] is False
        assert any("dry_run=False requires approved=True" in r for r in payload["validation"]["reasons"])

        # needs_human verdicts are recorded too.
        risk_human = RiskSummary(
            risk_id="risk-0001",
            signal_id="sig-0001",
            risk_score=0.9,
            max_position_size=1.0,
            needs_human=True,
            needs_human_reason="manual review",
        )
        flagged = writer.write_intent_artifact(
            ExecutionIntent(
                intent_id="intent-0003",
                signal_id="sig-0001",
                broker="alpaca",
                symbol="AAPL",
                side="buy",
                quantity=1.0,
                created_at=CREATED_AT,
            ),
            risk=risk_human,
            created_at=CREATED_AT,
        )
        payload = _read_json(flagged.path)
        assert payload["validation"]["needs_human"] is True
        assert payload["validation"]["status"] == "needs_human"

        # Non-intent input fails closed.
        not_intent = writer.write_intent_artifact({"intent_id": "x"})  # type: ignore[arg-type]
        assert not_intent.ok is False and not_intent.path is None

        # Fill artifact: shape-only, dry_run fills only.
        fake_fill = FillRecord(
            fill_id="fill-0001",
            intent_id="intent-0001",
            broker="paper",
            order_id="paper-order-0001",
            symbol="AAPL",
            side="buy",
            quantity=10.0,
            filled_at=CREATED_AT,
            price=100.0,
            dry_run=True,
        )
        fill_result = writer.write_fill_artifact(fake_fill, created_at=CREATED_AT)
        assert fill_result.ok is True, fill_result.reasons
        assert fill_result.path is not None and fill_result.path.name == "fill-fill-0001.json"
        payload = _read_json(fill_result.path)
        assert payload["fake_local_only"] is True
        assert payload["fill"]["dry_run"] is True

        # A non-dry-run fill must fail closed (no real fills through this scaffold).
        real_fill = FillRecord(
            fill_id="fill-0002",
            intent_id="intent-0002",
            broker="alpaca",
            order_id="live-order",
            symbol="AAPL",
            side="buy",
            quantity=10.0,
            filled_at=CREATED_AT,
            dry_run=False,
        )
        blocked_fill = writer.write_fill_artifact(real_fill, created_at=CREATED_AT)
        assert blocked_fill.ok is False and blocked_fill.path is None
        assert any("dry_run must be True" in r for r in blocked_fill.reasons)
        assert not (writer.artifact_dir / "fill-fill-0002.json").exists()

        # String dry_run is never accepted.
        coerced_fill = FillRecord(
            fill_id="fill-0003",
            intent_id="intent-0001",
            broker="paper",
            order_id="paper-order-0003",
            symbol="AAPL",
            side="buy",
            quantity=1.0,
            filled_at=CREATED_AT,
            dry_run="true",  # type: ignore[arg-type]
        )
        assert writer.write_fill_artifact(coerced_fill, created_at=CREATED_AT).ok is False

    # Module must not import networking or broker machinery.
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "import requests",
        "import httpx",
        "import urllib",
        "import socket",
        "from autohedge.brokers",
    ):
        assert forbidden not in source, f"forbidden reference {forbidden!r} in audit artifacts"

    print("audit artifacts scaffold smoke: ok")


if __name__ == "__main__":
    main()
