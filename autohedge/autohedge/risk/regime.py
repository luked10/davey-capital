"""Market regime detection (pure classification + optional yfinance snapshot).

``classify_regime`` is a deterministic pure function used by smokes and by the
runtime gates. ``get_regime_snapshot`` is the network-backed convenience that
feeds it from SPY/VIX data; it is never called from offline smokes and fails
soft by returning an "unknown" regime that callers must treat explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


REGIME_VERSION = "0.1.0"

# Thresholds (documented in doctrine; change only with human review).
VIX_PANIC = 30.0
VIX_RISK_OFF = 25.0
VIX_CAUTION = 20.0
SPY_DAY_RETURN_FAST_RISK_OFF = -1.5  # percent
SPY_3D_RETURN_FAST_RISK_OFF = -3.0  # percent

REGIME_SUPPRESSED_REASON = "regime_suppressed"

# Data-only symbols used for regime detection. Never tradeable.
REGIME_DATA_SYMBOLS = ("SPY", "^VIX")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if number != number:
        raise ValueError(f"{name} must not be NaN")
    return number


def classify_regime(
    spy_close: float,
    spy_sma200: float,
    spy_day_return: float,
    spy_3d_return: float,
    vix: float,
) -> dict[str, Any]:
    """Classify the market regime. Returns are percent for the return inputs.

    Precedence (most severe wins):
      VIX > 30                        -> panic         (no longs, x0.0)
      VIX > 25 or SPY < SMA200        -> risk_off      (no longs, x0.0)
      day < -1.5% or 3d < -3%         -> fast_risk_off (no longs, x0.0)
      VIX 20-25                       -> caution       (longs ok,  x0.5)
      else                            -> risk_on       (longs ok,  x1.0)
    """
    close = _require_number(spy_close, "spy_close")
    sma200 = _require_number(spy_sma200, "spy_sma200")
    day_return = _require_number(spy_day_return, "spy_day_return")
    three_day_return = _require_number(spy_3d_return, "spy_3d_return")
    vix_value = _require_number(vix, "vix")
    if close <= 0 or sma200 <= 0:
        raise ValueError("spy_close and spy_sma200 must be positive")

    spy_below_sma200 = close < sma200

    if vix_value > VIX_PANIC:
        regime = "panic"
        allow_new_longs = False
        size_multiplier = 0.0
        reason = f"VIX {vix_value:.2f} > {VIX_PANIC}"
    elif vix_value > VIX_RISK_OFF or spy_below_sma200:
        regime = "risk_off"
        allow_new_longs = False
        size_multiplier = 0.0
        reason = (
            f"VIX {vix_value:.2f} > {VIX_RISK_OFF}"
            if vix_value > VIX_RISK_OFF
            else f"SPY {close:.2f} below SMA200 {sma200:.2f}"
        )
    elif (
        day_return < SPY_DAY_RETURN_FAST_RISK_OFF
        or three_day_return < SPY_3D_RETURN_FAST_RISK_OFF
    ):
        regime = "fast_risk_off"
        allow_new_longs = False
        size_multiplier = 0.0
        reason = (
            f"SPY day return {day_return:.2f}% < {SPY_DAY_RETURN_FAST_RISK_OFF}%"
            if day_return < SPY_DAY_RETURN_FAST_RISK_OFF
            else f"SPY 3d return {three_day_return:.2f}% < {SPY_3D_RETURN_FAST_RISK_OFF}%"
        )
    elif vix_value >= VIX_CAUTION:
        regime = "caution"
        allow_new_longs = True
        size_multiplier = 0.5
        reason = f"VIX {vix_value:.2f} in caution band [{VIX_CAUTION}, {VIX_RISK_OFF}]"
    else:
        regime = "risk_on"
        allow_new_longs = True
        size_multiplier = 1.0
        reason = "no risk triggers"

    return {
        "regime": regime,
        "allow_new_longs": allow_new_longs,
        "size_multiplier": size_multiplier,
        "reason": reason,
        "inputs": {
            "spy_close": close,
            "spy_sma200": sma200,
            "spy_day_return": day_return,
            "spy_3d_return": three_day_return,
            "vix": vix_value,
            "spy_below_sma200": spy_below_sma200,
        },
        "vix_ok": vix_value <= VIX_RISK_OFF,
        "spy_trend_ok": not spy_below_sma200,
        "snapshot_at": _utc_now_iso(),
        "source": "classify_regime",
    }


def unknown_regime(reason: str) -> dict[str, Any]:
    """Fail-soft regime when market data is unavailable.

    New longs stay allowed at half size so a transient data outage cannot
    silently freeze the (dry-run) pipeline, but the regime is clearly labeled
    unknown so downstream audit records show data was missing.
    """
    return {
        "regime": "unknown",
        "allow_new_longs": True,
        "size_multiplier": 0.5,
        "reason": f"regime data unavailable: {reason}",
        "inputs": {},
        "vix_ok": True,
        "spy_trend_ok": True,
        "snapshot_at": _utc_now_iso(),
        "source": "unknown_regime",
    }


def get_regime_snapshot() -> dict[str, Any]:
    """Fetch SPY + VIX from yfinance and classify. Network-backed; never used
    from offline smokes (those call ``classify_regime`` with fixture inputs).
    """
    try:
        import yfinance as yf

        spy = yf.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=False)
        if spy is None or getattr(spy, "empty", True):
            return unknown_regime("no SPY history")
        closes = [float(v) for v in spy["Close"].dropna().tolist()]
        if len(closes) < 4:
            return unknown_regime("insufficient SPY history")
        spy_close = closes[-1]
        sma_window = closes[-200:]
        spy_sma200 = sum(sma_window) / len(sma_window)
        spy_day_return = (closes[-1] - closes[-2]) / closes[-2] * 100.0
        spy_3d_return = (closes[-1] - closes[-4]) / closes[-4] * 100.0

        vix_history = yf.Ticker("^VIX").history(
            period="5d", interval="1d", auto_adjust=False
        )
        if vix_history is None or getattr(vix_history, "empty", True):
            return unknown_regime("no VIX history")
        vix_closes = [float(v) for v in vix_history["Close"].dropna().tolist()]
        if not vix_closes:
            return unknown_regime("no VIX closes")

        snapshot = classify_regime(
            spy_close=spy_close,
            spy_sma200=spy_sma200,
            spy_day_return=spy_day_return,
            spy_3d_return=spy_3d_return,
            vix=vix_closes[-1],
        )
        snapshot["source"] = "yfinance"
        return snapshot
    except Exception as exc:
        return unknown_regime(str(exc))
