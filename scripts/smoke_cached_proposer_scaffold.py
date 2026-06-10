#!/usr/bin/env python3
"""Deterministic smoke for the cached proposer scaffold (Slice C).

Uses only the fake in-memory client: no Anthropic/OpenAI/Gemini/Poke calls,
no API keys, no environment variables, no broker calls, no scheduler wiring.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "proposal" / "cached_proposer.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.bridge_contract import RiskSummary, RunMetadata


def _load_module():
    spec = importlib.util.spec_from_file_location("cached_proposer", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cached_proposer"] = module
    spec.loader.exec_module(module)
    return module


CANDIDATE = {
    "event_id": "smoke-candidate-0001",
    "symbol": "AAPL",
    "side": "buy",
    "confidence": 0.8,
}

VALID_INTENT_JSON = json.dumps(
    {
        "intent_id": "intent-smoke",
        "signal_id": "sig-smoke",
        "broker": "alpaca",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 10.0,
        "created_at": "2026-06-10T00:00:00Z",
        "dry_run": True,
        "approved": False,
    }
)

SAFE_RISK = RiskSummary(
    risk_id="risk-smoke",
    signal_id="sig-smoke",
    risk_score=0.1,
    max_position_size=10.0,
)
SAFE_RUN = RunMetadata(
    run_id="run-smoke",
    source_system="cached-proposer-smoke",
    created_at="2026-06-10T00:00:00Z",
)


def main() -> None:
    mod = _load_module()
    FakeClient = mod.FakeProposalClient
    Proposer = mod.CachedProposerScaffold

    # Fake valid model output parses, validates, and records cache metadata.
    client = FakeClient(
        response_text=VALID_INTENT_JSON,
        cache_read_tokens=1024,
        cache_write_tokens=0,
    )
    proposer = Proposer(client)
    result = proposer.propose(CANDIDATE, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is True, result.reasons
    assert result.needs_human is False
    assert result.intent is not None
    assert result.intent.dry_run is True and result.intent.approved is False
    assert result.cache_metadata["model"] == "fake-local-model"
    assert result.cache_metadata["provider"] == "fake-local"
    assert result.cache_metadata["cache_read_tokens"] == 1024
    assert result.cache_metadata["static_prefix_chars"] > 0

    # The prompt is split into static cached prefix + variable candidate suffix.
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["static_prefix"] == mod.DEFAULT_STATIC_PREFIX
    assert call["candidate_suffix"].startswith("CANDIDATE:")
    assert "smoke-candidate-0001" in call["candidate_suffix"]

    # Fake invalid JSON fails closed.
    bad_json = Proposer(FakeClient(response_text="this is not json {"))
    result = bad_json.propose(CANDIDATE, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is False and result.intent is None
    assert any("not valid JSON" in r for r in result.reasons), result.reasons

    # JSON array (not an object) fails closed.
    array_json = Proposer(FakeClient(response_text="[1, 2, 3]"))
    result = array_json.propose(CANDIDATE, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is False
    assert any("JSON object" in r for r in result.reasons), result.reasons

    # Valid JSON with unexpected contract fields fails closed.
    extra_fields = Proposer(
        FakeClient(response_text='{"intent_id": "x", "place_real_order_now": true}')
    )
    result = extra_fields.propose(CANDIDATE, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is False
    assert any("does not match ExecutionIntent contract" in r for r in result.reasons)

    # Valid JSON but unsafe ExecutionIntent (bad side / zero quantity) fails validation.
    unsafe = json.loads(VALID_INTENT_JSON)
    unsafe["side"] = "hold"
    unsafe["quantity"] = 0
    unsafe_proposer = Proposer(FakeClient(response_text=json.dumps(unsafe)))
    result = unsafe_proposer.propose(CANDIDATE, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is False and result.intent is None
    assert any("invalid side" in r for r in result.reasons), result.reasons
    assert any("quantity must be positive" in r for r in result.reasons), result.reasons

    # Fake non-dry-run unapproved intent fails validation.
    live_unapproved = json.loads(VALID_INTENT_JSON)
    live_unapproved["dry_run"] = False
    live_unapproved["approved"] = False
    live_proposer = Proposer(FakeClient(response_text=json.dumps(live_unapproved)))
    result = live_proposer.propose(CANDIDATE, risk=SAFE_RISK, run=SAFE_RUN)
    assert result.allowed is False and result.intent is None
    assert any("dry_run=False requires approved=True" in r for r in result.reasons)

    # risk.needs_human blocks.
    risk_human = RiskSummary(
        risk_id="risk-human",
        signal_id="sig-smoke",
        risk_score=0.9,
        max_position_size=1.0,
        needs_human=True,
        needs_human_reason="manual review",
    )
    result = Proposer(FakeClient(response_text=VALID_INTENT_JSON)).propose(
        CANDIDATE, risk=risk_human, run=SAFE_RUN
    )
    assert result.allowed is False and result.needs_human is True
    assert result.status == "needs_human"

    # run.needs_human blocks.
    run_human = RunMetadata(
        run_id="run-human",
        source_system="cached-proposer-smoke",
        created_at="2026-06-10T00:00:00Z",
        needs_human=True,
        needs_human_reason="halt requested",
    )
    result = Proposer(FakeClient(response_text=VALID_INTENT_JSON)).propose(
        CANDIDATE, risk=SAFE_RISK, run=run_human
    )
    assert result.allowed is False and result.needs_human is True

    # Malformed candidate payload fails closed without calling the client.
    silent_client = FakeClient(response_text=VALID_INTENT_JSON)
    result = Proposer(silent_client).propose("not-a-dict")  # type: ignore[arg-type]
    assert result.allowed is False and result.needs_human is True
    assert silent_client.calls == [], "client must not be called for malformed candidate"

    # Non-local clients are rejected at construction (no network promotion).
    class NotLocalClient:
        is_local = False

        def complete(self, *, static_prefix: str, candidate_suffix: str):
            raise AssertionError("must never be called")

    try:
        Proposer(NotLocalClient())
    except ValueError as exc:
        assert "is_local" in str(exc)
    else:
        raise AssertionError("non-local client must be rejected")

    # Module must not import provider SDKs or networking machinery.
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "import anthropic",
        "import openai",
        "import google",
        "import requests",
        "import httpx",
        "import urllib",
        "import socket",
        "os.environ",
        "from autohedge.brokers",
    ):
        assert forbidden not in source, f"forbidden reference {forbidden!r} in cached proposer"

    print("cached proposer scaffold smoke: ok")


if __name__ == "__main__":
    main()
