from typing import Any

from autohedge.brokers.base_agent import BrokerAgent
from autohedge.brokers.paper_agent import PaperBrokerAgent
from autohedge.brokers.robinhood_agent import RobinhoodBrokerAgent
from autohedge.brokers.solana_agent import SolanaBrokerAgent

BROKER_AGENT_REGISTRY: dict[str, type[BrokerAgent]] = {
    'paper': PaperBrokerAgent,
    'robinhood': RobinhoodBrokerAgent,
    'solana': SolanaBrokerAgent,
}


def get_broker_agent(name: str, **kwargs: Any) -> BrokerAgent:
    key = name.strip().lower()
    try:
        broker_cls = BROKER_AGENT_REGISTRY[key]
    except KeyError as exc:
        available = ', '.join(sorted(BROKER_AGENT_REGISTRY))
        raise ValueError(f'Unknown broker agent: {name}. Available: {available}') from exc
    return broker_cls(**kwargs)
