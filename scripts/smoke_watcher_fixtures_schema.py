#!/usr/bin/env python3
"""Deterministic smoke for watcher payload schema hardening (Slice A).

Validates candidate, NEEDS_HUMAN, and local poke queue payload schemas with
fixed fixtures. Asserts valid payloads pass, malformed payloads fail closed,
and the poke destination stays pinned to the local queue. No network calls,
no broker calls, no credentials.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.overnight_scaffold import (
    LOCAL_POKE_DESTINATION,
    validate_candidate_event_payload,
    validate_needs_human_event_payload,
    validate_poke_handoff_payload,
)


# ---------------------------------------------------------------------------
# Deterministic fixtures.
# ---------------------------------------------------------------------------

VALID_CANDIDATE = {
    "event_id": "fixture-candidate-0001",
    "run_id": "fixture-run",
    "created_at": "2026-06-10T00:00:00Z",
    "symbol": "aapl",
    "side": "BUY",
    "confidence": 0.75,
    "source": "tier0_watcher_scaffold",
    "strategy": "fixture-strategy",
    "dry_run": True,
    "metadata": {"note": "fixture"},
}

CANDIDATE_MISSING_FIELDS = {
    "event_id": "fixture-candidate-0002",
    "run_id": "fixture-run",
    # created_at, symbol, side, confidence all missing
}

CANDIDATE_MALFORMED_TYPES = {
    "event_id": "fixture-candidate-0003",
    "run_id": "fixture-run",
    "created_at": "2026-06-10T00:00:00Z",
    "symbol": "AAPL",
    "side": "hold",
    "confidence": "not-a-number",
    "dry_run": "true",
    "metadata": ["not", "a", "dict"],
}

VALID_NEEDS_HUMAN = {
    "needs_human_id": "fixture-needs-human-0001",
    "run_id": "fixture-run",
    "created_at": "2026-06-10T00:00:00Z",
    "reason_code": "CANDIDATE_VALIDATION_ERROR",
    "reason": "fixture reason",
    "source_event_id": "fixture-candidate-0002",
    "dry_run": True,
    "metadata": {"payload": {}},
}

NEEDS_HUMAN_MISSING_REASON_CONTEXT = {
    "needs_human_id": "fixture-needs-human-0002",
    "run_id": "fixture-run",
    "created_at": "2026-06-10T00:00:00Z",
    # reason_code and reason missing
}

VALID_POKE_HANDOFF = {
    "handoff_id": "fixture-handoff-0001",
    "run_id": "fixture-run",
    "created_at": "2026-06-10T00:00:00Z",
    "candidate_event_id": "fixture-candidate-0001",
    "destination": LOCAL_POKE_DESTINATION,
    "dry_run": True,
    "metadata": {"symbol": "AAPL", "side": "buy", "confidence": 0.75},
}

MALFORMED_POKE_HANDOFF = {
    "handoff_id": "",
    "run_id": "fixture-run",
    "created_at": "2026-06-10T00:00:00Z",
    "candidate_event_id": "fixture-candidate-0001",
    "destination": "external_sms_bridge",
    "dry_run": 1,
}


def check_candidate_fixtures() -> None:
    result = validate_candidate_event_payload(VALID_CANDIDATE)
    assert result.valid is True, f"valid candidate rejected: {result.reasons}"
    normalized = result.require_valid()
    assert normalized["symbol"] == "AAPL", "symbol must be upper-cased"
    assert normalized["side"] == "buy", "side must be lower-cased"
    assert normalized["confidence"] == 0.75
    assert normalized["dry_run"] is True

    missing = validate_candidate_event_payload(CANDIDATE_MISSING_FIELDS)
    assert missing.valid is False, "candidate with missing fields must fail closed"
    assert missing.normalized is None
    assert any("missing required key: symbol" in r for r in missing.reasons), missing.reasons
    assert any("missing required key: confidence" in r for r in missing.reasons), missing.reasons

    malformed = validate_candidate_event_payload(CANDIDATE_MALFORMED_TYPES)
    assert malformed.valid is False, "candidate with malformed types must fail closed"
    assert any("side must be buy/sell" in r for r in malformed.reasons), malformed.reasons
    assert any("confidence must be a number" in r for r in malformed.reasons), malformed.reasons
    assert any("dry_run must be boolean" in r for r in malformed.reasons), malformed.reasons
    assert any("metadata must be a dict" in r for r in malformed.reasons), malformed.reasons

    # Out-of-range confidence fails closed.
    out_of_range = validate_candidate_event_payload({**VALID_CANDIDATE, "confidence": 1.2})
    assert out_of_range.valid is False
    assert any("within [0, 1]" in r for r in out_of_range.reasons), out_of_range.reasons

    # Boolean confidence is not a number.
    bool_confidence = validate_candidate_event_payload({**VALID_CANDIDATE, "confidence": True})
    assert bool_confidence.valid is False, "boolean confidence must fail closed"

    # Non-dict payload fails closed.
    non_dict = validate_candidate_event_payload(["not", "a", "dict"])
    assert non_dict.valid is False

    # require_valid raises on invalid payloads.
    try:
        missing.require_valid()
    except ValueError:
        pass
    else:
        raise AssertionError("require_valid must raise for invalid candidate payload")


def check_needs_human_fixtures() -> None:
    result = validate_needs_human_event_payload(VALID_NEEDS_HUMAN)
    assert result.valid is True, f"valid NEEDS_HUMAN rejected: {result.reasons}"
    normalized = result.require_valid()
    assert normalized["reason_code"] == "CANDIDATE_VALIDATION_ERROR"
    assert normalized["reason"] == "fixture reason"
    assert normalized["dry_run"] is True

    missing = validate_needs_human_event_payload(NEEDS_HUMAN_MISSING_REASON_CONTEXT)
    assert missing.valid is False, "NEEDS_HUMAN missing reason/context must fail closed"
    assert any("missing required key: reason_code" in r for r in missing.reasons), missing.reasons
    assert any("missing required key: reason" in r for r in missing.reasons), missing.reasons

    # Blank reason fails closed.
    blank_reason = validate_needs_human_event_payload({**VALID_NEEDS_HUMAN, "reason": "   "})
    assert blank_reason.valid is False
    assert any("reason must be non-empty" in r for r in blank_reason.reasons), blank_reason.reasons

    non_dict = validate_needs_human_event_payload(None)
    assert non_dict.valid is False


def check_poke_handoff_fixtures() -> None:
    result = validate_poke_handoff_payload(VALID_POKE_HANDOFF)
    assert result.valid is True, f"valid poke handoff rejected: {result.reasons}"
    normalized = result.require_valid()
    assert normalized["destination"] == LOCAL_POKE_DESTINATION
    assert normalized["dry_run"] is True

    malformed = validate_poke_handoff_payload(MALFORMED_POKE_HANDOFF)
    assert malformed.valid is False, "malformed poke handoff must fail closed"
    assert any("handoff_id must be non-empty" in r for r in malformed.reasons), malformed.reasons
    assert any("local-only" in r for r in malformed.reasons), malformed.reasons
    assert any("dry_run must be boolean" in r for r in malformed.reasons), malformed.reasons

    # Any non-local destination fails closed, even if everything else is valid.
    external = validate_poke_handoff_payload(
        {**VALID_POKE_HANDOFF, "destination": "poke_sms_external"}
    )
    assert external.valid is False, "external poke destination must fail closed"

    non_dict = validate_poke_handoff_payload("not-a-dict")
    assert non_dict.valid is False


def check_watcher_artifacts_match_schema() -> None:
    """Watcher-emitted artifacts must satisfy the hardened schemas (no network)."""
    import importlib.util
    import json
    import tempfile

    module_path = REPO_ROOT / "autohedge" / "autohedge" / "overnight_scaffold.py"
    spec = importlib.util.spec_from_file_location("overnight_scaffold_watcher", str(module_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["overnight_scaffold_watcher"] = module
    spec.loader.exec_module(module)

    with tempfile.TemporaryDirectory(prefix="watcher-schema-smoke-") as tmp:
        writer = module.OvernightArtifactWriter(
            session_id="schema-smoke",
            artifact_root=Path(tmp) / "artifacts",
        )
        watcher = module.DeterministicTier0Watcher(
            run_id="schema-smoke-run",
            writer=writer,
            dry_run=True,
            enable_poke_handoff=True,
        )
        watcher.run_once(
            [
                {"symbol": "MSFT", "side": "buy", "confidence": 0.5, "strategy": "schema"},
                {"symbol": "", "side": "sell", "confidence": 2.0, "strategy": "invalid"},
            ]
        )

        candidate_rows = [
            json.loads(line)
            for line in writer.candidate_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        needs_human_rows = [
            json.loads(line)
            for line in writer.needs_human_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        poke_rows = [
            json.loads(line)
            for line in writer.poke_queue_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        assert len(candidate_rows) == 1 and len(needs_human_rows) == 1 and len(poke_rows) == 1
        for row in candidate_rows:
            assert validate_candidate_event_payload(row).valid is True, row
        for row in needs_human_rows:
            assert validate_needs_human_event_payload(row).valid is True, row
        for row in poke_rows:
            assert validate_poke_handoff_payload(row).valid is True, row
            assert row["destination"] == LOCAL_POKE_DESTINATION


def main() -> None:
    check_candidate_fixtures()
    check_needs_human_fixtures()
    check_poke_handoff_fixtures()
    check_watcher_artifacts_match_schema()
    print("watcher fixtures schema smoke: ok")


if __name__ == "__main__":
    main()
