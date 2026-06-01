#!/usr/bin/env python3
"""Deterministic, offline structured-output reliability harness for ExecutionIntent.

This smoke exercises malformed / ambiguous structured payloads against the
existing bridge contract validation and broker-order conversion helpers. It is a
safety-hardening check only: it makes NO network calls, NO broker / Alpaca calls,
requires NO credentials or env vars, and never places an order.

It asserts that unsafe payloads are blocked (fail closed) and that only the
strictly-safe shapes are allowed, exiting nonzero on any assertion failure.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.bridge_contract import (
    ExecutionIntent,
    RiskSummary,
    RunMetadata,
    execution_intent_from_dict,
    execution_intent_from_json,
    execution_intent_to_broker_order,
    to_dict,
    to_json,
    validate_execution_intent,
)


# Deterministic, side-effect-free context objects (no needs_human by default).
SAFE_RISK = RiskSummary(
    risk_id="risk-soh",
    signal_id="sig-soh",
    risk_score=0.10,
    max_position_size=10.0,
)
SAFE_RUN = RunMetadata(
    run_id="run-soh",
    source_system="structured-output-harness",
    created_at="2026-05-31T00:00:00Z",
)


def base_intent(**overrides: object) -> ExecutionIntent:
    """A minimal, valid, dry-run intent. Override fields per case."""
    intent = ExecutionIntent(
        intent_id="intent-soh",
        signal_id="sig-soh",
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=10.0,
        created_at="2026-05-31T00:00:01Z",
    )
    return replace(intent, **overrides) if overrides else intent


def assert_allowed(intent: ExecutionIntent, label: str) -> None:
    result = validate_execution_intent(intent, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is True, f"[{label}] expected ALLOWED, got reasons={result.reasons}"
    assert result.normalized_intent is not None, f"[{label}] allowed but no normalized intent"


def assert_blocked(intent: ExecutionIntent, label: str, *, expect_reason: str | None = None,
                   risk: RiskSummary | None = SAFE_RISK, run: RunMetadata | None = SAFE_RUN,
                   expect_needs_human: bool = False) -> None:
    result = validate_execution_intent(intent, risk=risk, run=run)
    assert result.allowed is False, f"[{label}] expected BLOCKED, but validation allowed it"
    assert result.normalized_intent is None, f"[{label}] blocked but produced a normalized intent"
    if expect_reason is not None:
        assert any(expect_reason in r for r in result.reasons), (
            f"[{label}] expected reason containing {expect_reason!r}, got {result.reasons}"
        )
    if expect_needs_human:
        assert result.needs_human is True, f"[{label}] expected needs_human, got {result}"
    # Conversion guard: an invalid intent must never become a broker order.
    try:
        execution_intent_to_broker_order(intent, risk=risk, run=run)
    except ValueError:
        pass
    else:
        raise AssertionError(f"[{label}] invalid intent was converted into a broker order")


def check_approved_booleans() -> None:
    # approved=True (bool) alone is NOT enough: approver metadata is required.
    assert_blocked(
        base_intent(approved=True),
        "approved=true(bool) without approver",
        expect_reason="approved_by and approved_at",
    )
    # approved=True (bool) with approver metadata is safe (still dry_run by default).
    assert_allowed(
        base_intent(approved=True, approved_by="reviewer", approved_at="2026-05-31T00:00:02Z"),
        "approved=true(bool) with approver",
    )
    # Ambiguous truthy types must fail closed (never coerced to True).
    assert_blocked(base_intent(approved="true"), "approved='true'(str)", expect_reason="approved must be boolean")
    assert_blocked(base_intent(approved=1), "approved=1(int)", expect_reason="approved must be boolean")
    assert_blocked(base_intent(approved=None), "approved=None", expect_reason="approved must be boolean")
    # Missing approved deserializes to the safe default (False) and stays safe.
    missing_approved = execution_intent_from_dict({
        "intent_id": "intent-soh",
        "signal_id": "sig-soh",
        "broker": "alpaca",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 10.0,
        "created_at": "2026-05-31T00:00:01Z",
    })
    assert missing_approved.approved is False, "missing approved must default to False"
    assert_allowed(missing_approved, "missing approved -> default False")


def check_dry_run_booleans() -> None:
    assert_allowed(base_intent(dry_run=True), "dry_run=true(bool)")
    # dry_run=False (bool) requires full strict approval shape; bare False blocks.
    assert_blocked(
        base_intent(dry_run=False),
        "dry_run=false(bool) unapproved",
        expect_reason="dry_run=False requires approved=True",
    )
    # The ONLY non-dry-run shape that can pass.
    assert_allowed(
        base_intent(
            dry_run=False,
            approved=True,
            approved_by="reviewer",
            approved_at="2026-05-31T00:00:02Z",
        ),
        "dry_run=false fully approved (only passing non-dry-run shape)",
    )
    # Ambiguous types fail closed (string/int never coerced to False).
    assert_blocked(base_intent(dry_run="false"), "dry_run='false'(str)", expect_reason="dry_run must be boolean")
    assert_blocked(base_intent(dry_run=0), "dry_run=0(int)", expect_reason="dry_run must be boolean")
    # Missing dry_run deserializes to the safe default (True).
    missing_dry_run = execution_intent_from_dict({
        "intent_id": "intent-soh",
        "signal_id": "sig-soh",
        "broker": "alpaca",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 10.0,
        "created_at": "2026-05-31T00:00:01Z",
    })
    assert missing_dry_run.dry_run is True, "missing dry_run must default to True"
    assert_allowed(missing_dry_run, "missing dry_run -> default True")


def check_approval_metadata() -> None:
    approver = dict(approved=True, approved_by="reviewer", approved_at="2026-05-31T00:00:02Z")
    # Only non-dry-run approval shape that passes.
    assert_allowed(base_intent(dry_run=False, **approver), "non-dry-run full approval passes")
    # Missing approved_by blocks.
    assert_blocked(
        base_intent(dry_run=False, approved=True, approved_at="2026-05-31T00:00:02Z"),
        "non-dry-run missing approved_by",
        expect_reason="approved_by",
    )
    # Missing approved_at blocks.
    assert_blocked(
        base_intent(dry_run=False, approved=True, approved_by="reviewer"),
        "non-dry-run missing approved_at",
        expect_reason="approved_at",
    )
    # approved=False blocks non-dry-run.
    assert_blocked(
        base_intent(dry_run=False, approved=False),
        "non-dry-run approved=False",
        expect_reason="dry_run=False requires approved=True",
    )
    # approved=True from deserialized JSON is NOT enough by itself if metadata unsafe.
    deserialized_unsafe = execution_intent_from_json(
        '{"intent_id":"intent-soh","signal_id":"sig-soh","broker":"alpaca","symbol":"AAPL",'
        '"side":"buy","quantity":10,"created_at":"2026-05-31T00:00:01Z",'
        '"dry_run":false,"approved":true}'
    )
    assert_blocked(
        deserialized_unsafe,
        "deserialized approved=true without approver",
        expect_reason="approved_by",
    )


def check_order_fields() -> None:
    assert_blocked(base_intent(side="hold"), "invalid side", expect_reason="invalid side")
    assert_blocked(base_intent(order_type="stop"), "invalid order_type", expect_reason="invalid order_type")
    assert_blocked(base_intent(broker=""), "missing broker", expect_reason="missing broker")
    assert_blocked(base_intent(symbol=""), "missing symbol", expect_reason="missing symbol")
    # Missing quantity AND missing notional -> quantity invalid blocks.
    assert_blocked(
        base_intent(quantity=0.0, metadata={}),
        "missing quantity and notional",
        expect_reason="quantity must be positive",
    )
    assert_blocked(base_intent(quantity=0.0), "quantity<=0 (zero)", expect_reason="quantity must be positive")
    assert_blocked(base_intent(quantity=-5.0), "quantity<=0 (negative)", expect_reason="quantity must be positive")
    assert_blocked(
        base_intent(metadata={"notional": 0}),
        "notional<=0 (zero)",
        expect_reason="notional must be positive",
    )
    assert_blocked(
        base_intent(metadata={"notional": -100}),
        "notional<=0 (negative)",
        expect_reason="notional must be positive",
    )
    # Limit order without a valid limit_price blocks.
    assert_blocked(
        base_intent(order_type="limit", limit_price=None),
        "limit order missing limit_price",
        expect_reason="limit_price must be positive",
    )
    assert_blocked(
        base_intent(order_type="limit", limit_price=0.0),
        "limit order limit_price<=0",
        expect_reason="limit_price must be positive",
    )
    # Bad limit_price type fails closed.
    assert_blocked(
        base_intent(order_type="limit", limit_price="not-a-number"),
        "limit order bad limit_price type",
        expect_reason="limit_price must be positive",
    )


def check_needs_human() -> None:
    risk_human = RiskSummary(
        risk_id="risk-human",
        signal_id="sig-soh",
        risk_score=0.99,
        max_position_size=1.0,
        needs_human=True,
        needs_human_reason="manual override required",
    )
    assert_blocked(
        base_intent(),
        "risk.needs_human=True blocks",
        risk=risk_human,
        run=SAFE_RUN,
        expect_needs_human=True,
    )
    run_human = RunMetadata(
        run_id="run-human",
        source_system="structured-output-harness",
        created_at="2026-05-31T00:00:00Z",
        needs_human=True,
        needs_human_reason="halt requested",
    )
    assert_blocked(
        base_intent(),
        "run.needs_human=True blocks",
        risk=SAFE_RISK,
        run=run_human,
        expect_needs_human=True,
    )


def check_conversion_guard() -> None:
    # Conversion only succeeds for a validated, safe intent.
    safe = base_intent()
    order = execution_intent_to_broker_order(safe, risk=SAFE_RISK, run=SAFE_RUN)
    assert order["symbol"] == "AAPL" and order["side"] == "buy", "safe conversion mismatch"

    # Each unsafe shape must raise before producing an order (no silent passthrough).
    unsafe_intents = [
        ("string approved", base_intent(approved="true")),
        ("string dry_run", base_intent(dry_run="false")),
        ("bad side", base_intent(side="hold")),
        ("non-dry-run unapproved", base_intent(dry_run=False, approved=False)),
    ]
    for label, intent in unsafe_intents:
        try:
            execution_intent_to_broker_order(intent, risk=SAFE_RISK, run=SAFE_RUN)
        except ValueError:
            continue
        raise AssertionError(f"[conversion guard: {label}] unsafe intent produced a broker order")


def check_serialization_roundtrip() -> None:
    # Safe dry-run intent survives dict + JSON roundtrips and still validates.
    safe = base_intent()
    via_dict = execution_intent_from_dict(to_dict(safe))
    assert_allowed(via_dict, "safe intent dict roundtrip")
    via_json = execution_intent_from_json(to_json(safe))
    assert_allowed(via_json, "safe intent json roundtrip")
    assert via_json.dry_run is True and via_json.approved is False, "roundtrip mutated safe defaults"

    # Unsafe string/int booleans must NOT become executable approval after roundtrip.
    unsafe_payload = {
        "intent_id": "intent-soh",
        "signal_id": "sig-soh",
        "broker": "alpaca",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 10,
        "created_at": "2026-05-31T00:00:01Z",
        "dry_run": "false",
        "approved": "true",
        "approved_by": "reviewer",
        "approved_at": "2026-05-31T00:00:02Z",
    }
    from_dict_unsafe = execution_intent_from_dict(unsafe_payload)
    assert from_dict_unsafe.dry_run == "false", "string dry_run unexpectedly coerced on load"
    assert from_dict_unsafe.approved == "true", "string approved unexpectedly coerced on load"
    assert_blocked(from_dict_unsafe, "unsafe string-bool dict roundtrip fails closed")

    from_json_unsafe = execution_intent_from_json(to_json(from_dict_unsafe))
    assert_blocked(from_json_unsafe, "unsafe string-bool json roundtrip fails closed")


def main() -> None:
    check_approved_booleans()
    check_dry_run_booleans()
    check_approval_metadata()
    check_order_fields()
    check_needs_human()
    check_conversion_guard()
    check_serialization_roundtrip()
    print("execution_intent_structured_output smoke: ok")


if __name__ == "__main__":
    main()
