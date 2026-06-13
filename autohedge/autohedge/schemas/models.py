"""Pydantic models for strict local agent handoffs.

These models validate the data shape before triage/proposal payloads can move
closer to execution. They do not perform network or broker operations.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class _StrictHandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("handoff_id", "created_at", check_fields=False)
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class CandidateSignal(_StrictHandoffModel):
    handoff_id: str
    symbol: str
    side: Literal["buy", "sell"]
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: str
    dry_run: StrictBool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _uppercase_symbol(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class TriageDecision(_StrictHandoffModel):
    handoff_id: str
    proceed: StrictBool
    reason: str
    decided_at: str

    @field_validator("reason")
    @classmethod
    def _nonempty_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class ProposalResult(_StrictHandoffModel):
    handoff_id: str
    intent: dict[str, Any]
    needs_human: StrictBool
    rationale: str
    token_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rationale")
    @classmethod
    def _nonempty_rationale(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned
