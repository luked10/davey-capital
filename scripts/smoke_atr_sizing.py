#!/usr/bin/env python3
"""Offline smoke for ATR-based position sizing (deterministic, no network)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
ATR_MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "risk" / "atr_sizing.py"
SONNET_MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "proposal" / "sonnet_client.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    atr = _load_module("atr_sizing_smoke", ATR_MODULE_PATH)

    # --- Bucket classification (classify first, then size) ---
    assert atr.classify_atr_bucket(1.0).name == "calm_large_cap"
    assert atr.classify_atr_bucket(1.0).stop_mult == 1.75
    assert atr.classify_atr_bucket(2.4999).stop_mult == 1.75
    assert atr.classify_atr_bucket(2.5).name == "normal_tech"
    assert atr.classify_atr_bucket(3.0).stop_mult == 2.0
    assert atr.classify_atr_bucket(4.0).stop_mult == 2.25
    assert atr.classify_atr_bucket(5.0).name == "hot_tech"
    assert atr.classify_atr_bucket(5.0).stop_mult == 2.5
    assert atr.classify_atr_bucket(7.0).stop_mult == 2.5
    extreme = atr.classify_atr_bucket(8.0)
    assert extreme.name == "extreme"
    assert extreme.stop_mult == 3.0
    assert extreme.skip_recommended is True
    for bad in (0.0, -1.0, float("nan")):
        try:
            atr.classify_atr_bucket(bad)
            raise AssertionError(f"classify_atr_bucket accepted {bad!r}")
        except ValueError:
            pass

    # --- Sizing: notional-cap-bound case ---
    # entry=100, atr=3 (ATR% 3 -> normal_tech stop_mult 2.0), budget=1.0:
    # risk_per_share=6.0, shares_by_risk=0.1667, shares_by_cap=10/100=0.1
    sized = atr.size_position(symbol="nvda", entry_price=100.0, atr=3.0, risk_budget=1.0)
    assert sized.symbol == "NVDA"
    assert sized.bucket == "normal_tech"
    assert sized.stop_mult == 2.0
    assert sized.risk_per_share == 6.0
    assert abs(sized.shares_by_risk - 1.0 / 6.0) < 1e-6
    assert sized.shares_by_cap == 0.1
    assert sized.final_shares == 0.1
    assert sized.final_notional == 10.0
    assert sized.initial_stop == 94.0
    assert sized.skip_recommended is False

    # --- Sizing: risk-budget-bound case ---
    # entry=5, atr=0.2 (ATR% 4 -> stop_mult 2.25), budget=0.5:
    # risk_per_share=0.45, shares_by_risk=1.1111, shares_by_cap=2.0
    sized2 = atr.size_position(symbol="SOFI", entry_price=5.0, atr=0.2, risk_budget=0.5)
    assert sized2.stop_mult == 2.25
    assert abs(sized2.risk_per_share - 0.45) < 1e-9
    assert abs(sized2.final_shares - 0.5 / 0.45) < 1e-6
    assert sized2.final_shares < sized2.shares_by_cap
    assert abs(sized2.final_notional - sized2.final_shares * 5.0) < 1e-4

    # Caller stop_mult is clamped to the [1.75, 3.0] doctrine bounds.
    clamped = atr.size_position(
        symbol="NVDA", entry_price=100.0, atr=3.0, risk_budget=1.0, stop_mult=99.0
    )
    assert clamped.stop_mult == 3.0
    clamped_low = atr.size_position(
        symbol="NVDA", entry_price=100.0, atr=3.0, risk_budget=1.0, stop_mult=0.1
    )
    assert clamped_low.stop_mult == 1.75

    # Invalid inputs must raise, never return a size.
    for kwargs in (
        {"entry_price": 0.0, "atr": 1.0, "risk_budget": 1.0},
        {"entry_price": 100.0, "atr": -1.0, "risk_budget": 1.0},
        {"entry_price": 100.0, "atr": 1.0, "risk_budget": 0.0},
    ):
        try:
            atr.size_position(symbol="NVDA", **kwargs)
            raise AssertionError(f"size_position accepted {kwargs!r}")
        except ValueError:
            pass

    # --- compute_atr14 over fixture bars (pure, offline) ---
    closes = [100.0 + i * 0.5 for i in range(20)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    atr14 = atr.compute_atr14(highs, lows, closes)
    # Each true range: max(high-low=2.0, |high-prev_close|=1.5, |low-prev_close|=1.5) = 2.0
    assert abs(atr14 - 2.0) < 1e-9
    try:
        atr.compute_atr14(highs[:5], lows[:5], closes[:5])
        raise AssertionError("compute_atr14 accepted too few bars")
    except ValueError:
        pass

    # --- Intent blocks assembly ---
    blocks = atr.build_intent_risk_blocks(sized, vix_ok=True, spy_trend_ok=False)
    assert blocks["entry"] == {"style": "market", "max_notional": 10.0}
    assert blocks["risk"]["atr14"] == 3.0
    assert blocks["risk"]["stop_mult"] == 2.0
    assert blocks["risk"]["risk_per_share"] == 6.0
    assert blocks["risk"]["cooldown_days"] == 2
    assert blocks["exit_plan"]["initial_stop"] == 94.0
    assert blocks["exit_plan"]["tp1_r"] == 1.5
    assert blocks["exit_plan"]["trail_mult"] == 2.5
    assert blocks["exit_plan"]["max_holding_days"] == 5
    assert blocks["regime_gate"] == {"vix_ok": True, "spy_trend_ok": False}

    # --- Sonnet wiring: prompt schema + Python-side arithmetic ---
    sonnet = _load_module("atr_sizing_sonnet_smoke", SONNET_MODULE_PATH)
    prompt = sonnet._system_prompt_for_runtime()
    assert "stop_mult" in prompt, "Sonnet prompt schema must include stop_mult"
    assert "risk_budget" in prompt, "Sonnet prompt schema must include risk_budget"

    class FakeClient(sonnet.SonnetProposalClient):
        def __init__(self, raw: dict[str, Any]) -> None:
            self.raw = raw

        def complete(self, *, static_prefix: str, candidate_suffix: str):
            return sonnet.SonnetCompletionResponse(
                text=json.dumps(self.raw),
                model="fake-sonnet",
                provider="fake",
                cache_read_tokens=0,
                cache_write_tokens=0,
                input_tokens=0,
                output_tokens=0,
            )

    def model_payload(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "intent_id": "intent-atr-smoke",
            "signal_id": "sig-atr-smoke",
            "broker": "alpaca",
            "symbol": "NVDA",
            "side": "buy",
            # Deliberately-wrong model arithmetic: Python must overwrite this.
            "quantity": 999.0,
            "created_at": "2026-07-01T00:00:00Z",
            "order_type": "market",
            "limit_price": None,
            "estimated_notional": 999.0,
            "time_in_force": "day",
            "dry_run": True,
            "approved": False,
            "approved_by": "",
            "approved_at": "",
            "status": "pending",
            "metadata": {"rationale": "atr sizing smoke"},
            "risk": {"stop_mult": 2.0, "risk_budget": 1.0},
            "entry": None,
            "exit_plan": None,
            "regime_gate": None,
        }
        payload.update(overrides)
        return payload

    previous_live_mode = os.environ.pop("DAVEY_LIVE_MODE", None)
    try:
        candidate = {
            "symbol": "NVDA",
            "side": "buy",
            "confidence": 0.85,
            "metadata": {"atr14": 3.0, "latest_price": 100.0},
        }
        result = FakeClient(model_payload()).propose(candidate)
        assert result.intent is not None, result.error
        # Python arithmetic, not the model's 999: cap-bound at $10 / $100.
        assert result.intent.quantity == 0.1
        assert result.intent.estimated_notional == 10.0
        assert result.intent.entry == {"style": "market", "max_notional": 10.0}
        assert result.intent.risk["atr14"] == 3.0
        assert result.intent.risk["stop_mult"] == 2.0
        assert result.intent.risk["risk_per_share"] == 6.0
        assert result.intent.exit_plan["initial_stop"] == 94.0
        assert result.intent.regime_gate == {"vix_ok": True, "spy_trend_ok": True}
        assert result.allowed is True

        # Oversized model risk_budget is clamped to $2 before sizing.
        greedy = FakeClient(
            model_payload(risk={"stop_mult": 2.0, "risk_budget": 500.0})
        ).propose(candidate)
        assert greedy.intent is not None, greedy.error
        assert greedy.intent.risk["risk_per_share"] == 6.0
        assert greedy.intent.estimated_notional <= 10.0

        # Extreme ATR bucket fails closed to needs_human.
        hot_candidate = {
            "symbol": "IONQ",
            "side": "buy",
            "confidence": 0.9,
            "metadata": {"atr14": 8.0, "latest_price": 100.0},
        }
        hot = FakeClient(model_payload(symbol="IONQ")).propose(hot_candidate)
        assert hot.intent is None
        assert hot.needs_human is True
        assert "recommends skipping" in hot.error

        # No sizing inputs: legacy path unchanged, model risk block dropped.
        legacy = FakeClient(model_payload()).propose({"symbol": "NVDA"})
        assert legacy.intent is not None, legacy.error
        assert legacy.intent.quantity == 999.0
        assert legacy.intent.risk is None
    finally:
        if previous_live_mode is not None:
            os.environ["DAVEY_LIVE_MODE"] = previous_live_mode

    print("atr sizing smoke: ok")


if __name__ == "__main__":
    main()
