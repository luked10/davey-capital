#!/usr/bin/env python3
"""Offline smoke for Sonnet proposal runtime dry_run mode handling."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "proposal" / "sonnet_client.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sonnet_runtime_mode_smoke", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sonnet_runtime_mode_smoke"] = module
    spec.loader.exec_module(module)
    return module


def _base_payload(*, dry_run: bool, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "intent_id": "intent-runtime-mode",
        "signal_id": "sig-runtime-mode",
        "broker": "alpaca",
        "symbol": "NVDA",
        "side": "buy",
        "quantity": 1.0,
        "created_at": "2026-06-14T00:00:00Z",
        "order_type": "market",
        "limit_price": None,
        "time_in_force": "day",
        "dry_run": dry_run,
        "approved": False,
        "approved_by": "",
        "approved_at": "",
        "status": "pending",
        "metadata": metadata if metadata is not None else {"rationale": "runtime mode smoke"},
    }


def main() -> None:
    mod = _load_module()

    class FakeClient(mod.SonnetProposalClient):
        def __init__(self, raw: dict[str, Any]) -> None:
            self.raw = raw
            self.prompt = ""

        def complete(self, *, static_prefix: str, candidate_suffix: str):
            self.prompt = static_prefix
            return mod.SonnetCompletionResponse(
                text=json.dumps(self.raw),
                model="fake-sonnet",
                provider="fake",
                cache_read_tokens=0,
                cache_write_tokens=0,
                input_tokens=0,
                output_tokens=0,
            )

    previous_live_mode = os.environ.get("DAVEY_LIVE_MODE")
    try:
        os.environ.pop("DAVEY_LIVE_MODE", None)
        dry_client = FakeClient(_base_payload(dry_run=True))
        dry_result = dry_client.propose({"symbol": "NVDA"})
        assert dry_result.intent is not None, dry_result.error
        assert dry_result.intent.dry_run is True
        assert "dry_run must be true" in dry_client.prompt

        os.environ["DAVEY_LIVE_MODE"] = "1"
        live_client = FakeClient(_base_payload(dry_run=False))
        live_result = live_client.propose({"symbol": "NVDA"})
        assert live_result.intent is not None, live_result.error
        assert live_result.intent.dry_run is False
        assert live_result.intent.approved is False
        assert live_result.validation is not None
        assert live_result.validation.allowed is True
        assert "dry_run must be false" in live_client.prompt

        missing_rationale = FakeClient(
            _base_payload(dry_run=False, metadata={})
        ).propose({"symbol": "NVDA"})
        assert missing_rationale.intent is None
        assert "Model returned empty rationale, proposal rejected" in missing_rationale.error

        for bad_rat in ("", "N/A", "none", "null", "  none  "):
            bad_rat_client = FakeClient(
                _base_payload(dry_run=False, metadata={"rationale": bad_rat})
            )
            bad_rat_res = bad_rat_client.propose({"symbol": "NVDA"})
            assert bad_rat_res.intent is None
            assert "Model returned empty rationale, proposal rejected" in bad_rat_res.error

    finally:
        if previous_live_mode is None:
            os.environ.pop("DAVEY_LIVE_MODE", None)
        else:
            os.environ["DAVEY_LIVE_MODE"] = previous_live_mode

    print("sonnet client runtime mode smoke: ok")


if __name__ == "__main__":
    main()
