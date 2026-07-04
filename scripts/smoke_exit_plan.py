#!/usr/bin/env python3
"""Offline smoke for the exit plan state machine + position monitor."""

from __future__ import annotations

from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
EXIT_PLAN_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "runtime" / "exit_plan.py"
RUNTIME_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "runtime_scaffold.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_event(self, *, event_type: str, symbol: str, payload: dict[str, Any],
                     side: str | None = None, **kwargs: Any) -> dict[str, Any]:
        row = {"event_type": event_type, "symbol": symbol, "side": side, "payload": payload}
        self.events.append(row)
        return row

    def types_for(self, symbol: str) -> list[str]:
        return [e["event_type"] for e in self.events if e["symbol"] == symbol]


def main() -> None:
    ep = _load_module("exit_plan_smoke", EXIT_PLAN_MODULE)

    # --- Full happy-path state machine: ARMED -> TP1_HIT -> TRAILING -> EXITED ---
    # entry=100, atr=2, volatile_tech preset: stop_mult=2.25, tp1_r=1.5, trail=2.5
    plan = ep.exit_plan_from_preset(
        symbol="nvda", entry_price=100.0, atr=2.0,
        preset="volatile_tech", entry_date="2026-07-01",
    )
    assert plan.symbol == "NVDA"
    assert plan.state is ep.ExitState.ARMED
    assert abs(plan.risk_per_share - 4.5) < 1e-9
    assert abs(plan.initial_stop - 95.5) < 1e-9
    assert abs(plan.tp1_price - 106.75) < 1e-9

    as_of = date(2026, 7, 2)
    assert plan.update(101.0, as_of=as_of) == "hold"
    assert plan.state is ep.ExitState.ARMED
    assert plan.peak_price == 101.0

    assert plan.update(107.0, as_of=as_of) == "take_profit_1"
    assert plan.state is ep.ExitState.TP1_HIT

    # Next update transitions TP1_HIT -> TRAILING (remainder trails).
    assert plan.update(108.0, as_of=as_of) == "hold"
    assert plan.state is ep.ExitState.TRAILING
    assert plan.peak_price == 108.0
    # trailing_stop = peak 108 - 2.5*2 = 103
    assert abs(plan.trailing_stop - 103.0) < 1e-9

    assert plan.update(102.5, as_of=as_of) == "exit_trailing"
    assert plan.state is ep.ExitState.EXITED
    # After exit, updates are inert.
    assert plan.update(50.0, as_of=as_of) == "hold"
    assert plan.state is ep.ExitState.EXITED

    # --- Initial ATR stop fires exit_stop from ARMED ---
    stop_plan = ep.exit_plan_from_preset(
        symbol="AMD", entry_price=100.0, atr=2.0,
        preset="stable_mega_cap", entry_date="2026-07-01",
    )
    # stop_mult=1.75 -> initial_stop = 96.5
    assert stop_plan.update(96.4, as_of=as_of) == "exit_stop"
    assert stop_plan.state is ep.ExitState.EXITED

    # --- Hard -7% stop bypasses everything, in any state ---
    hard_plan = ep.exit_plan_from_preset(
        symbol="TSLA", entry_price=100.0, atr=10.0,  # wide ATR stop (72.5)
        preset="high_vol_event", entry_date="2026-07-01",
    )
    # -7.5% unrealized: above the ATR stop but below the -7% hard stop.
    assert hard_plan.update(92.5, as_of=as_of) == "exit_stop"
    assert hard_plan.state is ep.ExitState.EXITED
    assert "hard stop" in hard_plan.last_reason

    # --- +20% windfall injects a take-profit (approval path, not auto-exit) ---
    windfall = ep.exit_plan_from_preset(
        symbol="MU", entry_price=100.0, atr=10.0, preset="high_vol_event",
        entry_date="2026-07-01",
    )
    # +21% is below tp1 (entry + 2.0*27.5 = 155) but above the +20% windfall.
    assert windfall.update(121.0, as_of=as_of) == "take_profit_1"
    assert windfall.state is ep.ExitState.TP1_HIT

    # --- Time stop from ARMED goes to the approval path ---
    stale = ep.exit_plan_from_preset(
        symbol="META", entry_price=100.0, atr=2.0, preset="high_vol_event",
        entry_date="2026-07-01",
    )
    assert stale.update(100.5, as_of=date(2026, 7, 10)) == "exit_trailing"
    assert stale.state is ep.ExitState.TRAILING

    # --- Serialization round-trip ---
    payload = plan.to_dict()
    restored = ep.ExitPlan.from_dict(json.loads(json.dumps(payload)))
    assert restored.state is ep.ExitState.EXITED
    assert restored.peak_price == plan.peak_price
    assert restored.entry_price == plan.entry_price

    # --- Presets match doctrine ---
    presets = ep.EXIT_THRESHOLD_PRESETS
    assert presets["stable_mega_cap"].stop_mult == 1.75
    assert presets["stable_mega_cap"].tp1_r == 1.25
    assert presets["stable_mega_cap"].max_holding_days == 7
    assert presets["volatile_tech"].stop_mult == 2.25
    assert presets["volatile_tech"].max_holding_days == 5
    assert presets["high_vol_event"].stop_mult == 2.75
    assert presets["high_vol_event"].trail_mult == 3.0
    assert presets["high_vol_event"].max_holding_days == 3

    # --- Cooldown helpers ---
    # Friday + 2 trading days = Tuesday.
    assert ep.add_trading_days(date(2026, 7, 3), 2) == date(2026, 7, 7)
    until = ep.cooldown_until(reason="stop_out", start=date(2026, 7, 3))
    assert until == "2026-07-07"
    regime_until = ep.cooldown_until(reason="regime_suppressed", start=date(2026, 7, 3))
    assert regime_until == "2026-07-04"
    assert ep.is_cooldown_active("2026-07-07", as_of=date(2026, 7, 6)) is True
    assert ep.is_cooldown_active("2026-07-07", as_of=date(2026, 7, 7)) is False

    # --- Position monitor end-to-end (offline, injected prices/journal) ---
    runtime = _load_module("exit_plan_runtime_smoke", RUNTIME_MODULE)

    def position_entry(symbol: str, entry_price: float, atr: float) -> dict[str, Any]:
        monitor_plan = ep.exit_plan_from_preset(
            symbol=symbol, entry_price=entry_price, atr=atr,
            preset="volatile_tech", entry_date="2026-07-01",
        )
        return {
            "symbol": symbol,
            "entry_price": entry_price,
            "entry_date": "2026-07-01",
            "atr": atr,
            "exit_plan": monitor_plan.to_dict(),
            "original_thesis": f"{symbol} smoke thesis",
            "original_handoff_id": f"handoff-{symbol.lower()}",
        }

    prices = {
        "HARD": 90.0,   # -10% -> hard stop, immediate exit, no approval
        "TPWIN": 130.0,  # +30% -> take_profit_1, injected for approval
        "HOLD": 101.0,  # hold, peak updated
    }

    previous_root = os.environ.get("DAVEY_ROOT")
    with tempfile.TemporaryDirectory(prefix="exit-plan-monitor-smoke-") as tmp:
        os.environ["DAVEY_ROOT"] = tmp
        try:
            state = runtime._load_runtime_state_module().default_runtime_state()
            state.positions_summary = {
                "open_positions": 3,
                "positions": {
                    "HARD": position_entry("HARD", 100.0, 2.0),
                    "TPWIN": position_entry("TPWIN", 100.0, 2.0),
                    "HOLD": position_entry("HOLD", 100.0, 2.0),
                },
                "cooldowns": {},
            }
            runtime.write_runtime_state(state, repo_root=tmp)

            journal = FakeJournal()
            result = runtime.run_position_monitor(
                repo_root=tmp,
                session_id="exit-plan-smoke",
                price_fetcher=lambda symbol: prices.get(symbol),
                journal=journal,
                as_of=date(2026, 7, 2),
            )
            assert result["status"] == "ok"
            assert result["checked"] == 3
            by_symbol = {a["symbol"]: a for a in result["actions"]}

            # Hard stop: immediate exit, no approval required, cooldown applied.
            assert by_symbol["HARD"]["action"] == "exit_stop"
            assert by_symbol["HARD"]["unrealized_pnl_pct"] < -7.0
            assert "stop_hit" in journal.types_for("HARD")
            assert "closed" in journal.types_for("HARD")

            # Take profit: injected as a sell handoff for Poke approval.
            assert by_symbol["TPWIN"]["action"] == "take_profit_1"
            assert by_symbol["TPWIN"]["unrealized_pnl_pct"] > 20.0
            assert by_symbol["TPWIN"]["handoff"]["status"] == "ok"
            assert "take_profit" in journal.types_for("TPWIN")

            assert by_symbol["HOLD"]["action"] == "hold"

            saved = json.loads(
                (Path(tmp) / "runtime_state.json").read_text(encoding="utf-8")
            )
            summary = saved["positions_summary"]
            # Hard-stopped position removed; the other two remain.
            assert "HARD" not in summary["positions"]
            assert set(summary["positions"]) == {"TPWIN", "HOLD"}
            assert summary["open_positions"] == 2
            # Stop-out cooldown = 2 trading days from 2026-07-02 (Thu) -> Mon 07-06.
            assert summary["cooldowns"]["HARD"]["reason"] == "stop_out"
            # Peak price persisted for the held position.
            assert summary["positions"]["HOLD"]["exit_plan"]["peak_price"] == 101.0
            # TP position advanced to tp1_hit and awaits approval.
            assert summary["positions"]["TPWIN"]["exit_plan"]["state"] == "tp1_hit"

            # The injected sell handoff landed in the poke queue (approval path).
            queue_files = list(Path(tmp).glob("logs/overnight/*/poke_bridge_queue.jsonl"))
            assert queue_files, "expected an injected sell handoff in the poke queue"
            queued = [
                json.loads(line)
                for path in queue_files
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            sells = [q for q in queued if q["metadata"].get("side") == "sell"]
            assert len(sells) == 1
            assert sells[0]["metadata"]["symbol"] == "TPWIN"
            assert sells[0]["dry_run"] is True

            # Second run: EXITED/pending positions do not refire.
            journal2 = FakeJournal()
            result2 = runtime.run_position_monitor(
                repo_root=tmp,
                session_id="exit-plan-smoke",
                price_fetcher=lambda symbol: prices.get(symbol),
                journal=journal2,
                as_of=date(2026, 7, 2),
            )
            by_symbol2 = {a["symbol"]: a for a in result2["actions"]}
            # TPWIN advanced TP1_HIT -> TRAILING as a hold, no duplicate injection.
            assert by_symbol2["TPWIN"]["action"] == "hold"
            assert "take_profit" not in journal2.types_for("TPWIN")
        finally:
            if previous_root is None:
                os.environ.pop("DAVEY_ROOT", None)
            else:
                os.environ["DAVEY_ROOT"] = previous_root

    print("exit plan smoke: ok")


if __name__ == "__main__":
    main()
