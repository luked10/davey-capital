from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autohedge.brokers.base_agent import (
    AccountSnapshotBoi,
    BrokerBoi,
    BrokerFillBoi,
    BrokerOrderBoi,
    BrokerPositionBoi,
)


@dataclass(slots=True)
class PaperStateBoi:
    broker_name: str = 'paper'
    state_path: str | None = None
    starting_cash: float = 100000.0
    cash_balance: float = 100000.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    last_prices: dict[str, float] = field(default_factory=dict)
    next_order_id: int = 1
    next_fill_id: int = 1
    last_updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PaperStateStoreBoi:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> PaperStateBoi:
        if not self.path.exists():
            return PaperStateBoi(state_path=str(self.path))

        payload = json.loads(self.path.read_text())
        return PaperStateBoi(
            broker_name=payload.get('broker_name', 'paper'),
            state_path=payload.get('state_path', str(self.path)),
            starting_cash=self._safe_float(payload.get('starting_cash')) or 100000.0,
            cash_balance=self._safe_float(payload.get('cash_balance'))
            or self._safe_float(payload.get('starting_cash'))
            or 100000.0,
            positions=list(payload.get('positions', [])),
            open_orders=list(payload.get('open_orders', [])),
            fills=list(payload.get('fills', [])),
            last_prices={
                str(symbol): float(price)
                for symbol, price in dict(payload.get('last_prices', {})).items()
                if price is not None
            },
            next_order_id=self._safe_int(payload.get('next_order_id')) or 1,
            next_fill_id=self._safe_int(payload.get('next_fill_id')) or 1,
            last_updated_at=payload.get('last_updated_at'),
            metadata=dict(payload.get('metadata', {})),
        )

    def save(self, state: PaperStateBoi) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload['state_path'] = str(self.path)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is None or value == '':
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: Any) -> int | None:
        try:
            if value is None or value == '':
                return None
            return int(value)
        except (TypeError, ValueError):
            return None


