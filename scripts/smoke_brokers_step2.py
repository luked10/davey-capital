#!/usr/bin/env python3
"""Step 2 smoke checks for broker naming + Alpaca scaffold."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

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


def _bootstrap_broker_modules():
    # Avoid importing autohedge/__init__.py (which has heavy runtime deps)
    # and load only broker-layer modules under a lightweight package shell.
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


def main() -> None:
    from contracts.bridge_contract import ExecutionIntent

    mods = _bootstrap_broker_modules()
    base_mod = mods["base"]
    paper_mod = mods["paper"]
    robinhood_mod = mods["robinhood"]
    solana_mod = mods["solana"]
    alpaca_mod = mods["alpaca"]
    factory_mod = mods["factory"]
    init_mod = mods["init"]

    # existing paper/robinhood/solana imports still work
    assert hasattr(paper_mod, "PaperBrokerAgent")
    assert hasattr(robinhood_mod, "RobinhoodBrokerAgent")
    assert hasattr(solana_mod, "SolanaBrokerAgent")

    # *Boi aliases still work
    assert base_mod.BrokerOrderBoi is base_mod.BrokerOrderAgent
    assert base_mod.BrokerFillBoi is base_mod.BrokerFillAgent
    assert base_mod.BrokerPositionBoi is base_mod.BrokerPositionAgent
    assert base_mod.AccountSnapshotBoi is base_mod.AccountSnapshotAgent
    assert paper_mod.PaperBrokerBoi is paper_mod.PaperBrokerAgent
    assert robinhood_mod.RobinhoodBrokerBoi is robinhood_mod.RobinhoodBrokerAgent
    assert solana_mod.SolanaBrokerBoi is solana_mod.SolanaBrokerAgent
    assert alpaca_mod.AlpacaBrokerBoi is alpaca_mod.AlpacaBrokerAgent

    # Alpaca adapter can instantiate without secrets in dry_run mode
    alpaca = alpaca_mod.AlpacaBrokerAgent(config={"ALPACA_DRY_RUN": "true"})
    assert alpaca.dry_run is True
    assert alpaca.api_key == ""
    assert alpaca.api_secret == ""
    assert alpaca.live_call_attempts == 0

    # Registry wiring and package exports include alpaca
    assert "alpaca" in factory_mod.BROKER_AGENT_REGISTRY
    by_factory = factory_mod.get_broker_agent("alpaca", config={"ALPACA_DRY_RUN": "true"})
    assert isinstance(by_factory, alpaca_mod.AlpacaBrokerAgent)
    assert "AlpacaBrokerAgent" in getattr(init_mod, "__all__", [])

    # No live Alpaca API calls happen in smoke tests.
    order = base_mod.BrokerOrderAgent(
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        order_type="market",
    )
    result = alpaca.place_order(order)
    assert result["status"] == "simulated"
    assert result["dry_run"] is True
    assert result["live_call_made"] is False
    assert alpaca.live_call_attempts == 0

    # Unapproved ExecutionIntent cannot execute.
    unapproved = ExecutionIntent(
        intent_id="intent-step2-unsafe",
        signal_id="sig-step2",
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        created_at="2026-05-31T03:40:00Z",
        dry_run=True,
        approved=False,
    )
    blocked = False
    try:
        alpaca.place_execution_intent(unapproved)
    except ValueError:
        blocked = True
    assert blocked is True
    assert alpaca.live_call_attempts == 0

    # Approved dry-run ExecutionIntent can execute safely.
    approved = ExecutionIntent(
        intent_id="intent-step2-safe",
        signal_id="sig-step2",
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=1.0,
        created_at="2026-05-31T03:41:00Z",
        dry_run=True,
        approved=True,
        approved_by="smoke-test",
        approved_at="2026-05-31T03:41:05Z",
    )
    approved_result = alpaca.place_execution_intent(approved)
    assert approved_result["status"] == "simulated"
    assert approved_result["dry_run"] is True
    assert approved_result["live_call_made"] is False
    assert alpaca.live_call_attempts == 0

    print("brokers step2 smoke: ok")


if __name__ == "__main__":
    main()
