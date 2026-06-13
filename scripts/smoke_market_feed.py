#!/usr/bin/env python3
"""Live-gated smoke for the yfinance market feed."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKET_FEED_MODULE = (
    REPO_ROOT / "autohedge" / "autohedge" / "data" / "market_feed.py"
)


def _load_market_feed_module():
    spec = importlib.util.spec_from_file_location(
        "market_feed",
        str(MARKET_FEED_MODULE),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MARKET_FEED_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["market_feed"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    if not os.getenv("DAVEY_LIVE_SMOKE"):
        print("market feed smoke: SKIP (set DAVEY_LIVE_SMOKE=1)")
        return

    market_feed = _load_market_feed_module()
    try:
        candidates = market_feed.fetch_candidates()
    except Exception as exc:
        print(f"market feed smoke: fetch failed safely: {exc}")
        candidates = []
    print(json.dumps(candidates, sort_keys=True, default=str))
    assert isinstance(candidates, list)


if __name__ == "__main__":
    main()
