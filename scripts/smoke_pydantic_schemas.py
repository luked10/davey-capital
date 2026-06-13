#!/usr/bin/env python3
"""Offline smoke for strict Pydantic agent handoff schemas."""

from __future__ import annotations

from pathlib import Path
import sys

from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOHEDGE_ROOT = REPO_ROOT / "autohedge"
if str(AUTOHEDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOHEDGE_ROOT))

from autohedge.schemas.models import CandidateSignal, ProposalResult, TriageDecision


def _assert_validation_error(model_cls: type, payload: dict) -> None:
    try:
        model_cls.model_validate(payload)
    except ValidationError:
        return
    raise AssertionError(f"{model_cls.__name__} accepted invalid payload: {payload}")


def check_candidate_signal() -> None:
    valid = CandidateSignal.model_validate(
        {
            "handoff_id": "handoff-0001",
            "symbol": "nvda",
            "side": "buy",
            "confidence": 0.85,
            "created_at": "2026-06-13T00:00:00Z",
            "dry_run": True,
            "metadata": {"source": "smoke"},
        }
    )
    assert valid.symbol == "NVDA"
    assert valid.confidence == 0.85
    assert valid.dry_run is True

    _assert_validation_error(
        CandidateSignal,
        {
            "handoff_id": "handoff-0002",
            "symbol": "NVDA",
            "side": "hold",
            "confidence": 0.5,
            "created_at": "2026-06-13T00:00:00Z",
        },
    )
    _assert_validation_error(
        CandidateSignal,
        {
            "handoff_id": "handoff-0003",
            "symbol": "NVDA",
            "side": "buy",
            "confidence": 1.01,
            "created_at": "2026-06-13T00:00:00Z",
        },
    )
    _assert_validation_error(
        CandidateSignal,
        {
            "handoff_id": "handoff-0004",
            "symbol": "NVDA",
            "side": "buy",
            "confidence": 0.5,
            "created_at": "2026-06-13T00:00:00Z",
            "dry_run": "true",
        },
    )


def check_triage_decision() -> None:
    valid = TriageDecision.model_validate(
        {
            "handoff_id": "handoff-0001",
            "proceed": False,
            "reason": "skip low conviction",
            "decided_at": "2026-06-13T00:01:00Z",
        }
    )
    assert valid.proceed is False
    assert valid.reason == "skip low conviction"

    _assert_validation_error(
        TriageDecision,
        {
            "handoff_id": "handoff-0001",
            "proceed": "false",
            "reason": "skip",
            "decided_at": "2026-06-13T00:01:00Z",
        },
    )
    _assert_validation_error(
        TriageDecision,
        {
            "handoff_id": "handoff-0001",
            "proceed": False,
            "reason": " ",
            "decided_at": "2026-06-13T00:01:00Z",
        },
    )


def check_proposal_result() -> None:
    valid = ProposalResult.model_validate(
        {
            "handoff_id": "handoff-0001",
            "intent": {"intent_id": "intent-0001", "dry_run": True},
            "needs_human": True,
            "rationale": "human approval required",
            "token_meta": {"input_tokens": 10},
        }
    )
    assert valid.intent["intent_id"] == "intent-0001"
    assert valid.needs_human is True

    _assert_validation_error(
        ProposalResult,
        {
            "handoff_id": "handoff-0001",
            "intent": None,
            "needs_human": True,
            "rationale": "missing intent",
        },
    )
    _assert_validation_error(
        ProposalResult,
        {
            "handoff_id": "handoff-0001",
            "intent": {},
            "needs_human": "true",
            "rationale": "bad bool",
        },
    )
    _assert_validation_error(
        ProposalResult,
        {
            "handoff_id": "handoff-0001",
            "intent": {},
            "needs_human": True,
            "rationale": "",
            "extra": "forbidden",
        },
    )


def main() -> None:
    check_candidate_signal()
    check_triage_decision()
    check_proposal_result()
    print("pydantic schemas smoke: ok")


if __name__ == "__main__":
    main()
