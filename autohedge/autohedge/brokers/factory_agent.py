from typing import Any

from autohedge.brokers.base_agent import BrokerBoi
from autohedge.brokers.paper_agent import PaperBrokerBoi
from autohedge.brokers.robinhood_agent import RobinhoodBrokerBoi
from autohedge.brokers.solana_agent import SolanaBrokerBoi

BROKER_AGENT_REGISTRY: dict[str, type[BrokerBoi]] = {
    'paper': PaperBrokerBoi,
    'robinhood': RobinhoodBrokerBoi,
    'solana': SolanaBrokerBoi,
}


def get_broker_agent(name: str, **kwargs: Any) -> BrokerBoi:
    key = name.strip().lower()
    try:
        broker_cls = BROKER_AGENT_REGISTRY[key]
    except KeyError as exc:
        available = ', '.join(sorted(BROKER_AGENT_REGISTRY))
        raise ValueError(f'Unknown broker agent: {name}. Available: {available}') from exc
    return broker_cls(**kwargs)
