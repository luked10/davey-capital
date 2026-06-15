#!/usr/bin/env python3
"""Offline smoke for the persistent proposal store.

Verifies save/retrieve/delete and, critically, that a proposal survives a
simulated machine restart: a brand-new ProposalStore instance pointed at the
same DAVEY_ROOT reads back the proposal a previous instance wrote. This is the
guarantee that record_approval_decision keeps working after a Fly restart wipes
the in-memory dict.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
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


def _sample_intent(handoff_id: str) -> dict:
    return {
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
        intent = _sample_intent("verify-0001")
        store.save_proposal("verify-0001", intent, "Strong momentum with confirming volume.")
        got = store.get_proposal("verify-0001")
        assert got is not None
        assert got["handoff_id"] == "verify-0001"
        assert got["intent"] == intent
        assert got["rationale"] == "Strong momentum with confirming volume."
        assert got["saved_at"]

        # The db file lives on the volume path.
        assert (root / "state" / "proposals.db").exists()

        # Simulated restart: a fresh instance reads the same DB.
        restarted = ProposalStore(davey_root=root)
        survived = restarted.get_proposal("verify-0001")
        assert survived is not None
        assert survived["intent"] == intent
        assert survived["rationale"] == "Strong momentum with confirming volume."

        # Upsert overwrites in place.
        restarted.save_proposal("verify-0001", intent, "Updated rationale text.")
        assert restarted.get_proposal("verify-0001")["rationale"] == "Updated rationale text."

        # Delete cleans up; a later instance no longer sees it.
        restarted.delete_proposal("verify-0001")
        assert restarted.get_proposal("verify-0001") is None
        assert ProposalStore(davey_root=root).get_proposal("verify-0001") is None

        # Blank handoff ids are ignored, not stored.
        store.save_proposal("", intent, "ignored")
        assert store.get_proposal("") is None

        # Concurrent writes from independent instances are thread-safe.
        ids = [f"handoff-concurrent-{idx:04d}" for idx in range(100)]

        def save(handoff_id: str) -> None:
            ProposalStore(davey_root=root).save_proposal(
                handoff_id, _sample_intent(handoff_id), f"rationale {handoff_id}"
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(save, ids))

        verifier = ProposalStore(davey_root=root)
        for handoff_id in ids:
            row = verifier.get_proposal(handoff_id)
            assert row is not None
            assert row["intent"]["intent_id"] == f"intent-{handoff_id}"

    print("proposal store smoke: ok")


if __name__ == "__main__":
    main()
