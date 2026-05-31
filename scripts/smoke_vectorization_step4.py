#!/usr/bin/env python3
"""Smoke checks for mechanical vectorization refactors."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
VIBE_AGENT_ROOT = REPO_ROOT / "vibe-trading" / "agent"
VIBE_ROOT = REPO_ROOT / "vibe-trading"
if str(VIBE_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(VIBE_AGENT_ROOT))
if str(VIBE_ROOT) not in sys.path:
    sys.path.insert(0, str(VIBE_ROOT))

from src.tools.factor_analysis_tool import _compute_ic_series  # noqa: E402
from backtest.validation import _load_trades  # noqa: E402


def _legacy_compute_ic_series(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
) -> pd.Series:
    common_dates = factor_df.index.intersection(return_df.index)
    common_codes = factor_df.columns.intersection(return_df.columns)
    if len(common_dates) == 0 or len(common_codes) == 0:
        return pd.Series(dtype=float)

    factor_df = factor_df.loc[common_dates, common_codes]
    return_df = return_df.loc[common_dates, common_codes]

    ic_values: dict[object, float] = {}
    for date in common_dates:
        factor_row = factor_df.loc[date].dropna()
        return_row = return_df.loc[date].dropna()
        shared = factor_row.index.intersection(return_row.index)
        if len(shared) < 5:
            continue
        corr = factor_row[shared].corr(return_row[shared], method="spearman")
        if pd.notna(corr):
            ic_values[date] = float(corr)
    return pd.Series(ic_values, dtype=float)


def _legacy_load_trades(run_dir: Path):
    from backtest.models import TradeRecord

    path = run_dir / "artifacts" / "trades.csv"
    df = pd.read_csv(path)
    if df.empty:
        return []
    trades = []
    exit_rows = df[df["pnl"] != 0].reset_index(drop=True)
    for _, row in exit_rows.iterrows():
        trades.append(TradeRecord(
            symbol=str(row.get("code", "")),
            direction=1 if row.get("side") == "sell" else -1,
            entry_price=0.0,
            exit_price=float(row.get("price", 0)),
            entry_time=pd.Timestamp(row.get("timestamp", "2000-01-01")),
            exit_time=pd.Timestamp(row.get("timestamp", "2000-01-01")),
            size=float(row.get("qty", 0)),
            leverage=1.0,
            pnl=float(row.get("pnl", 0)),
            pnl_pct=float(row.get("return_pct", 0)),
            exit_reason=str(row.get("reason", "signal")),
            holding_bars=int(row.get("holding_days", 0)),
            commission=0.0,
        ))
    return trades


def _assert_factor_equivalence() -> None:
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
    symbols = ["A", "B", "C", "D", "E", "F"]
    factor_df = pd.DataFrame(
        [
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            [2.0, 2.0, 4.0, 4.0, 6.0, 8.0],
            [1.0, None, 2.0, None, 3.0, None],
        ],
        index=dates,
        columns=symbols,
    )
    return_df = pd.DataFrame(
        [
            [0.01, 0.03, 0.02, 0.05, 0.04, 0.06],
            [0.02, 0.01, 0.03, 0.06, 0.05, 0.04],
            [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],
        ],
        index=dates,
        columns=symbols,
    )

    legacy = _legacy_compute_ic_series(factor_df, return_df)
    current = _compute_ic_series(factor_df, return_df)
    pd.testing.assert_series_equal(
        current.sort_index(),
        legacy.sort_index(),
        check_names=False,
        check_dtype=False,
        atol=1e-12,
        rtol=1e-12,
    )


def _assert_validation_equivalence() -> None:
    with tempfile.TemporaryDirectory(prefix="vector-smoke-") as tmp:
        run_dir = Path(tmp)
        artifact_dir = run_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        trades_path = artifact_dir / "trades.csv"
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-01-01",
                    "code": "AAPL",
                    "side": "buy",
                    "qty": 10,
                    "price": 100,
                    "pnl": 0,
                    "return_pct": 0,
                    "reason": "entry",
                    "holding_days": 0,
                },
                {
                    "timestamp": "2026-01-03",
                    "code": "AAPL",
                    "side": "sell",
                    "qty": 10,
                    "price": 103,
                    "pnl": 30,
                    "return_pct": 0.03,
                    "reason": "signal",
                    "holding_days": 2,
                },
                {
                    "timestamp": "2026-01-04",
                    "code": "MSFT",
                    "side": "buy",
                    "qty": 5,
                    "price": 80,
                    "pnl": -5,
                    "return_pct": -0.01,
                    "reason": "stop",
                    "holding_days": 1,
                },
            ]
        ).to_csv(trades_path, index=False)

        legacy = _legacy_load_trades(run_dir)
        current = _load_trades(run_dir)

        assert len(current) == len(legacy)
        for current_trade, legacy_trade in zip(current, legacy):
            assert current_trade == legacy_trade


def main() -> None:
    _assert_factor_equivalence()
    _assert_validation_equivalence()
    print("vectorization step4 smoke: ok")


if __name__ == "__main__":
    main()
