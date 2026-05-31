from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from autohedge.brokers.base_agent import (
    AccountSnapshotAgent,
    BrokerAgent,
    BrokerFillAgent,
    BrokerOrderAgent,
    BrokerPositionAgent,
)


def _is_truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class AlpacaBrokerAgent(BrokerAgent):
    """Dry-run-safe Alpaca scaffold.

    This scaffold intentionally does not perform live Alpaca API calls.
    In non-dry-run mode it fails closed.
    """

    broker_name = "alpaca"

    def __init__(
        self,
        *,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(session_id=session_id, config=config)
        self._fills: list[BrokerFillAgent] = []
        self._positions: dict[tuple[str, str], BrokerPositionAgent] = {}
        self._order_seq = 1
        self.live_call_attempts = 0

        self.dry_run = self._read_bool("ALPACA_DRY_RUN", default=True)
        self.api_key = self._read_setting("ALPACA_API_KEY")
        self.api_secret = self._read_setting("ALPACA_API_SECRET")
        self.base_url = self._read_setting(
            "ALPACA_BASE_URL",
            default="https://paper-api.alpaca.markets",
        )

        # Never place live orders in this scaffold.
        self.live_enabled = False

    def _read_setting(self, key: str, *, default: str = "") -> str:
        if self.config:
            for candidate in (key, key.lower()):
                value = self.config.get(candidate)
                if value not in (None, ""):
                    return str(value)
        value = os.getenv(key)
        if value not in (None, ""):
            return str(value)
        return default

    def _read_bool(self, key: str, *, default: bool) -> bool:
        if self.config:
            for candidate in (key, key.lower()):
                if candidate in self.config:
                    return _is_truthy(str(self.config[candidate]), default=default)
        return _is_truthy(os.getenv(key), default=default)

    def _next_order_id(self) -> str:
        order_id = f"alpaca-dry-{self._order_seq}"
        self._order_seq += 1
        return order_id

    def place_order(self, order: BrokerOrderAgent) -> Any:
        if not self.dry_run:
            self.live_call_attempts += 1
            raise RuntimeError(
                "Alpaca live execution is disabled in scaffold mode. "
                "Set ALPACA_DRY_RUN=1 and use dry-run only."
            )

        side = order.side.strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"Unsupported side: {order.side!r}")

        order_type = order.order_type.strip().lower()
        if order_type not in {"market", "limit"}:
            raise ValueError(f"Unsupported order_type: {order.order_type!r}")
        if order_type == "limit" and (order.limit_price is None or float(order.limit_price) <= 0):
            raise ValueError("limit_price must be positive for limit orders")
        if float(order.quantity) <= 0:
            raise ValueError("quantity must be positive")

        order_id = self._next_order_id()
        fill_price = float(order.limit_price) if order.limit_price is not None else None
        fill = BrokerFillAgent(
            order_id=order_id,
            symbol=order.symbol.strip().upper(),
            side=side,
            quantity=float(order.quantity),
            price=fill_price,
            fee=0.0,
            status="simulated",
            asset_class=(order.asset_class or "stock").strip().lower(),
            metadata={
                **dict(order.metadata or {}),
                "dry_run": True,
                "broker": self.broker_name,
                "submitted_at": datetime.now(UTC).isoformat(),
            },
        )
        self._fills.append(fill)

        position_key = (fill.asset_class, fill.symbol)
        prev = self._positions.get(position_key)
        prev_qty = prev.quantity if prev else 0.0
        next_qty = prev_qty + fill.quantity if fill.side == "buy" else prev_qty - fill.quantity
        if next_qty == 0:
            self._positions.pop(position_key, None)
        else:
            self._positions[position_key] = BrokerPositionAgent(
                symbol=fill.symbol,
                quantity=next_qty,
                average_entry_price=fill.price,
                mark_price=fill.price,
                asset_class=fill.asset_class,
                metadata={"dry_run": True, "broker": self.broker_name},
            )

        return {
            "order_id": order_id,
            "status": "simulated",
            "dry_run": True,
            "live_call_made": False,
            "fill": fill,
        }

    def place_execution_intent(self, intent: Any, risk: Any = None, run: Any = None) -> Any:
        # Local import avoids hard dependency cycles at module import time.
        from contracts.bridge_contract import (
            execution_intent_to_broker_order,
            validate_execution_intent,
        )

        try:
            validation = validate_execution_intent(intent, risk=risk, run=run)
            normalized_intent = validation.require_allowed()
        except Exception as exc:
            raise ValueError("Execution intent blocked: unable to validate intent safely") from exc

        if not normalized_intent.approved:
            raise ValueError("Execution intent blocked: approved=True required")

        payload = execution_intent_to_broker_order(normalized_intent, risk=risk, run=run)
        order = BrokerOrderAgent(
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            quantity=float(payload["quantity"]),
            order_type=str(payload["order_type"]),
            limit_price=payload.get("limit_price"),
            time_in_force=str(payload.get("time_in_force", "day")),
            asset_class=str(payload.get("asset_class", "stock")),
            metadata=dict(payload.get("metadata", {})),
        )
        return self.place_order(order)

    def cancel_order(self, order_id: str) -> Any:
        return {
            "order_id": order_id,
            "status": "canceled",
            "dry_run": True,
            "live_call_made": False,
        }

    def get_positions(self) -> list[BrokerPositionAgent]:
        return list(self._positions.values())

    def get_fills(self) -> list[BrokerFillAgent]:
        return list(self._fills)

    def get_account_snapshot(self) -> AccountSnapshotAgent:
        return AccountSnapshotAgent(
            broker=self.broker_name,
            cash_balance=None,
            equity_value=None,
            buying_power=None,
            positions=self.get_positions(),
            metadata={
                "dry_run": self.dry_run,
                "live_enabled": self.live_enabled,
                "base_url": self.base_url,
                "live_call_attempts": self.live_call_attempts,
            },
        )


# Backwards-compatible alias.
AlpacaBrokerBoi = AlpacaBrokerAgent
