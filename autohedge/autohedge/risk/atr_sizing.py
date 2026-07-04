"""ATR-based position sizing (deterministic Python math, never LLM arithmetic).

Risk is the control knob; notional is the consequence. Sonnet may choose a
bucket (stop_mult) and a risk_budget, but every number below is computed in
Python:

    risk_per_share = atr * stop_mult
    shares_by_risk = risk_budget / risk_per_share
    shares_by_cap  = max_notional / entry_price   (hard cap, default $10)
    final_shares   = min(shares_by_risk, shares_by_cap)

``compute_atr14`` is a pure function over caller-supplied bars so smokes stay
offline; ``get_atr14`` is the yfinance-backed convenience wrapper and is never
called from smoke tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


ATR_SIZING_VERSION = "0.1.0"

# Hard per-order notional cap in USD. Matches the existing Alpaca per-order cap.
DEFAULT_MAX_NOTIONAL = 10.0

ATR_PERIOD = 14

# ATR% bucket boundaries (atr / entry_price, expressed as a percent).
CALM_MAX_ATR_PCT = 2.5
NORMAL_MAX_ATR_PCT = 4.5
HOT_MAX_ATR_PCT = 7.0


@dataclass(slots=True, frozen=True)
class AtrBucket:
    """Classification of a symbol's volatility regime by ATR%."""

    name: str
    stop_mult: float
    tp1_r: float
    trail_mult: float
    max_holding_days: int
    skip_recommended: bool = False


def classify_atr_bucket(atr_pct: float) -> AtrBucket:
    """Classify ATR% into a volatility bucket. Classify first, then size.

    ATR% < 2.5   -> calm large cap  (stop_mult 1.75)
    2.5 - 3.5    -> normal tech     (stop_mult 2.0)
    3.5 - 4.5    -> normal tech hot (stop_mult 2.25)
    4.5 - 7.0    -> hot/jumpy tech  (stop_mult 2.5)
    > 7.0        -> extreme         (stop_mult 3.0, skip recommended)
    """
    value = float(atr_pct)
    if value != value or value <= 0:
        raise ValueError(f"atr_pct must be a positive number, got {atr_pct!r}")
    if value < CALM_MAX_ATR_PCT:
        return AtrBucket(
            name="calm_large_cap",
            stop_mult=1.75,
            tp1_r=1.25,
            trail_mult=2.0,
            max_holding_days=7,
        )
    if value <= NORMAL_MAX_ATR_PCT:
        stop_mult = 2.0 if value <= 3.5 else 2.25
        return AtrBucket(
            name="normal_tech",
            stop_mult=stop_mult,
            tp1_r=1.5,
            trail_mult=2.5,
            max_holding_days=5,
        )
    if value <= HOT_MAX_ATR_PCT:
        return AtrBucket(
            name="hot_tech",
            stop_mult=2.5,
            tp1_r=2.0,
            trail_mult=3.0,
            max_holding_days=3,
        )
    return AtrBucket(
        name="extreme",
        stop_mult=3.0,
        tp1_r=2.0,
        trail_mult=3.0,
        max_holding_days=3,
        skip_recommended=True,
    )


@dataclass(slots=True, frozen=True)
class PositionSize:
    """Deterministic sizing result. All arithmetic happens in Python."""

    symbol: str
    entry_price: float
    atr: float
    atr_pct: float
    bucket: str
    stop_mult: float
    risk_budget: float
    risk_per_share: float
    shares_by_risk: float
    shares_by_cap: float
    final_shares: float
    final_notional: float
    max_notional: float
    initial_stop: float
    skip_recommended: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "bucket": self.bucket,
            "stop_mult": self.stop_mult,
            "risk_budget": self.risk_budget,
            "risk_per_share": self.risk_per_share,
            "shares_by_risk": self.shares_by_risk,
            "shares_by_cap": self.shares_by_cap,
            "final_shares": self.final_shares,
            "final_notional": self.final_notional,
            "max_notional": self.max_notional,
            "initial_stop": self.initial_stop,
            "skip_recommended": self.skip_recommended,
        }


