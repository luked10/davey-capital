from typing import Any

from autohedge.brokers.base_boi import (
    AccountSnapshotBoi,
    BrokerBoi,
    BrokerFillBoi,
    BrokerOrderBoi,
    BrokerPositionBoi,
)


class SolanaBrokerBoi(BrokerBoi):
    broker_name = 'solana'

    def place_order(self, order: BrokerOrderBoi) -> Any:
        raise NotImplementedError('SolanaBrokerBoi is a scaffold. Wire the Solana adapter here.')

    def cancel_order(self, order_id: str) -> Any:
        raise NotImplementedError('SolanaBrokerBoi is a scaffold. Wire cancel_order here.')

    def get_positions(self) -> list[BrokerPositionBoi]:
        raise NotImplementedError('SolanaBrokerBoi is a scaffold. Wire get_positions here.')

    def get_fills(self) -> list[BrokerFillBoi]:
        raise NotImplementedError('SolanaBrokerBoi is a scaffold. Wire get_fills here.')

    def get_account_snapshot(self) -> AccountSnapshotBoi:
        raise NotImplementedError('SolanaBrokerBoi is a scaffold. Wire get_account_snapshot here.')
