"""Optional dividend-focused risk gates for backtests.

This module evaluates two optional risk paths:
1) Dividend quality checks.
2) Stress-test scenario checks.

Both paths are config-driven and disabled by default.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Iterable, Optional

import pandas as pd


_PAYOUT_RATIO_ALIASES = (
    "payout_ratio",
    "dividend_payout_ratio",
    "fina_indicator_payout_ratio",
    "fina_indicator_cash_div_tax_ratio",
)
_DIVIDEND_ALIASES = (
    "dividend_per_share",
    "dps",
    "fina_indicator_dps",
)
_DIVIDEND_CASH_OUTFLOW_ALIASES = (
    "dividends_paid",
    "cash_dividends_paid",
    "cashflow_dividends_paid",
)
_FREE_CASH_FLOW_ALIASES = (
    "free_cash_flow",
    "cashflow_free_cash_flow",
    "cashflow_n_cashflow_act",
)
_DEBT_TO_EQUITY_ALIASES = (
    "debt_to_equity",
    "fina_indicator_debt_to_equity",
)
_DEBT_TO_ASSETS_ALIASES = (
    "debt_to_assets",
    "fina_indicator_debt_to_assets",
)
_TOTAL_DEBT_ALIASES = ("total_debt", "balancesheet_total_liab")
_TOTAL_ASSET_ALIASES = ("total_assets", "balancesheet_total_assets")
_TOTAL_EQUITY_ALIASES = (
    "total_equity",
    "balancesheet_total_hldr_eqy_exc_min_int",
)
_DIVIDEND_YIELD_ALIASES = ("dividend_yield", "trailing_dividend_yield")
_REVENUE_GROWTH_ALIASES = (
    "revenue_growth",
    "income_revenue_yoy",
    "income_total_revenue_yoy",
)


def apply_risk_gates(
    config: Dict[str, Any],
    data_map: Dict[str, pd.DataFrame],
    signal_map: Dict[str, pd.Series],
) -> tuple[Dict[str, pd.Series], Dict[str, Any]]:
    """Evaluate optional risk gates and optionally zero out failing signals."""
    risk_cfg = config.get("risk_gates")
    if not isinstance(risk_cfg, Mapping):
        return signal_map, {}

    updated = dict(signal_map)
    report: Dict[str, Any] = {}

    dividend_cfg = risk_cfg.get("dividend_quality")
    if isinstance(dividend_cfg, Mapping) and dividend_cfg.get("enabled"):
        dividend_result = evaluate_dividend_quality(data_map, dividend_cfg)
        report["dividend_quality"] = dividend_result
        if _is_enforced(dividend_cfg):
            _apply_symbol_gate(updated, dividend_result.get("blocked_symbols", []))

    stress_cfg = risk_cfg.get("stress_test")
    if isinstance(stress_cfg, Mapping) and stress_cfg.get("enabled"):
        stress_result = evaluate_stress_test(data_map, stress_cfg)
        report["stress_test"] = stress_result
        if _is_enforced(stress_cfg):
            _apply_symbol_gate(updated, stress_result.get("blocked_symbols", []))

    return updated, report


def evaluate_dividend_quality(
    data_map: Dict[str, pd.DataFrame],
    cfg: Mapping[str, Any],
) -> Dict[str, Any]:
    """Evaluate configurable dividend-quality checks per symbol."""
    payout_ratio_ceiling = _as_float(cfg.get("payout_ratio_ceiling"), 0.8)
    growth_years = max(1, int(cfg.get("dividend_growth_years", 3)))
    require_fcf_coverage = bool(cfg.get("require_fcf_coverage", True))
    debt_to_equity_ceiling = _as_float(cfg.get("debt_to_equity_ceiling"), 2.0)
    debt_to_assets_ceiling = _as_float(cfg.get("debt_to_assets_ceiling"), 0.75)
    min_checks_passed = max(1, int(cfg.get("min_checks_passed", 3)))
    fail_on_insufficient_data = bool(cfg.get("fail_on_insufficient_data", False))

    per_symbol: Dict[str, Any] = {}
    blocked: list[str] = []

    for symbol, frame in data_map.items():
        checks: Dict[str, Optional[bool]] = {}

        payout_ratio = _latest_value(frame, _PAYOUT_RATIO_ALIASES)
        checks["payout_ratio_ceiling"] = None if payout_ratio is None else payout_ratio <= payout_ratio_ceiling

        checks["positive_dividend_growth"] = _has_positive_dividend_growth(frame, years=growth_years)

        fcf = _latest_value(frame, _FREE_CASH_FLOW_ALIASES)
        dividend_outflow = _latest_value(frame, _DIVIDEND_CASH_OUTFLOW_ALIASES)
        if require_fcf_coverage:
            if fcf is None or dividend_outflow is None:
                checks["free_cash_flow_coverage"] = None
            else:
                checks["free_cash_flow_coverage"] = fcf >= abs(dividend_outflow)

        debt_to_equity = _latest_value(frame, _DEBT_TO_EQUITY_ALIASES)
        debt_to_assets = _latest_value(frame, _DEBT_TO_ASSETS_ALIASES)
        debt_sanity_ok = _debt_sanity_ok(frame, debt_to_assets_ceiling)
        checks["debt_sanity"] = debt_sanity_ok
        checks["debt_to_equity_ceiling"] = None if debt_to_equity is None else debt_to_equity <= debt_to_equity_ceiling
        checks["debt_to_assets_ceiling"] = None if debt_to_assets is None else debt_to_assets <= debt_to_assets_ceiling

        passed = sum(1 for ok in checks.values() if ok is True)
        evaluated = sum(1 for ok in checks.values() if ok is not None)
        required = min(min_checks_passed, evaluated) if evaluated > 0 else min_checks_passed
        if evaluated == 0 and not fail_on_insufficient_data:
            is_pass = True
        elif evaluated < min_checks_passed and not fail_on_insufficient_data:
            is_pass = passed == evaluated
        else:
            is_pass = passed >= required and evaluated > 0
        if not is_pass:
            blocked.append(symbol)

        per_symbol[symbol] = {
            "pass": is_pass,
            "checks": checks,
            "thresholds": {
                "payout_ratio_ceiling": payout_ratio_ceiling,
                "dividend_growth_years": growth_years,
                "debt_to_equity_ceiling": debt_to_equity_ceiling,
                "debt_to_assets_ceiling": debt_to_assets_ceiling,
                "min_checks_passed": min_checks_passed,
                "fail_on_insufficient_data": fail_on_insufficient_data,
            },
        }

    return {
        "enabled": True,
        "blocked_symbols": sorted(blocked),
        "per_symbol": per_symbol,
    }


def evaluate_stress_test(
    data_map: Dict[str, pd.DataFrame],
    cfg: Mapping[str, Any],
) -> Dict[str, Any]:
    """Evaluate downside stress scenarios for dividend positions."""
    recession_drawdown = _as_float(cfg.get("recession_drawdown_pct"), -0.3)
    rate_hike_bps = _as_float(cfg.get("rate_hike_bps"), 150.0)
    revenue_slowdown = _as_float(cfg.get("revenue_slowdown_pct"), -0.1)
    max_loss = _as_float(cfg.get("max_aggregate_stress_loss_pct"), -0.25)

    per_symbol: Dict[str, Any] = {}
    blocked: list[str] = []

    for symbol, frame in data_map.items():
        div_yield = _latest_value(frame, _DIVIDEND_YIELD_ALIASES) or 0.0
        debt_to_equity = _latest_value(frame, _DEBT_TO_EQUITY_ALIASES) or 1.0
        revenue_growth = _latest_value(frame, _REVENUE_GROWTH_ALIASES)
        if revenue_growth is None:
            revenue_growth = 0.0

        recession_loss = float(min(-0.05, recession_drawdown * (1 + max(div_yield, 0.0))))
        rate_hike_loss = float(min(-0.02, -(rate_hike_bps / 10_000.0) * (0.5 + max(debt_to_equity, 0.0))))
        slowdown_factor = max(0.0, abs(revenue_slowdown) - revenue_growth)
        revenue_slowdown_loss = float(min(-0.02, -(abs(revenue_slowdown) + slowdown_factor)))
        aggregate = (recession_loss + rate_hike_loss + revenue_slowdown_loss) / 3.0
        is_pass = aggregate >= max_loss
        if not is_pass:
            blocked.append(symbol)

        per_symbol[symbol] = {
            "pass": is_pass,
            "scenarios": {
                "recession_drawdown_pct": round(recession_loss, 4),
                "rate_hike_sensitivity_pct": round(rate_hike_loss, 4),
                "revenue_slowdown_pct": round(revenue_slowdown_loss, 4),
            },
            "aggregate_stress_loss_pct": round(float(aggregate), 4),
            "thresholds": {
                "max_aggregate_stress_loss_pct": max_loss,
                "rate_hike_bps": rate_hike_bps,
            },
        }

    return {
        "enabled": True,
        "blocked_symbols": sorted(blocked),
        "per_symbol": per_symbol,
    }


def _is_enforced(cfg: Mapping[str, Any]) -> bool:
    return bool(cfg.get("enforce", False))


def _apply_symbol_gate(signal_map: Dict[str, pd.Series], symbols: Iterable[str]) -> None:
    for symbol in symbols:
        if symbol in signal_map:
            signal_map[symbol] = signal_map[symbol] * 0.0


def _has_positive_dividend_growth(frame: pd.DataFrame, years: int) -> Optional[bool]:
    series = _latest_series(frame, _DIVIDEND_ALIASES)
    if series is None:
        return None

    raw_values = [float(v) for v in series.dropna().tolist() if pd.notna(v)]
    values: list[float] = []
    for value in raw_values:
        if not values or value != values[-1]:
            values.append(value)
    if len(values) < years + 1:
        return None
    recent = values[-(years + 1):]
    return all(later > earlier for earlier, later in zip(recent, recent[1:]))


def _debt_sanity_ok(frame: pd.DataFrame, debt_to_assets_ceiling: float) -> Optional[bool]:
    total_debt = _latest_value(frame, _TOTAL_DEBT_ALIASES)
    total_assets = _latest_value(frame, _TOTAL_ASSET_ALIASES)
    total_equity = _latest_value(frame, _TOTAL_EQUITY_ALIASES)

    checks: list[bool] = []
    if total_debt is not None:
        checks.append(total_debt >= 0)
    if total_assets is not None:
        checks.append(total_assets > 0)
    if total_debt is not None and total_assets is not None and total_assets > 0:
        checks.append((total_debt / total_assets) <= debt_to_assets_ceiling)
    if total_debt is not None and total_equity is not None and total_equity > 0:
        checks.append((total_debt / total_equity) <= 5.0)
    if not checks:
        return None
    return all(checks)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _latest_series(frame: pd.DataFrame, aliases: Iterable[str]) -> Optional[pd.Series]:
    for name in aliases:
        if name in frame.columns:
            series = frame[name]
            if not series.dropna().empty:
                return series
    return None


def _latest_value(frame: pd.DataFrame, aliases: Iterable[str]) -> Optional[float]:
    series = _latest_series(frame, aliases)
    if series is None:
        return None
    non_na = series.dropna()
    if non_na.empty:
        return None
    try:
        return float(non_na.iloc[-1])
    except (TypeError, ValueError):
        return None
