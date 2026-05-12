from typing import Any

from autohedge.brokers.base_boi import BrokerBoi
from autohedge.brokers.paper_boi import PaperBrokerBoi
from autohedge.brokers.robinhood_boi import RobinhoodBrokerBoi
from autohedge.brokers.solana_boi import SolanaBrokerBoi

BROKER_BOI_REGISTRY: dict[str, type[BrokerBoi]] = {
    'paper': PaperBrokerBoi,
    'robinhood': RobinhoodBrokerBoi,
    'solana': SolanaBrokerBoi,
}


def get_broker_boi(name: str, **kwargs: Any) -> BrokerBoi:
    key = name.strip().lower()
    try:
        broker_cls = BROKER_BOI_REGISTRY[key]
    except KeyError as exc:
        available = ', '.join(sorted(BROKER_BOI_REGISTRY))
        raise ValueError(f'Unknown broker boi: {name}. Available: {available}') from exc
    return broker_cls(**kwargs)
