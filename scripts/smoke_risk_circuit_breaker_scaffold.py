#!/usr/bin/env python3
"""Deterministic smoke for the circuit breaker scaffold (Slice B).

No broker calls, no live position reads, no network access, no credentials.
The module under test is loaded directly from its file so the heavyweight
autohedge package __init__ is never imported.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "risk" / "circuit_breaker.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("circuit_breaker", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["circuit_breaker"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    cb = _load_module()
    Config = cb.CircuitBreakerConfig
    evaluate = cb.evaluate_circuit_breaker

    # Default config is disabled and never blocks, even with breach-level inputs.
    default_result = evaluate(
        Config(),
        consecutive_losses=99,
        daily_loss_pct=-50.0,
        open_trades=99,
    )
    assert default_result.allowed is True, default_result
    assert default_result.needs_human is False, default_result
    assert default_result.triggered_rules == [], default_result
    assert "disabled" in default_result.reason

    # None config behaves as disabled default.
    none_result = evaluate(None, consecutive_losses=99)
    assert none_result.allowed is True and none_result.needs_human is False

    # Default values confirm config-only, no-op-by-default posture.
    assert Config().enabled is False
    assert Config().max_consecutive_losses == 3
    assert Config().daily_loss_limit_pct is None
    assert Config().max_open_trades is None

    enabled = Config(
        enabled=True,
        max_consecutive_losses=3,
        daily_loss_limit_pct=2.0,
        max_open_trades=5,
    )

    # Enabled config allows when everything is within limits.
    ok = evaluate(enabled, consecutive_losses=1, daily_loss_pct=-0.5, open_trades=2)
    assert ok.allowed is True and ok.needs_human is False, ok
    assert ok.triggered_rules == []
    assert ok.observed["consecutive_losses"] == 1

    # Consecutive loss breach blocks with needs_human.
    losses = evaluate(enabled, consecutive_losses=3, daily_loss_pct=0.0, open_trades=0)
    assert losses.allowed is False and losses.needs_human is True, losses
    assert any("max_consecutive_losses" in rule for rule in losses.triggered_rules), losses

    # Daily loss breach blocks (loss reported as negative drawdown).
    daily = evaluate(enabled, consecutive_losses=0, daily_loss_pct=-2.5, open_trades=0)
    assert daily.allowed is False and daily.needs_human is True, daily
    assert any("daily_loss_limit_pct" in rule for rule in daily.triggered_rules), daily

    # Max open trades breach blocks.
    trades = evaluate(enabled, consecutive_losses=0, daily_loss_pct=0.0, open_trades=5)
    assert trades.allowed is False and trades.needs_human is True, trades
    assert any("max_open_trades" in rule for rule in trades.triggered_rules), trades

    # Multiple simultaneous breaches all reported.
    multi = evaluate(enabled, consecutive_losses=10, daily_loss_pct=-9.0, open_trades=9)
    assert multi.allowed is False and len(multi.triggered_rules) == 3, multi

    # Malformed observations fail closed with needs_human=True.
    malformed_losses = evaluate(enabled, consecutive_losses="three")
    assert malformed_losses.allowed is False and malformed_losses.needs_human is True
    assert "malformed_observations" in malformed_losses.triggered_rules

    malformed_pct = evaluate(enabled, daily_loss_pct=float("nan"))
    assert malformed_pct.allowed is False and malformed_pct.needs_human is True

    malformed_trades = evaluate(enabled, open_trades=-1)
    assert malformed_trades.allowed is False and malformed_trades.needs_human is True

    bool_losses = evaluate(enabled, consecutive_losses=True)
    assert bool_losses.allowed is False and bool_losses.needs_human is True

    # Malformed config fails closed.
    malformed_config = evaluate("not-a-config")  # type: ignore[arg-type]
    assert malformed_config.allowed is False and malformed_config.needs_human is True
    assert "malformed_config" in malformed_config.triggered_rules

    bad_enabled = evaluate(Config(enabled="yes"))  # type: ignore[arg-type]
    assert bad_enabled.allowed is False and bad_enabled.needs_human is True

    bad_limit = evaluate(
        Config(enabled=True, daily_loss_limit_pct="two"),  # type: ignore[arg-type]
        consecutive_losses=0,
    )
    assert bad_limit.allowed is False and bad_limit.needs_human is True

    # The module must not import any networking or broker machinery.
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "import requests",
        "import httpx",
        "import urllib",
        "import socket",
        "import alpaca",
        "import robinhood",
        "from autohedge.brokers",
    ):
        assert forbidden not in source, (
            f"forbidden import {forbidden!r} found in circuit breaker module"
        )

    print("risk circuit breaker scaffold smoke: ok")


if __name__ == "__main__":
    main()
