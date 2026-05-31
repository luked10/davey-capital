from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class BrokerOrderAgent:
    symbol: str
    side: str
    quantity: float
    order_type: str = 'market'
    limit_price: float | None = None
    time_in_force: str = 'day'
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerFillAgent:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    fee: float | None = None
    status: str = 'filled'
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerPositionAgent:
    symbol: str
    quantity: float
    average_entry_price: float | None = None
    mark_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccountSnapshotAgent:
    broker: str
    cash_balance: float | None = None
    equity_value: float | None = None
    buying_power: float | None = None
    positions: list[BrokerPositionAgent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dict(self) -> dict[str, Any]:
        return asdict(self)


class BrokerAgent(ABC):
    broker_name = 'unknown'

    def __init__(self, *, session_id: str | None = None, config: dict[str, Any] | None = None) -> None:
        self.session_id = session_id
        self.config = config or {}

    @abstractmethod
    def place_order(self, order: BrokerOrderAgent) -> Any:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[BrokerPositionAgent]:
        raise NotImplementedError

    @abstractmethod
    def get_fills(self) -> list[BrokerFillAgent]:
        raise NotImplementedError

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshotAgent:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            'broker_name': self.broker_name,
            'session_id': self.session_id,
            'config_keys': sorted(self.config.keys()),
        }
