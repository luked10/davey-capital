"""Proposal package.

Offline tests use the fake cached proposer client. The Sonnet client is live
only when explicitly instantiated by a caller such as the gated live smoke.
"""

from autohedge.proposal.cached_proposer import (
    CachedProposerScaffold,
    FakeProposalClient,
    ProposalResult,
)
from autohedge.proposal.sonnet_client import (
    SonnetProposalClient,
    SonnetProposalResult,
)

__all__ = [
    "CachedProposerScaffold",
    "FakeProposalClient",
    "ProposalResult",
    "SonnetProposalClient",
    "SonnetProposalResult",
]
