#!/usr/bin/env python3
"""Deterministic parity smoke for the backtest trade loader timestamp handling.

Reproduces the wiped semantic regression in
``vibe-trading/backtest/validation._load_trades``:

  - Legacy dirty-timestamp semantics: when the timestamp COLUMN is present,
    NaN / invalid values coerce to NaT (NOT a synthetic "2000-01-01").
  - The "2000-01-01" sentinel is ONLY the missing-COLUMN default.

This covers NaN, invalid-string, sparse, and normal timestamp rows, plus the
missing-column case.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
VIBE_ROOT = REPO_ROOT / "vibe-trading"
if str(VIBE_ROOT) not in sys.path:
    sys.path.insert(0, str(VIBE_ROOT))

from backtest.validation import _load_trades  # noqa: E402


def _write_trades(run_dir: Path, rows: list[dict]) -> None:
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(artifact_dir / "trades.csv", index=False)


def _exit_row(timestamp, code: str, pnl: float) -> dict:
    # pnl != 0 so the loader treats it as an exit row.
    return {
        "timestamp": timestamp,
        "code": code,
        "side": "sell",
        "qty": 10,
        "price": 100.0,
        "pnl": pnl,
        "return_pct": 0.01,
        "reason": "signal",
        "holding_days": 1,
    }


def _assert_dirty_timestamps_become_nat() -> None:
    rows = [
        _exit_row("2026-01-03", "AAA", 10.0),   # normal
        _exit_row("", "BBB", 11.0),             # NaN (empty -> NaN on read)
        _exit_row("not-a-date", "CCC", 12.0),   # invalid string
        _exit_row("2026-02-15", "DDD", 13.0),   # normal (sparse mix)
    ]
    with tempfile.TemporaryDirectory(prefix="val-loader-smoke-") as tmp:
        run_dir = Path(tmp)
        _write_trades(run_dir, rows)
        trades = _load_trades(run_dir)

    assert len(trades) == 4, f"expected 4 exit trades, got {len(trades)}"

    # Row 0: normal -> exact timestamp.
    assert trades[0].entry_time == pd.Timestamp("2026-01-03")
    assert trades[0].exit_time == pd.Timestamp("2026-01-03")

    # Row 1: NaN -> NaT (NOT 2000-01-01).
    assert pd.isna(trades[1].entry_time), "NaN timestamp must coerce to NaT"
    assert pd.isna(trades[1].exit_time), "NaN timestamp must coerce to NaT"
    assert trades[1].entry_time != pd.Timestamp("2000-01-01")

    # Row 2: invalid string -> NaT (NOT 2000-01-01).
    assert pd.isna(trades[2].entry_time), "invalid timestamp must coerce to NaT"
    assert pd.isna(trades[2].exit_time), "invalid timestamp must coerce to NaT"
    assert trades[2].entry_time != pd.Timestamp("2000-01-01")

    # Row 3: normal -> exact timestamp.
    assert trades[3].entry_time == pd.Timestamp("2026-02-15")


def _assert_missing_column_uses_2000_default() -> None:
    # No "timestamp" column at all -> legacy "2000-01-01" default applies.
    rows = [
        {
            "code": "AAA",
            "side": "sell",
            "qty": 10,
            "price": 100.0,
            "pnl": 10.0,
            "return_pct": 0.01,
            "reason": "signal",
            "holding_days": 1,
        }
    ]
    with tempfile.TemporaryDirectory(prefix="val-loader-smoke-") as tmp:
        run_dir = Path(tmp)
        _write_trades(run_dir, rows)
        trades = _load_trades(run_dir)

    assert len(trades) == 1
    assert trades[0].entry_time == pd.Timestamp("2000-01-01"), (
        "missing timestamp COLUMN must keep the legacy 2000-01-01 default"
    )
    assert trades[0].exit_time == pd.Timestamp("2000-01-01")


def main() -> None:
    _assert_dirty_timestamps_become_nat()
    _assert_missing_column_uses_2000_default()
    print("validation loader parity smoke: ok")


if __name__ == "__main__":
    main()
