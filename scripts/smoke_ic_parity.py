#!/usr/bin/env python3
"""Deterministic parity smoke for IC under asymmetric NaN masks and ties.

Reproduces the wiped semantic regression in
``vibe-trading/agent/src/tools/factor_analysis_tool._compute_ic_series``:

  - Legacy semantics build the shared non-null mask FIRST (factor present AND
    return present), apply it to BOTH frames, and only THEN rank/correlate.
  - The buggy vectorized variant ranked the FULL frames independently, letting
    values whose counterpart is NaN influence the per-date ranks. On an
    asymmetric-missingness date this diverges from legacy (the originally
    reported blocker observed legacy IC ~0.794118 vs a divergent buggy value;
    that exact figure required ~17 shared tie-free assets, so this test
    reproduces the same class of divergence deterministically with a compact,
    tie-bearing cross-section instead).

This script asserts:
  1. The current implementation matches the legacy per-date Spearman loop
     EXACTLY (including ties).
  2. The buggy "rank the full frame" variant DIVERGES from legacy on the
     asymmetric date (so we are genuinely guarding the regression).
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
VIBE_AGENT_ROOT = REPO_ROOT / "vibe-trading" / "agent"
if str(VIBE_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(VIBE_AGENT_ROOT))

from src.tools.factor_analysis_tool import _compute_ic_series  # noqa: E402


def _legacy_compute_ic_series(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
) -> pd.Series:
    """Legacy ground truth: per-date Spearman on the shared non-null subset."""
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


def _buggy_compute_ic_series(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
) -> pd.Series:
    """The regression: rank the FULL frames independently before correlating."""
    common_dates = factor_df.index.intersection(return_df.index)
    common_codes = factor_df.columns.intersection(return_df.columns)
    if len(common_dates) == 0 or len(common_codes) == 0:
        return pd.Series(dtype=float)

    factor_df = factor_df.loc[common_dates, common_codes]
    return_df = return_df.loc[common_dates, common_codes]

    factor_rank = factor_df.rank(axis=1, method="average", na_option="keep")
    return_rank = return_df.rank(axis=1, method="average", na_option="keep")
    shared_counts = (factor_df.notna() & return_df.notna()).sum(axis=1)
    ic_series = factor_rank.corrwith(return_rank, axis=1, method="pearson")
    ic_series = ic_series[shared_counts >= 5].dropna()
    return ic_series.astype(float)


def _build_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Build frames with an asymmetric-NaN date carrying >=5 shared points and ties."""
    dates = pd.to_datetime(["2026-02-02", "2026-02-03", "2026-02-04"])
    asymmetric_date = dates[1]
    symbols = ["A", "B", "C", "D", "E", "F", "G", "H"]

    factor_df = pd.DataFrame(
        [
            # Fully populated control date.
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
            # Asymmetric date: factor NaN at F; tie between C and D (== 2.0).
            [1.0, 9.0, 2.0, 2.0, 5.0, None, 7.0, 8.0],
            # Second control date with a different ordering.
            [8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
        ],
        index=dates,
        columns=symbols,
    )
    return_df = pd.DataFrame(
        [
            [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08],
            # Asymmetric date: return NaN at B (different column than factor's F).
            [0.01, None, 0.02, 0.03, 0.04, 0.06, 0.05, 0.07],
            [0.02, 0.04, 0.01, 0.03, 0.06, 0.05, 0.08, 0.07],
        ],
        index=dates,
        columns=symbols,
    )
    return factor_df, return_df, asymmetric_date


def main() -> None:
    factor_df, return_df, asymmetric_date = _build_frames()

    legacy = _legacy_compute_ic_series(factor_df, return_df)
    current = _compute_ic_series(factor_df, return_df)
    buggy = _buggy_compute_ic_series(factor_df, return_df)

    # The asymmetric date must survive (>=5 shared) so it is actually tested,
    # and the shared subset must actually contain a tie (factor C == D == 2.0).
    assert asymmetric_date in legacy.index, "asymmetric date missing from legacy IC"
    assert asymmetric_date in current.index, "asymmetric date missing from current IC"
    shared_factor = factor_df.loc[asymmetric_date].where(
        factor_df.loc[asymmetric_date].notna() & return_df.loc[asymmetric_date].notna()
    ).dropna()
    assert len(shared_factor) >= 5, "asymmetric shared subset must have >=5 points"
    assert shared_factor.duplicated().any(), "asymmetric shared subset must contain a tie"

    # 1. Current implementation matches legacy per-date Spearman EXACTLY.
    pd.testing.assert_series_equal(
        current.sort_index(),
        legacy.sort_index(),
        check_names=False,
        check_dtype=False,
        atol=1e-12,
        rtol=1e-12,
    )

    # Pin the legacy value on the asymmetric date as a deterministic regression
    # constant (Spearman on the shared, tie-bearing subset).
    legacy_asym = float(legacy.loc[asymmetric_date])
    expected_asym = 0.9856107606091624
    assert abs(legacy_asym - expected_asym) < 1e-12, (
        f"legacy asymmetric IC drifted: {legacy_asym!r} != {expected_asym!r}"
    )
    assert abs(float(current.loc[asymmetric_date]) - expected_asym) < 1e-12

    # 2. The buggy "rank the full frame" variant must DIVERGE on the asymmetric date.
    buggy_asym = float(buggy.loc[asymmetric_date])
    assert abs(buggy_asym - expected_asym) > 1e-6, (
        "buggy variant unexpectedly matched legacy; test no longer guards the regression"
    )

    print(
        "ic parity smoke: ok "
        f"(asymmetric IC legacy={legacy_asym:.6f}, "
        f"current={float(current.loc[asymmetric_date]):.6f}, "
        f"buggy={buggy_asym:.6f})"
    )


if __name__ == "__main__":
    main()
