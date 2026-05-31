from typing import Any

from autohedge.brokers.base_agent import (
    AccountSnapshotAgent,
    BrokerAgent,
    BrokerFillAgent,
    BrokerOrderAgent,
    BrokerPositionAgent,
)


class SolanaBrokerAgent(BrokerAgent):
    broker_name = 'solana'

    def place_order(self, order: BrokerOrderAgent) -> Any:
        raise NotImplementedError('SolanaBrokerAgent is a scaffold. Wire the Solana adapter here.')

    def cancel_order(self, order_id: str) -> Any:
        raise NotImplementedError('SolanaBrokerAgent is a scaffold. Wire cancel_order here.')

    def get_positions(self) -> list[BrokerPositionAgent]:
        raise NotImplementedError('SolanaBrokerAgent is a scaffold. Wire get_positions here.')

    def get_fills(self) -> list[BrokerFillAgent]:
        raise NotImplementedError('SolanaBrokerAgent is a scaffold. Wire get_fills here.')

    def get_account_snapshot(self) -> AccountSnapshotAgent:
        raise NotImplementedError('SolanaBrokerAgent is a scaffold. Wire get_account_snapshot here.')


# Backwards-compatible alias.
SolanaBrokerBoi = SolanaBrokerAgent
