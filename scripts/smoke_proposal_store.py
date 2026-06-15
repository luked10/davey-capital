#!/usr/bin/env python3
"""Offline smoke for the persistent proposal store.

Verifies save/retrieve/delete and, critically, that a proposal survives a
simulated machine restart: a brand-new ProposalStore instance pointed at the
same DAVEY_ROOT reads back the proposal a previous instance wrote. This is the
guarantee that record_approval_decision keeps working after a Fly restart wipes
the in-memory state.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "state" / "proposal_store.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("proposal_store_smoke", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["proposal_store_smoke"] = module
    spec.loader.exec_module(module)
    return module


def _sample_intent_json(handoff_id: str) -> str:
    return json.dumps({
        "intent_id": f"intent-{handoff_id}",
        "signal_id": f"signal-{handoff_id}",
        "broker": "alpaca",
        "symbol": "NVDA",
        "side": "buy",
        "quantity": 1.0,
        "created_at": "2026-06-15T03:00:00Z",
        "order_type": "market",
        "limit_price": None,
        "time_in_force": "day",
        "dry_run": True,
        "approved": False,
        "approved_by": "",
        "approved_at": "",
        "status": "pending",
        "metadata": {"rationale": "Strong momentum with confirming volume."},
    })


def _sample_candidate(handoff_id: str) -> dict:
    return {
        "handoff_id": handoff_id,
        "symbol": "NVDA",
        "side": "buy",
        "confidence": 0.91,
    }


def _sample_proposal_payload(rationale: str) -> dict:
    return {
        "intent_id": "intent-smoke",
        "intent": None,
        "needs_human": False,
        "rationale": rationale,
        "proposal_text": f"TRADE PROPOSAL\nRationale: {rationale}",
    }


def main() -> None:
    mod = _load_module()
    ProposalStore = mod.ProposalStore

    with tempfile.TemporaryDirectory(prefix="proposal-store-smoke-") as tmp:
        root = Path(tmp)
        store = ProposalStore(davey_root=root)

        # Missing handoff returns None.
        assert store.get_proposal("missing-0000") is None

        # Save then retrieve in the same instance.
        intent_json = _sample_intent_json("verify-0001")
        rationale = "Strong momentum with confirming volume."
        store.save_proposal(
            handoff_id="verify-0001",
            session_id="smoke-session",
            candidate=_sample_candidate("verify-0001"),
            proposal_payload=_sample_proposal_payload(rationale),
            intent_json=intent_json,
        )
        got = store.get_proposal("verify-0001")
        assert got is not None, "expected record after save"
        assert got["session_id"] == "smoke-session"
        assert got["intent_json"] == intent_json
        assert got["proposal_payload"]["rationale"] == rationale
        assert got["candidate"]["handoff_id"] == "verify-0001"

        # The db file lives on the volume path.
        assert (root / "state" / "proposals.db").exists()

        # Simulated restart: a fresh instance reads the same DB.
        restarted = ProposalStore(davey_root=root)
        survived = restarted.get_proposal("verify-0001")
        assert survived is not None, "expected proposal to survive restart"
        assert survived["intent_json"] == intent_json
        assert survived["proposal_payload"]["rationale"] == rationale

        # Upsert overwrites in place.
        new_rationale = "Updated rationale text."
        restarted.save_proposal(
            handoff_id="verify-0001",
            session_id="smoke-session",
            candidate=_sample_candidate("verify-0001"),
            proposal_payload=_sample_proposal_payload(new_rationale),
            intent_json=intent_json,
        )
        upserted = restarted.get_proposal("verify-0001")
        assert upserted is not None
        assert upserted["proposal_payload"]["rationale"] == new_rationale

        # needs_human marker: intent_json=None is stored and returned as None.
        store.save_proposal(
            handoff_id="verify-needs-human",
            session_id="smoke-session",
            candidate=_sample_candidate("verify-needs-human"),
            proposal_payload=_sample_proposal_payload("Circuit breaker blocked"),
            intent_json=None,
        )
        marker = store.get_proposal("verify-needs-human")
        assert marker is not None
        assert marker["intent_json"] is None

        # Delete cleans up; a later instance no longer sees it.
        restarted.delete_proposal("verify-0001")
        assert restarted.get_proposal("verify-0001") is None
        assert ProposalStore(davey_root=root).get_proposal("verify-0001") is None

        # Blank handoff ids are ignored, not stored.
        store.save_proposal(
            handoff_id="",
            session_id="smoke-session",
            candidate={},
            proposal_payload={},
            intent_json=None,
        )
        assert store.get_proposal("") is None

        # count_proposals reflects persisted rows.
        count = store.count_proposals()
        assert count >= 1, f"expected at least 1 row, got {count}"

        # Concurrent writes from independent instances are thread-safe.
        ids = [f"handoff-concurrent-{idx:04d}" for idx in range(100)]

        def save(handoff_id: str) -> None:
            ij = _sample_intent_json(handoff_id)
            ProposalStore(davey_root=root).save_proposal(
                handoff_id=handoff_id,
                session_id="smoke-concurrent",
                candidate=_sample_candidate(handoff_id),
                proposal_payload=_sample_proposal_payload(f"rationale {handoff_id}"),
                intent_json=ij,
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(save, ids))

        verifier = ProposalStore(davey_root=root)
        for handoff_id in ids:
            row = verifier.get_proposal(handoff_id)
            assert row is not None, f"missing concurrent row for {handoff_id}"
            assert row["session_id"] == "smoke-concurrent"
            parsed = json.loads(row["intent_json"])
            assert parsed["intent_id"] == f"intent-{handoff_id}"

    print("proposal store smoke: ok")


if __name__ == "__main__":
    main()
