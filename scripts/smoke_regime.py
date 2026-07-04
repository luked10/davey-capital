#!/usr/bin/env python3
"""Offline smoke for regime detection + the regime_suppressed injection gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
REGIME_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "risk" / "regime.py"
RUNTIME_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "runtime_scaffold.py"
SERVER_MODULE = REPO_ROOT / "mcp_server" / "server.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stub_mcp() -> None:
    if "mcp" in sys.modules:
        return
    mcp_stub = types.ModuleType("mcp")
    mcp_stub.server = types.ModuleType("mcp.server")
    mcp_stub.server.fastmcp = types.ModuleType("mcp.server.fastmcp")
    sys.modules["mcp"] = mcp_stub
    sys.modules["mcp.server"] = mcp_stub.server
    sys.modules["mcp.server.fastmcp"] = mcp_stub.server.fastmcp


def _classify(regime_mod, **overrides: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "spy_close": 500.0,
        "spy_sma200": 480.0,
        "spy_day_return": 0.2,
        "spy_3d_return": 0.5,
        "vix": 15.0,
    }
    inputs.update(overrides)
    return regime_mod.classify_regime(**inputs)


def main() -> None:
    regime = _load_module("regime_smoke", REGIME_MODULE)

    # --- risk_on baseline ---
    calm = _classify(regime)
    assert calm["regime"] == "risk_on"
    assert calm["allow_new_longs"] is True
    assert calm["size_multiplier"] == 1.0
    assert calm["vix_ok"] is True and calm["spy_trend_ok"] is True

    # --- caution band: VIX 20-25, half size ---
    caution = _classify(regime, vix=22.0)
    assert caution["regime"] == "caution"
    assert caution["allow_new_longs"] is True
    assert caution["size_multiplier"] == 0.5

    # --- VIX 26 -> risk_off, NO new longs (checkpoint 4 requirement) ---
    vix26 = _classify(regime, vix=26.0)
    assert vix26["regime"] == "risk_off"
    assert vix26["allow_new_longs"] is False
    assert vix26["size_multiplier"] == 0.0
    assert vix26["vix_ok"] is False

    # --- SPY below 200D MA -> risk_off even with a calm VIX ---
    below_ma = _classify(regime, spy_close=470.0, spy_sma200=480.0, vix=15.0)
    assert below_ma["regime"] == "risk_off"
    assert below_ma["allow_new_longs"] is False
    assert below_ma["spy_trend_ok"] is False

    # --- Fast risk-off on day/-3d drawdowns ---
    fast_day = _classify(regime, spy_day_return=-1.6)
    assert fast_day["regime"] == "fast_risk_off"
    assert fast_day["allow_new_longs"] is False
    fast_3d = _classify(regime, spy_3d_return=-3.1)
    assert fast_3d["regime"] == "fast_risk_off"
    assert fast_3d["size_multiplier"] == 0.0

    # --- Panic dominates everything ---
    panic = _classify(regime, vix=31.0, spy_close=470.0, spy_day_return=-2.0)
    assert panic["regime"] == "panic"
    assert panic["allow_new_longs"] is False
    assert panic["size_multiplier"] == 0.0

    # Boundary precision: VIX exactly 25 is caution (not risk_off), 30 is
    # risk_off (not panic), 20 is caution.
    assert _classify(regime, vix=25.0)["regime"] == "caution"
    assert _classify(regime, vix=30.0)["regime"] == "risk_off"
    assert _classify(regime, vix=20.0)["regime"] == "caution"
    assert _classify(regime, vix=19.99)["regime"] == "risk_on"

    # --- unknown regime fails soft, clearly labeled ---
    unknown = regime.unknown_regime("smoke: no data")
    assert unknown["regime"] == "unknown"
    assert unknown["allow_new_longs"] is True
    assert unknown["size_multiplier"] == 0.5

    # Invalid inputs must raise.
    for bad in ({"vix": float("nan")}, {"spy_close": -1.0}, {"spy_sma200": 0.0}):
        try:
            _classify(regime, **bad)
            raise AssertionError(f"classify_regime accepted {bad!r}")
        except ValueError:
            pass

    # --- SPY/VIX are data-only members of the watch universe ---
    sys.path.insert(0, str(REPO_ROOT / "autohedge"))
    market_feed = _load_module(
        "regime_smoke_market_feed",
        REPO_ROOT / "autohedge" / "autohedge" / "data" / "market_feed.py",
    )
    assert "SPY" in market_feed.WATCH_ONLY
    assert "^VIX" in market_feed.WATCH_ONLY
    assert set(market_feed.DATA_ONLY_SYMBOLS) == {"SPY", "^VIX"}
    # Data-only symbols never auto-ping (watch_only tier has no thresholds).
    assert market_feed.priority_tier("SPY") == "watch_only"
    assert market_feed.TIER_THRESHOLDS.get("watch_only") is None

    # --- Injection gate: VIX 26 -> rejected with reason regime_suppressed ---
    _stub_mcp()
    server = _load_module("davey_regime_smoke_server", SERVER_MODULE)

    with tempfile.TemporaryDirectory(prefix="regime-smoke-") as tmp:
        root = Path(tmp)
        risk_off_snapshot = _classify(regime, vix=26.0)
        service = server.PokeBridgeService(
            repo_root=root,
            regime_provider=lambda: risk_off_snapshot,
        )

        rejected = service.inject_researched_candidate(
            symbol="NVDA",
            thesis="should be suppressed",
            confidence=0.9,
            trigger_reason="regime smoke",
            direction="buy",
        )
        assert rejected["queued"] is False, rejected
        assert rejected["error"] == "regime_suppressed", rejected
        assert rejected["reason"] == "regime_suppressed", rejected
        assert rejected["regime"]["regime"] == "risk_off"
        assert rejected["cooldown"]["reason"] == "regime_suppressed"

        # Nothing reached the poke queue.
        queue_rows = list(root.glob("logs/overnight/*/poke_bridge_queue.jsonl"))
        assert not queue_rows, "regime-suppressed injection must not enqueue"

        # 1-day cooldown persisted to runtime_state.json and now blocks even
        # a risk-on retry.
        saved = json.loads((root / "runtime_state.json").read_text(encoding="utf-8"))
        assert saved["positions_summary"]["cooldowns"]["NVDA"]["reason"] == "regime_suppressed"

        service_riskon = server.PokeBridgeService(
            repo_root=root,
            regime_provider=lambda: _classify(regime),
        )
        cooled = service_riskon.inject_researched_candidate(
            symbol="NVDA",
            thesis="retry during cooldown",
            confidence=0.9,
            trigger_reason="regime smoke",
            direction="sell",
        )
        assert cooled["queued"] is False
        assert "cooling down" in cooled["error"]

        # Sells (exits) pass the regime gate even in risk_off.
        sell_res = service.inject_researched_candidate(
            symbol="AMD",
            thesis="exit position",
            confidence=0.9,
            trigger_reason="regime smoke",
            direction="sell",
        )
        assert sell_res["queued"] is True, sell_res

        # Data-only symbols are never tradeable, regardless of regime.
        spy_res = service_riskon.inject_researched_candidate(
            symbol="SPY",
            thesis="never trade the regime inputs",
            confidence=0.9,
            trigger_reason="regime smoke",
            direction="buy",
        )
        assert spy_res["queued"] is False
        assert "data-only" in spy_res["error"]

    # --- Scheduler cycle: buys suppressed when regime disallows new longs ---
    runtime = _load_module("regime_smoke_runtime", RUNTIME_MODULE)
    with tempfile.TemporaryDirectory(prefix="regime-cycle-smoke-") as tmp:
        cycle = runtime.run_watcher_cycle(
            session_id="regime-smoke",
            repo_root=tmp,
            fetcher=lambda: [
                {"symbol": "NVDA", "side": "buy", "confidence": 0.9,
                 "strategy": "smoke", "source": "smoke", "dry_run": True},
                {"symbol": "AMD", "side": "sell", "confidence": 0.9,
                 "strategy": "smoke", "source": "smoke", "dry_run": True},
            ],
            regime_provider=lambda: _classify(regime, vix=26.0),
        )
        assert cycle["regime_suppressed_count"] == 1
        assert cycle["candidate_count"] == 1  # only the sell survived
        assert cycle["regime"]["regime"] == "risk_off"
        saved = json.loads(
            Path(cycle["runtime_state_path"]).read_text(encoding="utf-8")
        )
        assert saved["positions_summary"]["regime"]["regime"] == "risk_off"

    print("regime smoke: ok")


if __name__ == "__main__":
    main()
