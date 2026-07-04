"""Exit plan state machine for open positions (deterministic, offline-safe).

State machine: ARMED -> TP1_HIT -> TRAILING -> EXITED.

``ExitPlan.update(close_price)`` returns exactly one action per call:

- "hold"           keep the position; peak price is refreshed.
- "take_profit_1"  TP1 (or the +20% windfall rule) fired; the sell is
                   INJECTED FOR POKE APPROVAL, never auto-executed.
- "exit_stop"      hard stop — the ONLY action that may execute immediately
                   without Poke approval (initial ATR stop or the -7%
                   unrealized-PnL hard stop).
- "exit_trailing"  trailing stop or time stop fired; the sell is injected
                   for Poke approval.

All prices/decisions are pure functions of caller-supplied inputs: no broker
reads, no network, no hidden state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any


EXIT_PLAN_VERSION = "0.1.0"

# Portfolio-level hard rules (unrealized PnL, percent of entry price).
HARD_STOP_PNL_PCT = -7.0
WINDFALL_TP_PNL_PCT = 20.0

# Cooldowns (trading days) applied after exits/rejections.
STOP_OUT_COOLDOWN_TRADING_DAYS = 2
REGIME_REJECT_COOLDOWN_DAYS = 1


class ExitState(str, Enum):
    ARMED = "armed"
    TP1_HIT = "tp1_hit"
    TRAILING = "trailing"
    EXITED = "exited"


@dataclass(slots=True, frozen=True)
class ExitThresholds:
    name: str
    stop_mult: float
    tp1_r: float
    trail_mult: float
    max_holding_days: int


# Named presets from doctrine; pick by symbol character, not by model output.
EXIT_THRESHOLD_PRESETS: dict[str, ExitThresholds] = {
    "stable_mega_cap": ExitThresholds(
        name="stable_mega_cap",
        stop_mult=1.75,
        tp1_r=1.25,
        trail_mult=2.0,
        max_holding_days=7,
    ),
    "volatile_tech": ExitThresholds(
        name="volatile_tech",
        stop_mult=2.25,
        tp1_r=1.5,
        trail_mult=2.5,
        max_holding_days=5,
    ),
    "high_vol_event": ExitThresholds(
        name="high_vol_event",
        stop_mult=2.75,
        tp1_r=2.0,
        trail_mult=3.0,
        max_holding_days=3,
    ),
}


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def add_trading_days(start: date, trading_days: int) -> date:
    """Return the date ``trading_days`` weekdays after ``start`` (Mon-Fri)."""
    current = start
    remaining = max(0, int(trading_days))
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


@dataclass(slots=True)
class ExitPlan:
    """Per-position exit state machine, serializable into runtime_state.json."""

    symbol: str
    entry_price: float
    atr: float
    stop_mult: float
    tp1_r: float
    trail_mult: float
    max_holding_days: int
    entry_date: str = ""
    state: ExitState = ExitState.ARMED
    peak_price: float = 0.0
    last_action: str = ""
    last_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "").strip().upper()
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        for name in ("entry_price", "atr", "stop_mult", "tp1_r", "trail_mult"):
            value = float(getattr(self, name))
            if value != value or value <= 0:
                raise ValueError(f"{name} must be positive")
            setattr(self, name, value)
        self.max_holding_days = int(self.max_holding_days)
        if self.max_holding_days <= 0:
            raise ValueError("max_holding_days must be positive")
        if isinstance(self.state, str):
            self.state = ExitState(self.state)
        if self.peak_price < self.entry_price:
            self.peak_price = self.entry_price

    # -- Derived levels (pure) -------------------------------------------------

    @property
    def risk_per_share(self) -> float:
        return self.atr * self.stop_mult

    @property
    def initial_stop(self) -> float:
        return self.entry_price - self.risk_per_share

    @property
    def tp1_price(self) -> float:
        return self.entry_price + self.tp1_r * self.risk_per_share

    @property
    def trailing_stop(self) -> float:
        return self.peak_price - self.trail_mult * self.atr

    def unrealized_pnl_pct(self, close_price: float) -> float:
        return (float(close_price) - self.entry_price) / self.entry_price * 100.0

    def days_held(self, as_of: date | None = None) -> int:
        entered = _parse_date(self.entry_date)
        if entered is None:
            return 0
        return max(0, ((as_of or _utc_today()) - entered).days)

    # -- State machine ----------------------------------------------------------

    def update(self, close_price: float, *, as_of: date | None = None) -> str:
        """Advance the state machine for one observed close price."""
        price = float(close_price)
        if price != price or price <= 0:
            raise ValueError(f"close_price must be positive, got {close_price!r}")

        if self.state is ExitState.EXITED:
            self.last_action = "hold"
            self.last_reason = "already exited"
            return "hold"

        self.peak_price = max(self.peak_price, price)
        pnl_pct = self.unrealized_pnl_pct(price)

        # Hard stop: the ONLY no-approval exit path. Applies in every state.
        if pnl_pct < HARD_STOP_PNL_PCT or (
            self.state is ExitState.ARMED and price <= self.initial_stop
        ):
            self.state = ExitState.EXITED
            self.last_action = "exit_stop"
            self.last_reason = (
                f"hard stop: unrealized_pnl_pct={pnl_pct:.2f} <= {HARD_STOP_PNL_PCT}"
                if pnl_pct < HARD_STOP_PNL_PCT
                else f"initial ATR stop {self.initial_stop:.4f} hit at {price:.4f}"
            )
            return "exit_stop"

        if self.state is ExitState.ARMED:
            if pnl_pct > WINDFALL_TP_PNL_PCT or price >= self.tp1_price:
                self.state = ExitState.TP1_HIT
                self.last_action = "take_profit_1"
                self.last_reason = (
                    f"windfall: unrealized_pnl_pct={pnl_pct:.2f} > {WINDFALL_TP_PNL_PCT}"
                    if pnl_pct > WINDFALL_TP_PNL_PCT
                    else f"tp1 {self.tp1_price:.4f} reached at {price:.4f}"
                )
                return "take_profit_1"
            if self.days_held(as_of) > self.max_holding_days:
                self.state = ExitState.TRAILING
                self.last_action = "exit_trailing"
                self.last_reason = (
                    f"time stop: held {self.days_held(as_of)}d > "
                    f"{self.max_holding_days}d"
                )
                return "exit_trailing"
            self.last_action = "hold"
            self.last_reason = ""
            return "hold"

        if self.state is ExitState.TP1_HIT:
            # After TP1 the remainder trails; the transition itself is a hold.
            self.state = ExitState.TRAILING

        # TRAILING
        if price <= self.trailing_stop:
            self.state = ExitState.EXITED
            self.last_action = "exit_trailing"
            self.last_reason = (
                f"trailing stop {self.trailing_stop:.4f} hit at {price:.4f} "
                f"(peak {self.peak_price:.4f})"
            )
            return "exit_trailing"
        if self.days_held(as_of) > self.max_holding_days:
            self.state = ExitState.EXITED
            self.last_action = "exit_trailing"
            self.last_reason = (
                f"time stop: held {self.days_held(as_of)}d > {self.max_holding_days}d"
            )
            return "exit_trailing"
        self.last_action = "hold"
        self.last_reason = ""
        return "hold"

    def mark_exited(self, reason: str = "") -> None:
        self.state = ExitState.EXITED
        if reason:
            self.last_reason = reason

    # -- Serialization ----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "atr": self.atr,
            "stop_mult": self.stop_mult,
            "tp1_r": self.tp1_r,
            "trail_mult": self.trail_mult,
            "max_holding_days": self.max_holding_days,
            "entry_date": self.entry_date,
            "state": self.state.value,
            "peak_price": self.peak_price,
            "initial_stop": self.initial_stop,
            "tp1_price": self.tp1_price,
            "trailing_stop": self.trailing_stop,
            "last_action": self.last_action,
            "last_reason": self.last_reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExitPlan":
        if not isinstance(payload, dict):
            raise ValueError("exit plan payload must be a dict")
        return cls(
            symbol=payload.get("symbol", ""),
            entry_price=payload.get("entry_price", 0.0),
            atr=payload.get("atr", 0.0),
            stop_mult=payload.get("stop_mult", 0.0),
            tp1_r=payload.get("tp1_r", 0.0),
            trail_mult=payload.get("trail_mult", 0.0),
            max_holding_days=payload.get("max_holding_days", 0),
            entry_date=str(payload.get("entry_date", "") or ""),
            state=ExitState(str(payload.get("state", ExitState.ARMED.value))),
            peak_price=float(payload.get("peak_price", 0.0) or 0.0),
            last_action=str(payload.get("last_action", "") or ""),
            last_reason=str(payload.get("last_reason", "") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )


def exit_plan_from_preset(
    *,
    symbol: str,
    entry_price: float,
    atr: float,
    preset: str,
    entry_date: str = "",
    metadata: dict[str, Any] | None = None,
) -> ExitPlan:
    thresholds = EXIT_THRESHOLD_PRESETS.get(str(preset or "").strip())
    if thresholds is None:
        raise ValueError(
            f"unknown exit preset {preset!r}; expected one of "
            f"{sorted(EXIT_THRESHOLD_PRESETS)}"
        )
    return ExitPlan(
        symbol=symbol,
        entry_price=entry_price,
        atr=atr,
        stop_mult=thresholds.stop_mult,
        tp1_r=thresholds.tp1_r,
        trail_mult=thresholds.trail_mult,
        max_holding_days=thresholds.max_holding_days,
        entry_date=entry_date,
        metadata=dict(metadata or {}),
    )


def cooldown_until(
    *,
    reason: str,
    start: date | None = None,
) -> str:
    """Return the ISO date a symbol stays cooled down until.

    2 trading days after a stop-out; 1 calendar day after a regime rejection.
    """
    begin = start or _utc_today()
    if reason == "stop_out":
        return add_trading_days(begin, STOP_OUT_COOLDOWN_TRADING_DAYS).isoformat()
    return (begin + timedelta(days=REGIME_REJECT_COOLDOWN_DAYS)).isoformat()


def is_cooldown_active(until_iso: str, *, as_of: date | None = None) -> bool:
    until = _parse_date(until_iso)
    if until is None:
        return False
    return (as_of or _utc_today()) < until
