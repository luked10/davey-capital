#!/usr/bin/env python3
"""Guarded live smoke for Alpaca paper/live wrapper.

Offline tests for the market-order price-fetch + cap logic run unconditionally.

Live Alpaca network calls run only when DAVEY_LIVE_SMOKE=1 and the required
env vars are set. Uses Alpaca paper trading by default; do not set
ALPACA_LIVE_TRADING=1 unless intentionally testing real-money routing.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
AUTOHEDGE_PACKAGE_ROOT = REPO_ROOT / "autohedge"
if str(AUTOHEDGE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOHEDGE_PACKAGE_ROOT))

from contracts.bridge_contract import ExecutionIntent, to_dict


def _load_alpaca_live_module():
    module_path = REPO_ROOT / "autohedge" / "autohedge" / "brokers" / "alpaca_live.py"
    spec = importlib.util.spec_from_file_location("alpaca_live_smoke", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["alpaca_live_smoke"] = module
    spec.loader.exec_module(module)
    return module


def _make_market_intent(intent_id: str, quantity: float) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id=intent_id,
        signal_id="smoke",
        broker="alpaca",
        symbol="NVDA",
        side="buy",
        quantity=quantity,
        order_type="market",
        created_at="2026-06-15T00:00:00Z",
        dry_run=False,
        approved=True,
        approved_by="smoke",
        approved_at="2026-06-15T00:00:01Z",
    )


def _smoke_offline_market_price_fetch(alpaca_live_module) -> None:
    """Offline tests for the market-order price-fetch + cap logic.

    Injects a fake _fetch_market_price to exercise quantity adjustment and
    fail-closed paths without network access.
    """
    AlpacaLiveBroker = alpaca_live_module.AlpacaLiveBroker
    MAX_CAP = alpaca_live_module.MAX_ORDER_NOTIONAL_USD

    with tempfile.TemporaryDirectory(prefix="alpaca-offline-smoke-") as tmp:
        artifact_root = Path(tmp) / "logs" / "audit"

        def make_broker(price_fn):
            """Construct a bare AlpacaLiveBroker with a stubbed _fetch_market_price."""
            from autohedge.audit.artifacts import AuditArtifactWriter
            b = object.__new__(AlpacaLiveBroker)
            b.session_id = "smoke-offline"
            b.artifact_root = artifact_root
            b.api_key = "fake-key"
            b.api_secret = "fake-secret"
            b.live_mode = True
            b.live_trading = False
            b.base_url = alpaca_live_module.ALPACA_PAPER_URL
            b._writer = AuditArtifactWriter(
                session_id="smoke-offline",
                artifact_root=artifact_root,
                model="",
                provider="alpaca_live",
            )
            b._fetch_market_price = price_fn
            return b

        # A) Under cap — no quantity adjustment.
        # 3 shares × $50 = $150 < $200
        broker_a = make_broker(lambda sym: 50.0)
        order_a, notional_a = broker_a._validate_for_submission(
            _make_market_intent("smoke-a", quantity=3.0)
        )
        assert float(order_a["quantity"]) == 3.0, (
            f"qty should not change under cap, got {order_a['quantity']}"
        )
        assert abs(notional_a - 150.0) < 0.01, f"notional should be 150, got {notional_a}"

        # B) Over cap — quantity reduced to floor(200/price).
        # 5 shares × $80 = $400 > $200 → adjusted to floor(200/80) = 2 shares
        broker_b = make_broker(lambda sym: 80.0)
        order_b, notional_b = broker_b._validate_for_submission(
            _make_market_intent("smoke-b", quantity=5.0)
        )
        expected_qty_b = max(0.001, math.floor(MAX_CAP / 80.0))  # 2
        assert float(order_b["quantity"]) == float(expected_qty_b), (
            f"expected qty={expected_qty_b}, got {order_b['quantity']}"
        )
        assert notional_b <= MAX_CAP, f"notional {notional_b} exceeds cap {MAX_CAP}"

        # C) Price so high that even 1 share > cap → minimum qty 0.001.
        # 2 shares × $500 = $1000 > $200 → floor(200/500) = 0 → min 0.001
        broker_c = make_broker(lambda sym: 500.0)
        order_c, notional_c = broker_c._validate_for_submission(
            _make_market_intent("smoke-c", quantity=2.0)
        )
        assert float(order_c["quantity"]) == 0.001, (
            f"expected minimum 0.001, got {order_c['quantity']}"
        )
        assert notional_c <= MAX_CAP

        # D) yfinance fetch failure blocks the order (fail closed).
        def raise_on_fetch(sym):
            raise ValueError("simulated yfinance network failure")

        broker_d = make_broker(raise_on_fetch)
        blocked = False
        try:
            broker_d._validate_for_submission(
                _make_market_intent("smoke-d", quantity=1.0)
            )
        except ValueError as exc:
            blocked = True
            assert "simulated" in str(exc) or "price fetch" in str(exc).lower(), (
                f"unexpected error message: {exc}"
            )
        assert blocked, "expected ValueError when price fetch fails"

    print("alpaca live smoke (offline market price logic): ok")


def main() -> None:
    alpaca_live = _load_alpaca_live_module()

    # Offline tests always run — no network or credentials needed.
    _smoke_offline_market_price_fetch(alpaca_live)

    if os.getenv("DAVEY_LIVE_SMOKE", "").strip() != "1":
        print("alpaca live smoke (live Alpaca): skipped (set DAVEY_LIVE_SMOKE=1)")
        return
    if os.getenv("DAVEY_LIVE_MODE", "").strip() != "1":
        raise RuntimeError("DAVEY_LIVE_MODE=1 is required for Alpaca execution smoke")
    if not os.getenv("ALPACA_API_KEY") or not (
        os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_API_SECRET")
    ):
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY or ALPACA_API_SECRET are required"
        )

    with tempfile.TemporaryDirectory(prefix="alpaca-live-smoke-") as tmp:
        broker = alpaca_live.AlpacaLiveBroker(
            session_id="alpaca-live-smoke",
            artifact_root=Path(tmp) / "logs" / "audit",
        )

        # A) Limit order — price known from metadata; existing path.
        intent_limit = ExecutionIntent(
            intent_id="alpaca-live-smoke-nvda-limit",
            signal_id="alpaca-live-smoke",
            broker="alpaca",
            symbol="NVDA",
            side="buy",
            quantity=1.0,
            order_type="limit",
            limit_price=1.0,
            created_at="2026-06-13T00:00:00Z",
            dry_run=False,
            approved=True,
            approved_by="alpaca-live-smoke",
            approved_at="2026-06-13T00:00:01Z",
            metadata={"estimated_price": 1.0, "notional": 1.0},
        )
        fill_limit = broker.submit_order(intent_limit)
        assert fill_limit.status, "fill record must include status"
        print("alpaca live smoke (limit order) fill:")
        print(json.dumps(to_dict(fill_limit), indent=2, sort_keys=True))

        # B) Market order — price fetched from yfinance, quantity capped at $200.
        #    Request a deliberately oversized quantity so the broker must reduce it.
        intent_market = ExecutionIntent(
            intent_id="alpaca-live-smoke-nvda-market",
            signal_id="alpaca-live-smoke",
            broker="alpaca",
            symbol="NVDA",
            side="buy",
            quantity=1000.0,
            order_type="market",
            created_at="2026-06-13T00:00:00Z",
            dry_run=False,
            approved=True,
            approved_by="alpaca-live-smoke",
            approved_at="2026-06-13T00:00:01Z",
        )
        fill_market = broker.submit_order(intent_market)
        assert fill_market.status, "market order fill must include status"
        assert fill_market.quantity <= alpaca_live.MAX_ORDER_NOTIONAL_USD, (
            f"adjusted quantity {fill_market.quantity} exceeds cap expectation"
        )
        print("alpaca live smoke (market order) fill:")
        print(json.dumps(to_dict(fill_market), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
