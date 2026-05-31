#!/usr/bin/env python3
"""Smoke checks for shared bridge contract models."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.bridge_contract import (
    BridgeEnvelope,
    ExecutionIntent,
    FillRecord,
    RiskSummary,
    RunMetadata,
    SignalPayload,
    bridge_envelope_from_json,
    execution_intent_from_json,
    execution_intent_to_broker_order,
    to_json,
    validate_execution_intent,
)


def main() -> None:
    run = RunMetadata(
        run_id="run-001",
        source_system="vibe-trading",
        created_at="2026-05-31T03:30:00Z",
    )
    signal = SignalPayload(
        signal_id="sig-001",
        symbol="AAPL",
        side="buy",
        confidence=0.74,
        generated_at="2026-05-31T03:30:05Z",
        strategy="momentum-breakout",
        timeframe="1D",
    )
    risk = RiskSummary(
        risk_id="risk-001",
        signal_id=signal.signal_id,
        risk_score=0.32,
        max_position_size=25.0,
        max_notional=25000.0,
    )
    intent = ExecutionIntent(
        intent_id="intent-001",
        signal_id=signal.signal_id,
        broker="alpaca",
        symbol=signal.symbol,
        side=signal.side,
        quantity=10.0,
        created_at="2026-05-31T03:30:10Z",
    )
    fill = FillRecord(
        fill_id="fill-001",
        intent_id=intent.intent_id,
        broker=intent.broker,
        order_id="dry-order-001",
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        filled_at="2026-05-31T03:30:15Z",
        price=190.25,
    )

    envelope = BridgeEnvelope(
        run=run,
        signal=signal,
        risk=risk,
        intent=intent,
        fill=fill,
    )

    raw = to_json(envelope)
    decoded = bridge_envelope_from_json(raw)

    assert decoded.run.run_id == run.run_id
    assert decoded.signal.signal_id == signal.signal_id
    assert decoded.risk.risk_id == risk.risk_id
    assert decoded.intent.intent_id == intent.intent_id
    assert decoded.fill is not None
    assert decoded.fill.fill_id == fill.fill_id

    # Guardrails: dry-run defaults remain safe.
    assert decoded.intent.dry_run is True
    assert decoded.intent.approved is False
    assert decoded.run.dry_run is True

    valid = validate_execution_intent(decoded.intent, risk=decoded.risk, run=decoded.run)
    assert valid.allowed is True
    broker_order = execution_intent_to_broker_order(decoded.intent, risk=decoded.risk, run=decoded.run)
    assert broker_order["symbol"] == "AAPL"
    assert broker_order["side"] == "buy"
    assert broker_order["order_type"] == "market"

    unsafe_unapproved = execution_intent_from_json(
        '{"intent_id":"intent-unsafe-1","signal_id":"sig-001","broker":"alpaca","symbol":"AAPL","side":"buy","quantity":10,"created_at":"2026-05-31T03:35:00Z","dry_run":false,"approved":false}'
    )
    blocked = validate_execution_intent(unsafe_unapproved, risk=risk, run=run)
    assert blocked.allowed is False
    assert any("dry_run=False requires approved=True" in reason for reason in blocked.reasons)

    unsafe_missing_approver = execution_intent_from_json(
        '{"intent_id":"intent-unsafe-2","signal_id":"sig-001","broker":"alpaca","symbol":"AAPL","side":"buy","quantity":10,"created_at":"2026-05-31T03:35:01Z","dry_run":false,"approved":true,"approved_at":"2026-05-31T03:35:02Z"}'
    )
    blocked = validate_execution_intent(unsafe_missing_approver, risk=risk, run=run)
    assert blocked.allowed is False
    assert any("approved_by" in reason for reason in blocked.reasons)

    unsafe_bad_side = execution_intent_from_json(
        '{"intent_id":"intent-unsafe-3","signal_id":"sig-001","broker":"alpaca","symbol":"AAPL","side":"hold","quantity":10,"created_at":"2026-05-31T03:35:03Z"}'
    )
    blocked = validate_execution_intent(unsafe_bad_side, risk=risk, run=run)
    assert blocked.allowed is False
    assert any("invalid side" in reason for reason in blocked.reasons)

    risk_needs_human = RiskSummary(
        risk_id="risk-002",
        signal_id=signal.signal_id,
        risk_score=0.95,
        max_position_size=5.0,
        needs_human=True,
        needs_human_reason="manual override required",
    )
    blocked = validate_execution_intent(decoded.intent, risk=risk_needs_human, run=run)
    assert blocked.allowed is False
    assert blocked.needs_human is True
    assert blocked.status == "needs_human"

    print("bridge_contract smoke: ok")


if __name__ == "__main__":
    main()
