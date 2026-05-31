#!/usr/bin/env python3
"""Parity smoke for the ``_load_trades`` column/default refactor.

This script protects a mechanical refactor of
``backtest.validation._load_trades``. It embeds a *legacy reference*
implementation that faithfully reproduces the PRE-refactor, column-based
behaviour of ``_load_trades`` on this branch, then proves record-by-record,
field-by-field, NaN/NaT-aware parity between that reference and the live
``_load_trades`` across a battery of deterministic in-memory fixtures.

No network, no credentials, no live trading. The only IO is writing
deterministic CSV fixtures into a per-run temporary directory.
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, List

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
VIBE_ROOT = REPO_ROOT / "vibe-trading"
if str(VIBE_ROOT) not in sys.path:
    sys.path.insert(0, str(VIBE_ROOT))

from backtest.models import TradeRecord  # noqa: E402
from backtest.validation import _load_trades  # noqa: E402


# ─── Legacy reference (faithful copy of pre-refactor _load_trades) ───
#
# This mirrors the column-based implementation exactly: per-column extraction
# with column-missing defaults, a per-row NaN->"2000-01-01" timestamp guard
# applied BEFORE pd.Timestamp(), and per-element float()/int() coercions.
# It is intentionally NOT the row.get(...) variant used elsewhere, because
# row.get() defaults only on a missing key and would mishandle NaN timestamps.

def _legacy_load_trades(run_dir: Path) -> List[TradeRecord]:
    path = run_dir / "artifacts" / "trades.csv"
    df = pd.read_csv(path)
    if df.empty:
        return []

    exit_rows = df[df["pnl"] != 0].reset_index(drop=True)
    if exit_rows.empty:
        return []

    row_count = len(exit_rows)
    symbols = (
        exit_rows["code"].tolist()
        if "code" in exit_rows.columns
        else [""] * row_count
    )
    sides = (
        exit_rows["side"].tolist()
        if "side" in exit_rows.columns
        else [""] * row_count
    )
    prices = (
        exit_rows["price"].tolist()
        if "price" in exit_rows.columns
        else [0] * row_count
    )
    timestamps = (
        exit_rows["timestamp"].tolist()
        if "timestamp" in exit_rows.columns
        else ["2000-01-01"] * row_count
    )
    quantities = (
        exit_rows["qty"].tolist()
        if "qty" in exit_rows.columns
        else [0] * row_count
    )
    pnls = (
        exit_rows["pnl"].tolist()
        if "pnl" in exit_rows.columns
        else [0] * row_count
    )
    pnl_pcts = (
        exit_rows["return_pct"].tolist()
        if "return_pct" in exit_rows.columns
        else [0] * row_count
    )
    reasons = (
        exit_rows["reason"].tolist()
        if "reason" in exit_rows.columns
        else ["signal"] * row_count
    )
    holding_days = (
        exit_rows["holding_days"].tolist()
        if "holding_days" in exit_rows.columns
        else [0] * row_count
    )

    trades: List[TradeRecord] = []
    for index in range(row_count):
        timestamp = timestamps[index]
        timestamp = "2000-01-01" if pd.isna(timestamp) else timestamp
        trades.append(TradeRecord(
            symbol=str(symbols[index]),
            direction=1 if sides[index] == "sell" else -1,
            entry_price=0.0,
            exit_price=float(prices[index]),
            entry_time=pd.Timestamp(timestamp),
            exit_time=pd.Timestamp(timestamp),
            size=float(quantities[index]),
            leverage=1.0,
            pnl=float(pnls[index]),
            pnl_pct=float(pnl_pcts[index]),
            exit_reason=str(reasons[index]),
            holding_bars=int(holding_days[index]),
            commission=0.0,
        ))
    return trades


# ─── NaN/NaT-aware comparison helpers ───

_FIELDS = [f.name for f in dataclasses.fields(TradeRecord)]


def _is_nanlike(value: Any) -> bool:
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, bool) else False


def _values_equal(a: Any, b: Any) -> bool:
    a_na = _is_nanlike(a)
    b_na = _is_nanlike(b)
    if a_na or b_na:
        return a_na and b_na
    if type(a) is not type(b):
        return False
    return bool(a == b)


def _records_equal(a: TradeRecord, b: TradeRecord) -> List[str]:
    """Return list of field names that differ (empty == identical)."""
    diffs: List[str] = []
    for field in _FIELDS:
        if not _values_equal(getattr(a, field), getattr(b, field)):
            diffs.append(field)
    return diffs


# ─── Fixture infrastructure ───

def _write_trades_csv(run_dir: Path, rows: List[dict], columns: List[str] | None) -> None:
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    if columns is not None:
        df = df[columns]
    df.to_csv(artifact_dir / "trades.csv", index=False)


def _run_case(name: str, rows: List[dict], columns: List[str] | None) -> bool:
    """Run one fixture; return True on PASS, False on FAIL. Prints details."""
    with tempfile.TemporaryDirectory(prefix="trade-loader-smoke-") as tmp:
        run_dir = Path(tmp)
        _write_trades_csv(run_dir, rows, columns)

        legacy_error: BaseException | None = None
        current_error: BaseException | None = None
        legacy_trades: List[TradeRecord] = []
        current_trades: List[TradeRecord] = []

        try:
            legacy_trades = _legacy_load_trades(run_dir)
        except BaseException as exc:  # noqa: BLE001 - parity must include raising
            legacy_error = exc
        try:
            current_trades = _load_trades(run_dir)
        except BaseException as exc:  # noqa: BLE001 - parity must include raising
            current_error = exc

        # Parity on the raising path.
        if legacy_error is not None or current_error is not None:
            if legacy_error is None or current_error is None:
                print(f"  [FAIL] {name}: only one impl raised "
                      f"(legacy={legacy_error!r}, current={current_error!r})")
                return False
            if type(legacy_error) is not type(current_error):
                print(f"  [FAIL] {name}: exception type mismatch "
                      f"(legacy={type(legacy_error).__name__}, "
                      f"current={type(current_error).__name__})")
                return False
            print(f"  [PASS] {name}: both raised {type(legacy_error).__name__}")
            return True

        if len(legacy_trades) != len(current_trades):
            print(f"  [FAIL] {name}: length mismatch "
                  f"(legacy={len(legacy_trades)}, current={len(current_trades)})")
            return False

        for idx, (lt, ct) in enumerate(zip(legacy_trades, current_trades)):
            diffs = _records_equal(lt, ct)
            if diffs:
                for field in diffs:
                    print(f"  [FAIL] {name}: row {idx} field '{field}' "
                          f"legacy={getattr(lt, field)!r} ({type(getattr(lt, field)).__name__}) "
                          f"current={getattr(ct, field)!r} ({type(getattr(ct, field)).__name__})")
                return False

        print(f"  [PASS] {name}: {len(current_trades)} record(s) identical")
        return True


# ─── Deterministic fixtures (cases A–G) ───

def _cases() -> List[Callable[[], bool]]:
    full_cols = [
        "timestamp", "code", "side", "qty", "price",
        "pnl", "return_pct", "reason", "holding_days",
    ]

    def case_a_normal() -> bool:
        rows = [
            {"timestamp": "2026-01-01", "code": "AAPL", "side": "buy", "qty": 10,
             "price": 100, "pnl": 0, "return_pct": 0, "reason": "entry",
             "holding_days": 0},
            {"timestamp": "2026-01-03", "code": "AAPL", "side": "sell", "qty": 10,
             "price": 103, "pnl": 30, "return_pct": 0.03, "reason": "signal",
             "holding_days": 2},
            {"timestamp": "2026-01-04", "code": "MSFT", "side": "buy", "qty": 5,
             "price": 80, "pnl": -5, "return_pct": -0.01, "reason": "stop",
             "holding_days": 1},
            {"timestamp": "2026-01-09", "code": "MSFT", "side": "sell", "qty": 5,
             "price": 85, "pnl": 25, "return_pct": 0.0625, "reason": "signal",
             "holding_days": 5},
        ]
        return _run_case("A normal mixed entry/exit", rows, full_cols)

    def case_b_missing_optional() -> bool:
        # Only pnl (required by the exit filter) + a couple of columns present.
        rows = [
            {"price": 100, "pnl": 0},
            {"price": 110, "pnl": 50},
            {"price": 90, "pnl": -10},
        ]
        return _run_case("B missing optional columns", rows, ["price", "pnl"])

    def case_c_nan_blank_timestamps() -> bool:
        # Blank cells become NaN on read -> baseline substitutes 2000-01-01.
        rows = [
            {"timestamp": "2026-02-01", "code": "AAPL", "side": "sell", "qty": 10,
             "price": 100, "pnl": 12, "return_pct": 0.01, "reason": "signal",
             "holding_days": 3},
            {"timestamp": "", "code": "MSFT", "side": "sell", "qty": 4,
             "price": 70, "pnl": 8, "return_pct": 0.02, "reason": "signal",
             "holding_days": 1},
            {"timestamp": None, "code": "TSLA", "side": "buy", "qty": 2,
             "price": 200, "pnl": -4, "return_pct": -0.01, "reason": "stop",
             "holding_days": 2},
        ]
        return _run_case("C NaN/blank timestamps", rows, full_cols)

    def case_d_invalid_timestamps() -> bool:
        # Non-parseable timestamp -> baseline raises inside pd.Timestamp().
        rows = [
            {"timestamp": "not-a-date", "code": "AAPL", "side": "sell", "qty": 1,
             "price": 100, "pnl": 5, "return_pct": 0.01, "reason": "signal",
             "holding_days": 1},
        ]
        return _run_case("D invalid timestamps (expect parity raise)", rows, full_cols)

    def case_e_numeric_strings() -> bool:
        # Quoted numeric values exercise the float()/int() coercion path.
        rows = [
            {"timestamp": "2026-03-01", "code": "AAPL", "side": "sell", "qty": "10",
             "price": "103.5", "pnl": "30.25", "return_pct": "0.03", "reason": "signal",
             "holding_days": "2"},
            {"timestamp": "2026-03-02", "code": "MSFT", "side": "buy", "qty": "5",
             "price": "80.0", "pnl": "-5.5", "return_pct": "-0.01", "reason": "stop",
             "holding_days": "1"},
        ]
        return _run_case("E numeric strings", rows, full_cols)

    def case_f_sparse_rows() -> bool:
        # Sparse/dirty rows: missing optional cells (NaN) across several columns.
        rows = [
            {"timestamp": "2026-04-01", "code": None, "side": None, "qty": None,
             "price": None, "pnl": 7, "return_pct": None, "reason": None,
             "holding_days": 0},
            {"timestamp": "2026-04-02", "code": "AAPL", "side": "sell", "qty": 3,
             "price": 50, "pnl": -3, "return_pct": -0.02, "reason": "signal",
             "holding_days": 4},
        ]
        return _run_case("F sparse/dirty rows", rows, full_cols)

    def case_g_pnl_zero_filtered() -> bool:
        # Every row has pnl == 0 -> all filtered out -> empty result.
        rows = [
            {"timestamp": "2026-05-01", "code": "AAPL", "side": "buy", "qty": 10,
             "price": 100, "pnl": 0, "return_pct": 0, "reason": "entry",
             "holding_days": 0},
            {"timestamp": "2026-05-02", "code": "AAPL", "side": "sell", "qty": 10,
             "price": 100, "pnl": 0, "return_pct": 0, "reason": "signal",
             "holding_days": 1},
        ]
        return _run_case("G pnl==0 rows filtered", rows, full_cols)

    return [
        case_a_normal,
        case_b_missing_optional,
        case_c_nan_blank_timestamps,
        case_d_invalid_timestamps,
        case_e_numeric_strings,
        case_f_sparse_rows,
        case_g_pnl_zero_filtered,
    ]


def main() -> int:
    print("validation _load_trades parity smoke")
    results = [case() for case in _cases()]
    passed = sum(1 for r in results if r)
    total = len(results)
    if passed == total:
        print(f"PASS: {passed}/{total} cases identical (legacy reference == _load_trades)")
        return 0
    print(f"FAIL: {passed}/{total} cases passed; {total - passed} mismatched")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
