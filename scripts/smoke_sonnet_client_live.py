#!/usr/bin/env python3
"""Gated live smoke for the Anthropic Sonnet proposal client.

This smoke is intentionally NOT part of the deterministic offline matrix. It
exits successfully with SKIP unless DAVEY_LIVE_SMOKE=1 is set. When enabled, it
loads ANTHROPIC_API_KEY from the environment or local .env via the client,
calls Sonnet once, validates the resulting ExecutionIntent, and prints token
metadata so cache reads can be checked manually on a second run.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
AUDIT_MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "audit" / "artifacts.py"
SONNET_MODULE_PATH = (
    REPO_ROOT / "autohedge" / "autohedge" / "proposal" / "sonnet_client.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    if os.environ.get("DAVEY_LIVE_SMOKE") != "1":
        print("SKIP: set DAVEY_LIVE_SMOKE=1 to run live Sonnet smoke")
        return

    audit_module = _load_module("audit_artifacts_live_smoke", AUDIT_MODULE_PATH)
    sonnet_module = _load_module("sonnet_client_live_smoke", SONNET_MODULE_PATH)
    AuditArtifactWriter = audit_module.AuditArtifactWriter
    SonnetProposalClient = sonnet_module.SonnetProposalClient

    candidate = {
        "event_id": "live-smoke-nvda-0001",
        "signal_id": "sig-live-smoke-nvda-0001",
        "symbol": "NVDA",
        "side": "buy",
        "confidence": 0.85,
        "strategy": "live-smoke-fixture",
        "created_at": "2026-06-12T00:00:00Z",
        "dry_run": True,
        "metadata": {
            "source": "smoke_sonnet_client_live",
            "note": "dry-run proposal only; no broker execution",
        },
    }

    client = SonnetProposalClient()
    result = client.propose(candidate)

    assert result.intent is not None or result.needs_human is True, result
    assert result.needs_human is True or result.validation is not None, result
    if result.intent is not None:
        assert result.validation is not None
        assert result.validation.allowed is True, result.validation.reasons
        assert result.intent.dry_run is True
        assert result.intent.approved is False

        with tempfile.TemporaryDirectory(prefix="sonnet-live-smoke-audit-") as tmp:
            writer = AuditArtifactWriter(
                session_id="sonnet-live-smoke",
                artifact_root=Path(tmp),
                model=client.model,
                provider="anthropic",
            )
            write_result = writer.write_intent_artifact(result.intent)
            assert write_result.ok is True, write_result.reasons
            assert write_result.path is not None and write_result.path.exists()

    print("sonnet client live smoke: ok")
    print("needs_human:", result.needs_human)
    print("error:", result.error)
    print("token_meta:", json.dumps(result.token_meta, sort_keys=True))
    if result.raw:
        print("raw:", result.raw)


if __name__ == "__main__":
    main()
