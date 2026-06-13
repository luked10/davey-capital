"""Config-only circuit breaker scaffold (deterministic, no-op by default).

This module evaluates locally-supplied observations against a static config.
It performs NO broker calls, NO live position reads, and NO network access.
The default config is disabled and never blocks, so adding this scaffold does
not change any runtime behavior. Enabling it for live execution requires
explicit human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CIRCUIT_BREAKER_SCAFFOLD_VERSION = "0.1.0"


@dataclass(slots=True)
class CircuitBreakerConfig:
    enabled: bool = False
    max_consecutive_losses: int = 3
    daily_loss_limit_pct: float | None = None
    max_daily_loss_pct: float | None = None
    max_open_trades: int | None = None


@dataclass(slots=True)
class CircuitBreakerResult:
    allowed: bool
    needs_human: bool
    reason: str
    triggered_rules: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.allowed


def _non_negative_int(value: Any) -> int | None:
    """Parse a non-negative integer count; None when malformed."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    parsed = int(value)
    if parsed < 0:
        return None
    return parsed


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def evaluate_circuit_breaker(
    config: CircuitBreakerConfig | None = None,
    observations: dict[str, Any] | None = None,
    *,
    consecutive_losses: Any = 0,
    daily_loss_pct: Any = 0.0,
    open_trades: Any = 0,
) -> CircuitBreakerResult:
    """Evaluate circuit breaker rules against locally-supplied observations.

    Inputs are observations only (counts/percentages computed elsewhere from
    repo-backed artifacts). Malformed config or observations fail closed with
    needs_human=True. A disabled config always allows.
    """
    if observations is not None:
        if not isinstance(observations, dict):
            return CircuitBreakerResult(
                allowed=False,
                needs_human=True,
                reason="malformed observations: expected dict",
                triggered_rules=["malformed_observations"],
                observed={"observations": observations},
            )
        consecutive_losses = observations.get("consecutive_losses", consecutive_losses)
        daily_loss_pct = observations.get("daily_loss_pct", daily_loss_pct)
        open_trades = observations.get("open_trades", open_trades)

    observed = {
        "consecutive_losses": consecutive_losses,
        "daily_loss_pct": daily_loss_pct,
        "open_trades": open_trades,
    }

    if config is None:
        config = CircuitBreakerConfig()
    if not isinstance(config, CircuitBreakerConfig):
        return CircuitBreakerResult(
            allowed=False,
            needs_human=True,
            reason="malformed config: expected CircuitBreakerConfig",
            triggered_rules=["malformed_config"],
            observed=observed,
        )
    if not isinstance(config.enabled, bool):
        return CircuitBreakerResult(
            allowed=False,
            needs_human=True,
            reason="malformed config: enabled must be boolean",
            triggered_rules=["malformed_config"],
            observed=observed,
        )

    if config.enabled is False:
        return CircuitBreakerResult(
            allowed=True,
            needs_human=False,
            reason="circuit breaker disabled (default no-op)",
            observed=observed,
        )

    # Enabled path: parse observations strictly; malformed input fails closed.
    parse_failures: list[str] = []
    losses = _non_negative_int(consecutive_losses)
    if losses is None:
        parse_failures.append("consecutive_losses must be a non-negative integer")
    loss_pct = _finite_float(daily_loss_pct)
    if loss_pct is None:
        parse_failures.append("daily_loss_pct must be a finite number")
    trades = _non_negative_int(open_trades)
    if trades is None:
        parse_failures.append("open_trades must be a non-negative integer")

    if parse_failures:
        return CircuitBreakerResult(
            allowed=False,
            needs_human=True,
            reason="malformed observations: " + "; ".join(parse_failures),
            triggered_rules=["malformed_observations"],
            observed=observed,
        )

    triggered: list[str] = []

    max_losses = _non_negative_int(config.max_consecutive_losses)
    if max_losses is None:
        return CircuitBreakerResult(
            allowed=False,
            needs_human=True,
            reason="malformed config: max_consecutive_losses must be a non-negative integer",
            triggered_rules=["malformed_config"],
            observed=observed,
        )
    if losses >= max_losses:
        triggered.append(
            f"max_consecutive_losses: {losses} >= {max_losses}"
        )

    daily_limit = (
        config.max_daily_loss_pct
        if config.max_daily_loss_pct is not None
        else config.daily_loss_limit_pct
    )
    if daily_limit is not None:
        limit_pct = _finite_float(daily_limit)
        if limit_pct is None or limit_pct < 0:
            return CircuitBreakerResult(
                allowed=False,
                needs_human=True,
                reason="malformed config: daily loss limit must be a non-negative number",
                triggered_rules=["malformed_config"],
                observed=observed,
            )
        # daily_loss_pct is a loss magnitude; compare absolute drawdown.
        if abs(loss_pct) >= limit_pct:
            triggered.append(
                f"daily_loss_limit_pct: abs({loss_pct}) >= {limit_pct}"
            )

    if config.max_open_trades is not None:
        max_trades = _non_negative_int(config.max_open_trades)
        if max_trades is None:
            return CircuitBreakerResult(
                allowed=False,
                needs_human=True,
                reason="malformed config: max_open_trades must be a non-negative integer",
                triggered_rules=["malformed_config"],
                observed=observed,
            )
        if trades >= max_trades:
            triggered.append(f"max_open_trades: {trades} >= {max_trades}")

    if triggered:
        return CircuitBreakerResult(
            allowed=False,
            needs_human=True,
            reason="circuit breaker tripped: " + "; ".join(triggered),
            triggered_rules=triggered,
            observed=observed,
        )

    return CircuitBreakerResult(
        allowed=True,
        needs_human=False,
        reason="all circuit breaker rules within limits",
        observed=observed,
    )
