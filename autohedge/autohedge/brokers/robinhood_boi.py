from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autohedge.brokers.base_boi import (
    AccountSnapshotBoi,
    BrokerBoi,
    BrokerFillBoi,
    BrokerOrderBoi,
    BrokerPositionBoi,
)
from autohedge.brokers.robinhood_state_boi import (
    RobinhoodStateBoi,
    RobinhoodStateStoreBoi,
)



def _load_dotenv_if_available() -> None:
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


class RobinhoodBrokerBoi(BrokerBoi):
    broker_name = 'robinhood'

    def __init__(
        self,
        *,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        _load_dotenv_if_available()
        super().__init__(session_id=session_id, config=config)
        self.username = self._read_setting('ROBINHOOD_USERNAME')
        self.password = self._read_setting('ROBINHOOD_PASSWORD')
        self.mfa_code = self._read_setting('ROBINHOOD_MFA_CODE')
        self.device_token = self._read_setting('ROBINHOOD_DEVICE_TOKEN')
        self.client_id = self._read_setting('ROBINHOOD_CLIENT_ID')
        self.session_pickle_path = Path(
            self._read_setting(
                'ROBINHOOD_SESSION_PICKLE_PATH',
                '.autohedge/robinhood_session_boi.pkl',
            )
        )
        self.state_path = Path(
            self._read_setting(
                'ROBINHOOD_STATE_PATH',
                '.autohedge/robinhood_state_boi.json',
            )
        )
        self.state_store = RobinhoodStateStoreBoi(self.state_path)
        self.state = self.state_store.load()
        self.rh = self._import_robinhood_client()
        self.auth_payload: dict[str, Any] = {}
        self._login()

    def _read_setting(self, key: str, default: str = '') -> str:
        value = self.config.get(key.lower()) if self.config else None
        if value:
            return str(value)
        value = self.config.get(key) if self.config else None
        if value:
            return str(value)
        return os.getenv(key, default)

    def _import_robinhood_client(self):
        try:
            import robin_stocks.robinhood as rh  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                'Robinhood support requires the robin-stocks package.'
            ) from exc
        return rh

    def _login(self) -> None:
        if not self.username or not self.password:
            raise ValueError(
                'ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD are required.'
            )

        login_kwargs: dict[str, Any] = {
            'username': self.username,
            'password': self.password,
            'store_session': True,
            'pickle_name': str(self.session_pickle_path),
        }
        if self.mfa_code:
            login_kwargs['mfa_code'] = self.mfa_code
        if self.device_token:
            login_kwargs['device_token'] = self.device_token
        if self.client_id:
            login_kwargs['client_id'] = self.client_id

        try:
            self.auth_payload = self.rh.login(**login_kwargs)
        except TypeError:
            fallback_kwargs = dict(login_kwargs)
            fallback_kwargs.pop('pickle_name', None)
            fallback_kwargs.pop('client_id', None)
            fallback_kwargs.pop('device_token', None)
            self.auth_payload = self.rh.login(**fallback_kwargs)

        self.state = RobinhoodStateBoi(
            username=self.username,
            session_pickle_path=str(self.session_pickle_path),
            state_path=str(self.state_path),
            last_login_at=datetime.now(UTC).isoformat(),
            stock_account_id=self._discover_stock_account_id(),
            crypto_account_id=self._discover_crypto_account_id(),
            asset_classes=['stock', 'crypto'],
            metadata={'auth_keys': sorted(self.auth_payload.keys())},
        )
        self.state_store.save(self.state)

    def _call_first(self, names: list[str], *args: Any, **kwargs: Any) -> Any:
        for name in names:
            target: Any = self.rh
            for part in name.split('.'):
                target = getattr(target, part, None)
                if target is None:
                    break
            if callable(target):
                return target(*args, **kwargs)
        raise AttributeError(f'No Robinhood method found for {names}')

    def _call_first_optional(
        self,
        names: list[str],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            return self._call_first(names, *args, **kwargs)
        except AttributeError:
            return None

    def _discover_stock_account_id(self) -> str | None:
        profile = self._call_first_optional(
            ['load_account_profile', 'account.load_account_profile']
        )
        if isinstance(profile, dict):
            return profile.get('account_number') or profile.get('id')
        return None

    def _discover_crypto_account_id(self) -> str | None:
        profile = self._call_first_optional(
            ['load_crypto_profile', 'crypto.load_crypto_profile']
        )
        if isinstance(profile, dict):
            return profile.get('account_number') or profile.get('id')
        return None

    def place_order(self, order: BrokerOrderBoi) -> Any:
        asset_class = (order.asset_class or 'stock').strip().lower()
        side = order.side.strip().lower()
        order_type = order.order_type.strip().lower()

        if asset_class == 'crypto':
            return self._place_crypto_order(order, side, order_type)
        return self._place_stock_order(order, side, order_type)

    def _place_stock_order(
        self,
        order: BrokerOrderBoi,
        side: str,
        order_type: str,
    ) -> Any:
        if order_type == 'market':
            method = (
                'order_buy_market' if side == 'buy' else 'order_sell_market'
            )
            return self._call_first(
                [method, f'orders.{method}'],
                order.symbol,
                order.quantity,
                timeInForce=order.time_in_force,
            )
        if order_type == 'limit':
            if order.limit_price is None:
                raise ValueError('limit_price is required for limit orders')
            method = (
                'order_buy_limit' if side == 'buy' else 'order_sell_limit'
            )
            return self._call_first(
                [method, f'orders.{method}'],
                order.symbol,
                order.quantity,
                order.limit_price,
                timeInForce=order.time_in_force,
            )
        raise ValueError(f'Unsupported stock order type: {order_type}')

    def _place_crypto_order(
        self,
        order: BrokerOrderBoi,
        side: str,
        order_type: str,
    ) -> Any:
        if order_type == 'market':
            method = (
                'order_buy_crypto_by_quantity'
                if side == 'buy'
                else 'order_sell_crypto_by_quantity'
            )
            return self._call_first(
                [method, f'crypto.{method}'],
                order.symbol,
                order.quantity,
            )
        if order_type == 'limit':
            if order.limit_price is None:
                raise ValueError('limit_price is required for limit orders')
            method = (
                'order_buy_crypto_limit'
                if side == 'buy'
                else 'order_sell_crypto_limit'
            )
            return self._call_first(
                [method, f'crypto.{method}'],
                order.symbol,
                order.quantity,
                order.limit_price,
            )
        raise ValueError(f'Unsupported crypto order type: {order_type}')

    def cancel_order(self, order_id: str) -> Any:
        return self._call_first(
            ['cancel_stock_order', 'orders.cancel_stock_order'], order_id
        )

    def get_positions(self) -> list[BrokerPositionBoi]:
        positions: list[BrokerPositionBoi] = []

        holdings = self._call_first_optional(
            ['build_holdings', 'account.build_holdings']
        )
        if isinstance(holdings, dict):
            for symbol, payload in holdings.items():
                quantity = float(
                    payload.get('quantity')
                    or payload.get('shares')
                    or 0.0
                )
                avg = payload.get('average_buy_price')
                market = payload.get('equity')
                positions.append(
                    BrokerPositionBoi(
                        symbol=symbol,
                        quantity=quantity,
                        average_entry_price=float(avg) if avg else None,
                        mark_price=float(market) if market else None,
                        asset_class='stock',
                        metadata=dict(payload),
                    )
                )

        crypto_positions = self._call_first_optional(
            ['get_crypto_positions', 'crypto.get_crypto_positions']
        )
        if isinstance(crypto_positions, list):
            for payload in crypto_positions:
                symbol = (
                    payload.get('currency', {}).get('code')
                    or payload.get('asset_code')
                    or payload.get('symbol')
                    or 'crypto'
                )
                quantity = float(
                    payload.get('quantity')
                    or payload.get('available_quantity')
                    or 0.0
                )
                avg = payload.get('average_buy_price')
                positions.append(
                    BrokerPositionBoi(
                        symbol=symbol,
                        quantity=quantity,
                        average_entry_price=float(avg) if avg else None,
                        asset_class='crypto',
                        metadata=dict(payload),
                    )
                )

        return positions

    def get_fills(self) -> list[BrokerFillBoi]:
        fills: list[BrokerFillBoi] = []
        stock_orders = self._call_first_optional(
            ['get_all_stock_orders', 'orders.get_all_stock_orders']
        )
        if isinstance(stock_orders, list):
            for payload in stock_orders:
                fills.append(self._order_to_fill(payload, 'stock'))

        crypto_orders = self._call_first_optional(
            ['get_all_crypto_orders', 'crypto.get_all_crypto_orders']
        )
        if isinstance(crypto_orders, list):
            for payload in crypto_orders:
                fills.append(self._order_to_fill(payload, 'crypto'))

        return fills

    def _order_to_fill(
        self,
        payload: dict[str, Any],
        asset_class: str,
    ) -> BrokerFillBoi:
        return BrokerFillBoi(
            order_id=str(payload.get('id') or payload.get('url') or ''),
            symbol=str(
                payload.get('symbol') or payload.get('currency_code') or ''
            ),
            side=str(payload.get('side') or payload.get('direction') or ''),
            quantity=float(
                payload.get('quantity')
                or payload.get('cumulative_quantity')
                or 0.0
            ),
            price=(
                float(payload['average_price'])
                if payload.get('average_price')
                else None
            ),
            fee=(float(payload['fee']) if payload.get('fee') else None),
            status=str(payload.get('state') or payload.get('status') or 'open'),
            asset_class=asset_class,
            metadata=dict(payload),
        )

    def get_account_snapshot(self) -> AccountSnapshotBoi:
        cash_balance = None
        equity_value = None
        buying_power = None

        account_profile = self._call_first_optional(
            ['load_account_profile', 'profiles.load_account_profile', 'account.load_account_profile']
        )
        if isinstance(account_profile, dict):
            buying_power = self._safe_float(
                account_profile.get('buying_power')
                or account_profile.get('day_trade_buying_power')
                or account_profile.get('cash_held_for_orders')
            )
            cash_balance = self._safe_float(
                account_profile.get('cash_available_for_withdrawal')
                or account_profile.get('cash')
                or account_profile.get('available_balance')
                or account_profile.get('unallocated_margin_cash')
            )

        portfolio_profile = self._call_first_optional(
            ['load_portfolio_profile', 'profiles.load_portfolio_profile', 'account.load_portfolio_profile']
        )
        if isinstance(portfolio_profile, dict):
            equity_value = self._safe_float(
                portfolio_profile.get('equity')
                or portfolio_profile.get('market_value')
                or portfolio_profile.get('portfolio_equity')
            )
            if cash_balance is None:
                cash_balance = self._safe_float(
                    portfolio_profile.get('cash')
                    or portfolio_profile.get('extended_hours_equity')
                )

        return AccountSnapshotBoi(
            broker=self.broker_name,
            cash_balance=cash_balance,
            equity_value=equity_value,
            buying_power=buying_power,
            positions=self.get_positions(),
            metadata={
                'stock_account_id': self.state.stock_account_id,
                'crypto_account_id': self.state.crypto_account_id,
                'state_path': str(self.state_path),
                'session_pickle_path': str(self.session_pickle_path),
                'username': self.username,
            },
        )

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is None or value == '':
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
