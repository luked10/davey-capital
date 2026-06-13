#!/usr/bin/env python3
"""Gated live-mode smoke for local circuit breaker observation wiring.

This smoke performs no broker calls and no network access. It is gated because
it exercises the live circuit-breaker path intended for DAVEY_ROOT wiring.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
CIRCUIT_BREAKER_PATH = REPO_ROOT / "autohedge" / "autohedge" / "risk" / "circuit_breaker.py"
OBSERVATIONS_PATH = REPO_ROOT / "autohedge" / "autohedge" / "risk" / "observations.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    if os.getenv("DAVEY_LIVE_SMOKE", "").strip() != "1":
        print("circuit breaker live smoke: skipped (set DAVEY_LIVE_SMOKE=1)")
        return

    cb = _load_module("circuit_breaker_live_smoke", CIRCUIT_BREAKER_PATH)
    observations_mod = _load_module("observations_live_smoke", OBSERVATIONS_PATH)

    with tempfile.TemporaryDirectory(prefix="circuit-breaker-live-smoke-") as tmp:
        previous_root = os.environ.get("DAVEY_ROOT")
        os.environ["DAVEY_ROOT"] = tmp
        try:
            observations = observations_mod.build_observations("NVDA")
        finally:
            if previous_root is None:
                os.environ.pop("DAVEY_ROOT", None)
            else:
                os.environ["DAVEY_ROOT"] = previous_root

    assert observations["symbol"] == "NVDA", observations
    assert observations["consecutive_losses"] == 0, observations
    assert observations["daily_loss_pct"] == 0.0, observations
    assert observations["open_trades"] == 0, observations

    config = cb.CircuitBreakerConfig(
        enabled=True,
        max_consecutive_losses=3,
        max_daily_loss_pct=0.02,
        max_open_trades=5,
    )
    result = cb.evaluate_circuit_breaker(config, observations)
    assert result.blocked is False, result
    assert result.allowed is True, result
    assert result.needs_human is False, result

    print("circuit breaker live smoke: ok")


if __name__ == "__main__":
    main()
