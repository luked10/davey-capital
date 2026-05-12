from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BrokerOrderBoi:
    symbol: str
    side: str
    quantity: float
    asset_class: str = 'stock'
    order_type: str = 'market'
    limit_price: float | None = None
    time_in_force: str = 'day'
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerFillBoi:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    fee: float | None = None
    status: str = 'filled'
    asset_class: str = 'stock'
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerPositionBoi:
    symbol: str
    quantity: float
    average_entry_price: float | None = None
    mark_price: float | None = None
    asset_class: str = 'stock'
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccountSnapshotBoi:
    broker: str
    cash_balance: float | None = None
    equity_value: float | None = None
    buying_power: float | None = None
    positions: list[BrokerPositionBoi] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BrokerBoi(ABC):
    broker_name = 'unknown'

    def __init__(
        self,
        *,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.config = config or {}

    @abstractmethod
    def place_order(self, order: BrokerOrderBoi) -> Any:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[BrokerPositionBoi]:
        raise NotImplementedError

    @abstractmethod
    def get_fills(self) -> list[BrokerFillBoi]:
        raise NotImplementedError

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshotBoi:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            'broker_name': self.broker_name,
            'session_id': self.session_id,
            'config_keys': sorted(self.config.keys()),
        }
