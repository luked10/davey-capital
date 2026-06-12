"""Cached proposer scaffold (Tier 2 shape, fake client by default).

Models the prompt-caching structure planned for rare Sonnet/Fable structured
proposal calls: a static cacheable prefix (system doctrine + schema) plus a
small variable candidate suffix. The default fake client performs NO network
calls, requires NO API keys or environment variables, and never touches brokers
or the scheduler. Networked proposal clients are allowed only when callers
explicitly instantiate and pass one in; every output still passes through the
same fail-closed validation boundary before being surfaced.

All model output passes through ``validate_execution_intent`` before being
surfaced; unsafe or unapproved intents fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

from contracts.bridge_contract import (
    ExecutionIntent,
    RiskSummary,
    RunMetadata,
    execution_intent_from_dict,
    validate_execution_intent,
)


CACHED_PROPOSER_SCAFFOLD_VERSION = "0.1.0"

# Static prefix concept: stable doctrine/schema text that a future provider
# integration would mark as a cached prompt prefix. Deterministic by design.
DEFAULT_STATIC_PREFIX = (
    "You are the Davey Capital Tier 2 proposal model.\n"
    "Given one candidate event, emit exactly one JSON object matching the\n"
    "ExecutionIntent contract. Hard rules: dry_run must be true, approved\n"
    "must be false, never invent approval fields, never propose live\n"
    "execution. Output JSON only, no prose.\n"
)


class ProposalClient(Protocol):
    """Interface a proposal client must satisfy."""

    is_local: bool

    def complete(self, *, static_prefix: str, candidate_suffix: str) -> Any:
        ...


@dataclass(slots=True)
class FakeProposalResponse:
    """Shape of a model response, including cache accounting fields."""

    text: str
    model: str = "fake-local-model"
    provider: str = "fake-local"
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class FakeProposalClient:
    """Deterministic in-memory client for tests. Never touches the network."""

    response_text: str = ""
    model: str = "fake-local-model"
    provider: str = "fake-local"
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    is_local: bool = True
    calls: list[dict[str, str]] = field(default_factory=list)

    def complete(self, *, static_prefix: str, candidate_suffix: str) -> FakeProposalResponse:
        self.calls.append(
            {"static_prefix": static_prefix, "candidate_suffix": candidate_suffix}
        )
        return FakeProposalResponse(
            text=self.response_text,
            model=self.model,
            provider=self.provider,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            input_tokens=len(static_prefix) + len(candidate_suffix),
            output_tokens=len(self.response_text),
        )


@dataclass(slots=True)
class ProposalResult:
    allowed: bool
    needs_human: bool
    status: str
    reasons: tuple[str, ...] = ()
    intent: ExecutionIntent | None = None
    cache_metadata: dict[str, Any] = field(default_factory=dict)


def build_candidate_suffix(candidate_payload: dict[str, Any]) -> str:
    """Render the small variable suffix appended after the cached prefix."""
    return "CANDIDATE:\n" + json.dumps(
        candidate_payload, ensure_ascii=False, sort_keys=True
    )


def _parse_intent_json(raw_text: str, reasons: list[str]) -> ExecutionIntent | None:
    try:
        payload = json.loads(raw_text)
    except (TypeError, ValueError) as exc:
        reasons.append(f"model output is not valid JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        reasons.append("model output must be a JSON object")
        return None
    try:
        return execution_intent_from_dict(payload)
    except TypeError as exc:
        reasons.append(f"model output does not match ExecutionIntent contract: {exc}")
        return None


class CachedProposerScaffold:
    """Tier 2 proposal scaffold with a pluggable client.

    Fake clients keep the offline smokes deterministic. Real clients may be
    passed in explicitly, but their outputs still cannot bypass
    ``validate_execution_intent``.
    """

    def __init__(
        self,
        client: ProposalClient,
        *,
        static_prefix: str = DEFAULT_STATIC_PREFIX,
    ) -> None:
        self._client = client
        self.static_prefix = static_prefix

    def propose(
        self,
        candidate_payload: dict[str, Any],
        *,
        risk: RiskSummary | None = None,
        run: RunMetadata | None = None,
    ) -> ProposalResult:
        reasons: list[str] = []

        if not isinstance(candidate_payload, dict):
            return ProposalResult(
                allowed=False,
                needs_human=True,
                status="needs_human",
                reasons=("candidate payload must be a dict",),
            )

        response = self._client.complete(
            static_prefix=self.static_prefix,
            candidate_suffix=build_candidate_suffix(candidate_payload),
        )
        cache_metadata = {
            "model": response.model,
            "provider": response.provider,
            "cache_read_tokens": response.cache_read_tokens,
            "cache_write_tokens": response.cache_write_tokens,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "static_prefix_chars": len(self.static_prefix),
        }

        intent = _parse_intent_json(response.text, reasons)
        if intent is None:
            return ProposalResult(
                allowed=False,
                needs_human=False,
                status="blocked",
                reasons=tuple(reasons),
                cache_metadata=cache_metadata,
            )

        validation = validate_execution_intent(intent, risk=risk, run=run)
        return ProposalResult(
            allowed=validation.allowed,
            needs_human=validation.needs_human,
            status=validation.status,
            reasons=validation.reasons,
            intent=validation.normalized_intent,
            cache_metadata=cache_metadata,
        )