def _require_positive(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if number != number or number <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return number


def size_position(
    *,
    symbol: str,
    entry_price: float,
    atr: float,
    risk_budget: float,
    stop_mult: float | None = None,
    max_notional: float = DEFAULT_MAX_NOTIONAL,
) -> PositionSize:
    """Compute the final share count and notional from risk, deterministically.

    ``stop_mult`` normally comes from the ATR bucket (classify first, then
    size); a caller-supplied value (e.g. Sonnet's bucket choice) is clamped to
    the bucket's stop_mult range [1.75, 3.0] so the model can never widen risk
    beyond the doctrine bounds.
    """
    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        raise ValueError("symbol must be non-empty")
    price = _require_positive(entry_price, "entry_price")
    atr_value = _require_positive(atr, "atr")
    budget = _require_positive(risk_budget, "risk_budget")
    cap = _require_positive(max_notional, "max_notional")

    atr_pct = atr_value / price * 100.0
    bucket = classify_atr_bucket(atr_pct)

    if stop_mult is None:
        chosen_stop_mult = bucket.stop_mult
    else:
        chosen_stop_mult = _require_positive(stop_mult, "stop_mult")
        chosen_stop_mult = min(max(chosen_stop_mult, 1.75), 3.0)

    risk_per_share = atr_value * chosen_stop_mult
    shares_by_risk = budget / risk_per_share
    shares_by_cap = cap / price
    final_shares = min(shares_by_risk, shares_by_cap)
    final_notional = final_shares * price
    initial_stop = price - risk_per_share

    return PositionSize(
        symbol=clean_symbol,
        entry_price=price,
        atr=atr_value,
        atr_pct=round(atr_pct, 6),
        bucket=bucket.name,
        stop_mult=chosen_stop_mult,
        risk_budget=budget,
        risk_per_share=round(risk_per_share, 6),
        shares_by_risk=round(shares_by_risk, 6),
        shares_by_cap=round(shares_by_cap, 6),
        final_shares=round(final_shares, 6),
        final_notional=round(final_notional, 6),
        max_notional=cap,
        initial_stop=round(initial_stop, 6),
        skip_recommended=bucket.skip_recommended,
    )


def compute_atr14(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int = ATR_PERIOD,
) -> float:
    """Pure Wilder-style ATR over caller-supplied daily bars (offline-safe).

    Requires at least ``period + 1`` bars so every true range has a previous
    close. Uses the simple mean of the last ``period`` true ranges.
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must have equal length")
    if len(closes) < period + 1:
        raise ValueError(f"need at least {period + 1} bars, got {len(closes)}")

    true_ranges: list[float] = []
    for i in range(1, len(closes)):
        high = float(highs[i])
        low = float(lows[i])
        prev_close = float(closes[i - 1])
        true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
        if true_range != true_range or true_range < 0:
            raise ValueError(f"invalid bar at index {i}")
        true_ranges.append(true_range)

    window = true_ranges[-period:]
    return sum(window) / len(window)


def get_atr14(symbol: str, *, period: int = ATR_PERIOD) -> float | None:
    """Fetch daily bars from yfinance and compute ATR14. Returns None on failure.

    Network-backed convenience only — never called from offline smokes, which
    use ``compute_atr14`` with fixture bars instead.
    """
    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return None
    try:
        import yfinance as yf

        history = yf.Ticker(clean_symbol).history(
            period="3mo",
            interval="1d",
            auto_adjust=False,
        )
        if history is None or getattr(history, "empty", True):
            return None
        highs = [float(v) for v in history["High"].dropna().tolist()]
        lows = [float(v) for v in history["Low"].dropna().tolist()]
        closes = [float(v) for v in history["Close"].dropna().tolist()]
        return compute_atr14(highs, lows, closes, period=period)
    except Exception:
        return None


def build_intent_risk_blocks(
    sizing: PositionSize,
    *,
    cooldown_days: int = 2,
    vix_ok: bool = True,
    spy_trend_ok: bool = True,
) -> dict[str, dict[str, Any]]:
    """Assemble the structured entry/risk/exit_plan/regime_gate intent blocks."""
    bucket = classify_atr_bucket(sizing.atr_pct)
    return {
        "entry": {
            "style": "market",
            "max_notional": sizing.max_notional,
        },
        "risk": {
            "atr14": sizing.atr,
            "stop_mult": sizing.stop_mult,
            "risk_per_share": sizing.risk_per_share,
            "cooldown_days": int(cooldown_days),
        },
        "exit_plan": {
            "initial_stop": sizing.initial_stop,
            "tp1_r": bucket.tp1_r,
            "trail_mult": bucket.trail_mult,
            "max_holding_days": bucket.max_holding_days,
        },
        "regime_gate": {
            "vix_ok": bool(vix_ok),
            "spy_trend_ok": bool(spy_trend_ok),
        },
    }
