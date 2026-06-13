#!/usr/bin/env python3
"""Step 3 smoke checks for broker/contract consistency + safety hardening."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOHEDGE_SRC = REPO_ROOT / "autohedge" / "autohedge"
BROKERS_DIR = AUTOHEDGE_SRC / "brokers"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap_broker_modules() -> dict[str, Any]:
    # Avoid importing autohedge/__init__.py (heavy runtime deps) and load
    # only broker-layer modules under a lightweight package shell.
    autohedge_pkg = types.ModuleType("autohedge")
    autohedge_pkg.__path__ = [str(AUTOHEDGE_SRC)]
    brokers_pkg = types.ModuleType("autohedge.brokers")
    brokers_pkg.__path__ = [str(BROKERS_DIR)]
    sys.modules["autohedge"] = autohedge_pkg
    sys.modules["autohedge.brokers"] = brokers_pkg

    base_mod = _load_module("autohedge.brokers.base_agent", BROKERS_DIR / "base_agent.py")
    robinhood_state_mod = _load_module(
        "autohedge.brokers.robinhood_state_agent",
        BROKERS_DIR / "robinhood_state_agent.py",
    )
    paper_mod = _load_module("autohedge.brokers.paper_agent", BROKERS_DIR / "paper_agent.py")
    robinhood_mod = _load_module("autohedge.brokers.robinhood_agent", BROKERS_DIR / "robinhood_agent.py")
    solana_mod = _load_module("autohedge.brokers.solana_agent", BROKERS_DIR / "solana_agent.py")
    alpaca_mod = _load_module("autohedge.brokers.alpaca_agent", BROKERS_DIR / "alpaca_agent.py")
    factory_mod = _load_module("autohedge.brokers.factory_agent", BROKERS_DIR / "factory_agent.py")
    init_mod = _load_module("autohedge.brokers", BROKERS_DIR / "__init__.py")
    return {
        "base": base_mod,
        "robinhood_state": robinhood_state_mod,
        "paper": paper_mod,
        "robinhood": robinhood_mod,
        "solana": solana_mod,
        "alpaca": alpaca_mod,
        "factory": factory_mod,
        "init": init_mod,
    }


def _expect_value_error(fn: Callable[[], Any], *, includes: str) -> None:
    try:
        fn()
    except ValueError as exc:
        assert includes in str(exc), f"Expected {includes!r} in: {exc}"
        return
    raise AssertionError("Expected ValueError")


def main() -> None:
    from contracts.bridge_contract import ExecutionIntent, execution_intent_from_json, validate_execution_intent

    mods = _bootstrap_broker_modules()
    base_mod = mods["base"]
    robinhood_state_mod = mods["robinhood_state"]
    paper_mod = mods["paper"]
    robinhood_mod = mods["robinhood"]
    solana_mod = mods["solana"]
    alpaca_mod = mods["alpaca"]
    factory_mod = mods["factory"]
    init_mod = mods["init"]

    # A) all broker *Agent imports/exports remain available.
    required_agents: dict[Any, tuple[str, ...]] = {
        base_mod: (
            "BrokerAgent",
            "BrokerOrderAgent",
            "BrokerFillAgent",
            "BrokerPositionAgent",
            "AccountSnapshotAgent",
        ),
        paper_mod: ("PaperStateAgent", "PaperStateStoreAgent", "PaperBrokerAgent"),
        robinhood_state_mod: ("RobinhoodStateAgent", "RobinhoodStateStoreAgent"),
        robinhood_mod: ("RobinhoodBrokerAgent",),
        solana_mod: ("SolanaBrokerAgent",),
        alpaca_mod: ("AlpacaBrokerAgent",),
    }
    for module, agent_names in required_agents.items():
        for agent_name in agent_names:
            assert hasattr(module, agent_name), f"Missing {agent_name} in {module.__name__}"

    exported = set(getattr(init_mod, "__all__", []))
    for agent_name in (
        "BrokerAgent",
        "BrokerOrderAgent",
        "BrokerFillAgent",
        "BrokerPositionAgent",
        "AccountSnapshotAgent",
        "PaperStateAgent",
        "PaperStateStoreAgent",
        "PaperBrokerAgent",
        "RobinhoodStateAgent",
        "RobinhoodStateStoreAgent",
        "RobinhoodBrokerAgent",
        "SolanaBrokerAgent",
        "AlpacaBrokerAgent",
        "AlpacaLiveBroker",
    ):
        assert agent_name in exported, f"Missing package export for {agent_name}"

    # B) backward-compatible *Boi aliases remain stable.
    assert base_mod.BrokerBoi is base_mod.BrokerAgent
    assert base_mod.BrokerOrderBoi is base_mod.BrokerOrderAgent
    assert base_mod.BrokerFillBoi is base_mod.BrokerFillAgent
    assert base_mod.BrokerPositionBoi is base_mod.BrokerPositionAgent
    assert base_mod.AccountSnapshotBoi is base_mod.AccountSnapshotAgent
    assert paper_mod.PaperStateBoi is paper_mod.PaperStateAgent
    assert paper_mod.PaperStateStoreBoi is paper_mod.PaperStateStoreAgent
    assert paper_mod.PaperBrokerBoi is paper_mod.PaperBrokerAgent
    assert robinhood_state_mod.RobinhoodStateBoi is robinhood_state_mod.RobinhoodStateAgent
    assert robinhood_state_mod.RobinhoodStateStoreBoi is robinhood_state_mod.RobinhoodStateStoreAgent
    assert robinhood_mod.RobinhoodBrokerBoi is robinhood_mod.RobinhoodBrokerAgent
    assert solana_mod.SolanaBrokerBoi is solana_mod.SolanaBrokerAgent
    assert alpaca_mod.AlpacaBrokerBoi is alpaca_mod.AlpacaBrokerAgent

    # C) factory registration/normalization/error paths remain deterministic.
    registry_keys = set(factory_mod.BROKER_AGENT_REGISTRY.keys())
    assert {"alpaca", "paper", "robinhood", "solana"}.issubset(registry_keys)
    from_factory = factory_mod.get_broker_agent("  AlPaCa  ", config={"ALPACA_DRY_RUN": "true"})
    assert isinstance(from_factory, alpaca_mod.AlpacaBrokerAgent)
    _expect_value_error(
        lambda: factory_mod.get_broker_agent("unsupported-broker"),
        includes="Unknown broker agent",
    )
    _expect_value_error(
        lambda: factory_mod.get_broker_agent("   "),
        includes="non-empty string",
    )
    _expect_value_error(
        lambda: factory_mod.get_broker_agent(None),
        includes="non-empty string",
    )

    # D) asset_class normalization edge cases.
    asset_class_cases = [
        (None, "stock"),
        ("", "stock"),
        ("crypto", "crypto"),
        ("CRYPTO", "crypto"),
        ("CrYpTo", "crypto"),
    ]
    for raw_asset_class, expected_asset_class in asset_class_cases:
        order = base_mod.BrokerOrderAgent(
            symbol="AAPL",
            side="buy",
            quantity=1.0,
            asset_class=raw_asset_class,
        )
        fill = base_mod.BrokerFillAgent(
            order_id="order-1",
            symbol="AAPL",
            side="buy",
            quantity=1.0,
            asset_class=raw_asset_class,
        )
        position = base_mod.BrokerPositionAgent(
            symbol="AAPL",
            quantity=1.0,
            asset_class=raw_asset_class,
        )
        assert order.asset_class == expected_asset_class
        assert fill.asset_class == expected_asset_class
        assert position.asset_class == expected_asset_class

    # E/F/G) ExecutionIntent safety checks stay fail-closed and dry-run by default.
    default_intent = ExecutionIntent(
        intent_id="intent-step3-default",
        signal_id="sig-step3",
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        created_at="2026-05-31T04:00:00Z",
    )
    assert default_intent.dry_run is True  # F
    from_json_default = execution_intent_from_json(
        '{"intent_id":"intent-step3-json-default","signal_id":"sig-step3","broker":"alpaca","symbol":"AAPL","side":"buy","quantity":1.0,"created_at":"2026-05-31T04:00:01Z"}'
    )
    assert from_json_default.dry_run is True  # F

    alpaca = alpaca_mod.AlpacaBrokerAgent(config={"ALPACA_DRY_RUN": "true"})
    assert alpaca.dry_run is True  # F

    _expect_value_error(
        lambda: alpaca.place_execution_intent(default_intent),
        includes="approved=True required",
    )  # E
    assert alpaca.live_call_attempts == 0

    approved_intent = ExecutionIntent(
        intent_id="intent-step3-approved",
        signal_id="sig-step3",
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        created_at="2026-05-31T04:00:02Z",
        dry_run=True,
        approved=True,
        approved_by="smoke-test",
        approved_at="2026-05-31T04:00:03Z",
    )
    approved_result = alpaca.place_execution_intent(approved_intent)
    assert approved_result["status"] == "simulated"
    assert approved_result["dry_run"] is True
    assert approved_result["live_call_made"] is False
    assert alpaca.live_call_attempts == 0

    missing_approval_metadata = ExecutionIntent(
        intent_id="intent-step3-missing-approval",
        signal_id="sig-step3",
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        created_at="2026-05-31T04:00:04Z",
        dry_run=False,
        approved=True,
        approved_by="",
        approved_at="",
    )
    blocked = validate_execution_intent(missing_approval_metadata)
    assert blocked.allowed is False
    assert any("approved_by" in reason for reason in blocked.reasons)
    assert any("approved_at" in reason for reason in blocked.reasons)

    ambiguous_dry_run = execution_intent_from_json(
        '{"intent_id":"intent-step3-ambiguous-dry-run","signal_id":"sig-step3","broker":"alpaca","symbol":"AAPL","side":"buy","quantity":1.0,"created_at":"2026-05-31T04:00:05Z","dry_run":"false","approved":true,"approved_by":"smoke-test","approved_at":"2026-05-31T04:00:06Z"}'
    )
    blocked = validate_execution_intent(ambiguous_dry_run)
    assert blocked.allowed is False
    assert any("dry_run must be boolean" in reason for reason in blocked.reasons)

    ambiguous_approved = execution_intent_from_json(
        '{"intent_id":"intent-step3-ambiguous-approved","signal_id":"sig-step3","broker":"alpaca","symbol":"AAPL","side":"buy","quantity":1.0,"created_at":"2026-05-31T04:00:07Z","dry_run":true,"approved":"true","approved_by":"smoke-test","approved_at":"2026-05-31T04:00:08Z"}'
    )
    blocked = validate_execution_intent(ambiguous_approved)
    assert blocked.allowed is False
    assert any("approved must be boolean" in reason for reason in blocked.reasons)

    _expect_value_error(
        lambda: alpaca.place_execution_intent({"intent_id": "not-a-valid-intent"}),
        includes="unable to validate intent safely",
    )
    assert alpaca.live_call_attempts == 0

    # H) Reviewed Alpaca live wrapper can submit through an injected fake
    # submitter only after live-mode, approval, credentials, and $200 cap gates.
    old_env = {
        key: os.environ.get(key)
        for key in (
            "DAVEY_LIVE_MODE",
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
            "ALPACA_LIVE_TRADING",
        )
    }
    try:
        os.environ["DAVEY_LIVE_MODE"] = "1"
        os.environ["ALPACA_API_KEY"] = "paper-key"
        os.environ["ALPACA_SECRET_KEY"] = "paper-secret"
        os.environ.pop("ALPACA_LIVE_TRADING", None)
        submissions: list[dict[str, Any]] = []

        def fake_submitter(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
            submissions.append({"url": url, "payload": payload, "headers": headers})
            return {
                "id": "paper-order-0001",
                "status": "accepted",
                "submitted_at": "2026-05-31T04:00:10Z",
                "symbol": payload["symbol"],
                "side": payload["side"],
                "qty": payload["qty"],
                "type": payload["type"],
                "limit_price": payload.get("limit_price"),
            }

        with tempfile.TemporaryDirectory(prefix="alpaca-live-wrapper-") as tmp:
            live_broker = init_mod.AlpacaLiveBroker(
                session_id="smoke-live",
                artifact_root=Path(tmp) / "logs" / "audit",
                submitter=fake_submitter,
            )
            live_intent = ExecutionIntent(
                intent_id="intent-step3-live-paper",
                signal_id="sig-step3",
                broker="alpaca",
                symbol="NVDA",
                side="buy",
                quantity=1.0,
                order_type="limit",
                limit_price=100.0,
                created_at="2026-05-31T04:00:09Z",
                dry_run=False,
                approved=True,
                approved_by="smoke-test",
                approved_at="2026-05-31T04:00:09Z",
                metadata={"estimated_price": 100.0},
            )
            fill = live_broker.submit_order(live_intent)
            assert fill.status == "accepted"
            assert fill.dry_run is False
            assert fill.metadata["paper_trading"] is True
            assert submissions[0]["url"].startswith("https://paper-api.alpaca.markets")
            fill_path = (
                Path(tmp)
                / "logs"
                / "audit"
                / "smoke-live"
                / "fill-alpaca-paper-order-0001.json"
            )
            assert fill_path.exists()

            too_large = ExecutionIntent(
                intent_id="intent-step3-too-large",
                signal_id="sig-step3",
                broker="alpaca",
                symbol="NVDA",
                side="buy",
                quantity=3.0,
                order_type="limit",
                limit_price=100.0,
                created_at="2026-05-31T04:00:11Z",
                dry_run=False,
                approved=True,
                approved_by="smoke-test",
                approved_at="2026-05-31T04:00:11Z",
            )
            _expect_value_error(
                lambda: live_broker.submit_order(too_large),
                includes="exceeds hard $200 cap",
            )
            assert len(submissions) == 1
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print("brokers step3 smoke: ok")


if __name__ == "__main__":
    main()