class PaperBrokerBoi(BrokerBoi):
    broker_name = 'paper'

    def __init__(
        self,
        *,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._load_dotenv_if_available()
        super().__init__(session_id=session_id, config=config)
        self.state_path = Path(
            self._read_setting('PAPER_STATE_PATH', default='.autohedge/paper_state_agent.json')
        )
        self.state_store = PaperStateStoreBoi(self.state_path)
        self.state = self.state_store.load()
        self.starting_cash = self._read_float('PAPER_STARTING_CASH', default=str(self.state.starting_cash))
        self.fee_rate = self._read_float('PAPER_FEE_RATE', default='0.0')
        self.slippage_bps = self._read_float('PAPER_SLIPPAGE_BPS', default='0.0')
        self.allow_short = self._read_bool('PAPER_ALLOW_SHORT', default='false')
        self.allow_margin = self._read_bool('PAPER_ALLOW_MARGIN', default='false')
        self.price_map = self._load_price_map()

        if not self.state.positions and self.state.cash_balance == 100000.0 and self.starting_cash != 100000.0:
            self.state.starting_cash = self.starting_cash
            self.state.cash_balance = self.starting_cash
            self._touch_state()
            self.state_store.save(self.state)

    def _load_dotenv_if_available(self) -> None:
        candidates = [
            Path.cwd() / '.env',
            Path.cwd() / 'scratch_robinhood' / '.env',
            Path(__file__).resolve().parents[3] / '.env',
            Path(__file__).resolve().parents[4] / '.env',
        ]
        for env_path in candidates:
            if not env_path.exists():
                continue
            try:
                for raw_line in env_path.read_text().splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
                return
            except OSError:
                continue

    def _read_setting(self, *keys: str, default: str = '') -> str:
        for key in keys:
            if self.config:
                value = self.config.get(key.lower())
                if value not in (None, ''):
                    return str(value)
                value = self.config.get(key)
                if value not in (None, ''):
                    return str(value)
            value = os.getenv(key)
            if value not in (None, ''):
                return value
        return default

    def _read_float(self, *keys: str, default: str = '0.0') -> float:
        value = self._read_setting(*keys, default=default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _read_bool(self, *keys: str, default: str = 'false') -> bool:
        value = self._read_setting(*keys, default=default).strip().lower()
        return value in {'1', 'true', 'yes', 'y', 'on'}

    def _load_price_map(self) -> dict[str, float]:
        raw = self._read_setting('PAPER_LAST_PRICES', 'PAPER_PRICES', default='')
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, float] = {}
        for symbol, price in payload.items():
            try:
                if price is None or price == '':
                    continue
                result[str(symbol)] = float(price)
            except (TypeError, ValueError):
                continue
        return result

    def _timestamp(self) -> str:
        return datetime.now(UTC).isoformat()

    def _touch_state(self) -> None:
        self.state.last_updated_at = self._timestamp()
        self.state_store.save(self.state)

    def _next_order_id(self) -> str:
        order_id = f'paper-order-{self.state.next_order_id}'
        self.state.next_order_id += 1
        return order_id

    def _next_fill_id(self) -> str:
        fill_id = f'paper-fill-{self.state.next_fill_id}'
        self.state.next_fill_id += 1
        return fill_id

    def _market_price(self, order: BrokerOrderBoi) -> float:
        symbol = order.symbol.strip()
        if symbol in self.state.last_prices:
            return float(self.state.last_prices[symbol])
        if symbol in self.price_map:
            return float(self.price_map[symbol])
        if order.limit_price is not None:
            return float(order.limit_price)
        metadata = order.metadata or {}
        for key in ('mark_price', 'market_price', 'price', 'last_price'):
            value = metadata.get(key)
            if value not in (None, ''):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 1.0

    def _apply_slippage(self, price: float, side: str) -> float:
        if self.slippage_bps <= 0:
            return price
        basis_points = self.slippage_bps / 10000.0
        if side == 'buy':
            return price * (1.0 + basis_points)
        return price * (1.0 - basis_points)

    def _calc_fee(self, quantity: float, price: float) -> float:
        if self.fee_rate <= 0:
            return 0.0
        return abs(quantity * price) * self.fee_rate

    def _position_key(self, symbol: str, asset_class: str) -> tuple[str, str]:
        return (asset_class.strip().lower() or 'stock', symbol.strip())

    def _get_position_index(self, symbol: str, asset_class: str) -> int | None:
        key_asset, key_symbol = self._position_key(symbol, asset_class)
        for index, position in enumerate(self.state.positions):
            if (
                str(position.get('symbol', '')).strip() == key_symbol
                and str(position.get('asset_class', 'stock')).strip().lower() == key_asset
            ):
                return index
        return None

    def _upsert_position(
        self,
        *,
        symbol: str,
        asset_class: str,
        quantity_delta: float,
        fill_price: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        index = self._get_position_index(symbol, asset_class)
        if index is None:
            if quantity_delta <= 0:
                raise ValueError('Cannot sell a non-existent paper position.')
            self.state.positions.append(
                {
                    'symbol': symbol,
                    'quantity': quantity_delta,
                    'average_entry_price': fill_price,
                    'mark_price': fill_price,
                    'asset_class': asset_class,
                    'metadata': metadata or {},
                }
            )
            return

        position = dict(self.state.positions[index])
        current_quantity = float(position.get('quantity', 0.0))
        current_avg = position.get('average_entry_price')
        current_avg_price = float(current_avg) if current_avg not in (None, '') else fill_price
        new_quantity = current_quantity + quantity_delta

        if new_quantity < 0 and not self.allow_short:
            raise ValueError('PaperBrokerBoi does not allow short positions by default.')

        if quantity_delta > 0:
            total_cost_basis = (current_quantity * current_avg_price) + (quantity_delta * fill_price)
            average_entry_price = total_cost_basis / new_quantity if new_quantity else fill_price
        else:
            average_entry_price = current_avg_price if new_quantity else None

        if new_quantity == 0:
            del self.state.positions[index]
            return

        position.update(
            {
                'quantity': new_quantity,
                'average_entry_price': average_entry_price,
                'mark_price': fill_price,
                'asset_class': asset_class,
                'metadata': {**dict(position.get('metadata', {})), **(metadata or {})},
            }
        )
        self.state.positions[index] = position

    def _record_fill(
        self,
        order: BrokerOrderBoi,
        *,
        order_id: str,
        fill_price: float,
        status: str = 'filled',
    ) -> dict[str, Any]:
        fee = self._calc_fee(order.quantity, fill_price)
        signed_quantity = float(order.quantity)
        side = order.side.strip().lower()
        if side == 'buy':
            cash_delta = -(signed_quantity * fill_price) - fee
            quantity_delta = signed_quantity
        elif side == 'sell':
            cash_delta = (signed_quantity * fill_price) - fee
            quantity_delta = -signed_quantity
        else:
            raise ValueError(f'Unsupported side: {order.side}')

        projected_cash = self.state.cash_balance + cash_delta
        if projected_cash < 0 and not self.allow_margin:
            raise ValueError('PaperBrokerBoi does not allow negative cash by default.')

        self.state.cash_balance = projected_cash
        self._upsert_position(
            symbol=order.symbol,
            asset_class=order.asset_class,
            quantity_delta=quantity_delta,
            fill_price=fill_price,
            metadata=order.metadata,
        )
        self.state.last_prices[order.symbol.strip()] = fill_price

        fill_record = {
            'fill_id': self._next_fill_id(),
            'order_id': order_id,
            'symbol': order.symbol,
            'side': side,
            'quantity': float(order.quantity),
            'price': fill_price,
            'fee': fee,
            'status': status,
            'asset_class': order.asset_class,
            'timestamp': self._timestamp(),
            'metadata': dict(order.metadata or {}),
        }
        self.state.fills.append(fill_record)
        self._touch_state()
        return fill_record

    def _limit_is_executable(self, order: BrokerOrderBoi, market_price: float) -> bool:
        side = order.side.strip().lower()
        if side == 'buy':
            return order.limit_price is not None and order.limit_price >= market_price
        return order.limit_price is not None and order.limit_price <= market_price

    def place_order(self, order: BrokerOrderBoi) -> Any:
        side = order.side.strip().lower()
        order_type = order.order_type.strip().lower()
        asset_class = order.asset_class.strip().lower() or 'stock'
        order_id = self._next_order_id()
        market_price = self._market_price(order)

        if order_type == 'market':
            fill_price = self._apply_slippage(market_price, side)
            fill_record = self._record_fill(order, order_id=order_id, fill_price=fill_price)
            return {
                'order_id': order_id,
                'status': 'filled',
                'fill': fill_record,
            }

        if order_type == 'limit':
            if order.limit_price is None:
                raise ValueError('limit_price is required for limit orders')
            if self._limit_is_executable(order, market_price):
                fill_price = self._apply_slippage(
                    min(order.limit_price, market_price) if side == 'buy' else max(order.limit_price, market_price),
                    side,
                )
                fill_record = self._record_fill(order, order_id=order_id, fill_price=fill_price)
                return {
                    'order_id': order_id,
                    'status': 'filled',
                    'fill': fill_record,
                }

            open_order = {
                'order_id': order_id,
                'symbol': order.symbol,
                'side': side,
                'quantity': float(order.quantity),
                'order_type': order_type,
                'asset_class': asset_class,
                'time_in_force': order.time_in_force,
                'limit_price': float(order.limit_price),
                'status': 'open',
                'created_at': self._timestamp(),
                'metadata': dict(order.metadata or {}),
            }
            self.state.open_orders.append(open_order)
            self._touch_state()
            return open_order

        raise ValueError(f'Unsupported paper order type: {order_type}')

    def cancel_order(self, order_id: str) -> Any:
        remaining_orders = []
        canceled_order: dict[str, Any] | None = None
        for order in self.state.open_orders:
            if str(order.get('order_id')) == str(order_id):
                canceled_order = dict(order)
                canceled_order['status'] = 'canceled'
                canceled_order['canceled_at'] = self._timestamp()
                continue
            remaining_orders.append(order)
        self.state.open_orders = remaining_orders
        self._touch_state()
        return canceled_order or {'order_id': order_id, 'status': 'not_found'}

    def get_positions(self) -> list[BrokerPositionBoi]:
        positions: list[BrokerPositionBoi] = []
        for payload in self.state.positions:
            positions.append(
                BrokerPositionBoi(
                    symbol=str(payload.get('symbol', '')),
                    quantity=float(payload.get('quantity', 0.0)),
                    average_entry_price=self._safe_float(payload.get('average_entry_price')),
                    mark_price=self._safe_float(payload.get('mark_price')),
                    asset_class=str(payload.get('asset_class', 'stock')),
                    metadata=dict(payload.get('metadata', {})),
                )
            )
        return positions

    def get_fills(self) -> list[BrokerFillBoi]:
        fills: list[BrokerFillBoi] = []
        for payload in self.state.fills:
            fills.append(
                BrokerFillBoi(
                    order_id=str(payload.get('order_id', '')),
                    symbol=str(payload.get('symbol', '')),
                    side=str(payload.get('side', '')),
                    quantity=float(payload.get('quantity', 0.0)),
                    price=self._safe_float(payload.get('price')),
                    fee=self._safe_float(payload.get('fee')),
                    status=str(payload.get('status', 'filled')),
                    asset_class=str(payload.get('asset_class', 'stock')),
                    metadata=dict(payload.get('metadata', {})),
                )
            )
        return fills

    def get_account_snapshot(self) -> AccountSnapshotBoi:
        positions = self.get_positions()
        equity_value = self.state.cash_balance
        for position in positions:
            mark_price = position.mark_price
            if mark_price is None:
                mark_price = self.state.last_prices.get(position.symbol)
            if mark_price is None:
                mark_price = position.average_entry_price or 0.0
            equity_value += float(position.quantity) * float(mark_price)

        buying_power = self.state.cash_balance if not self.allow_margin else max(self.state.cash_balance * 2.0, self.state.cash_balance)
        return AccountSnapshotBoi(
            broker=self.broker_name,
            cash_balance=self.state.cash_balance,
            equity_value=equity_value,
            buying_power=buying_power,
            positions=positions,
            metadata={
                'state_path': str(self.state_path),
                'starting_cash': self.starting_cash,
                'fee_rate': self.fee_rate,
                'slippage_bps': self.slippage_bps,
                'allow_short': self.allow_short,
                'allow_margin': self.allow_margin,
                'next_order_id': self.state.next_order_id,
                'next_fill_id': self.state.next_fill_id,
                'last_updated_at': self.state.last_updated_at,
            },
        )

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is None or value == '':
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
