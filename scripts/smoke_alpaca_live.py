#!/usr/bin/env python3
"""Guarded live smoke for Alpaca paper/live wrapper.

This script performs a real network call only when DAVEY_LIVE_SMOKE=1. It uses
Alpaca paper trading by default; do not set ALPACA_LIVE_TRADING=1 for this
smoke unless intentionally testing real-money routing.
"""

from __future__ import annotations

import importlib.util
import json
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


def main() -> None:
    if os.getenv("DAVEY_LIVE_SMOKE", "").strip() != "1":
        print("alpaca live smoke: skipped (set DAVEY_LIVE_SMOKE=1)")
        return
    if os.getenv("DAVEY_LIVE_MODE", "").strip() != "1":
        raise RuntimeError("DAVEY_LIVE_MODE=1 is required for Alpaca execution smoke")
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")

    with tempfile.TemporaryDirectory(prefix="alpaca-live-smoke-") as tmp:
        alpaca_live = _load_alpaca_live_module()
        broker = alpaca_live.AlpacaLiveBroker(
            session_id="alpaca-live-smoke",
            artifact_root=Path(tmp) / "logs" / "audit",
        )
        intent = ExecutionIntent(
            intent_id="alpaca-live-smoke-nvda-1",
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
        fill = broker.submit_order(intent)
        assert fill.status, "fill record must include status"
        print("alpaca live smoke fill:")
        print(json.dumps(to_dict(fill), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
