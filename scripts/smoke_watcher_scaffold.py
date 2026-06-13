#!/usr/bin/env python3
"""Smoke test for local deterministic watcher scaffolding."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOHEDGE_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "overnight_scaffold.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_overnight_module():
    spec = importlib.util.spec_from_file_location(
        "overnight_scaffold",
        str(AUTOHEDGE_MODULE),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {AUTOHEDGE_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["overnight_scaffold"] = module
    spec.loader.exec_module(module)
    return module


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def main() -> None:
    overnight_module = _load_overnight_module()
    writer_cls = overnight_module.OvernightArtifactWriter
    watcher_cls = overnight_module.DeterministicTier0Watcher

    with tempfile.TemporaryDirectory(prefix="watcher-smoke-") as tmp:
        root = Path(tmp) / "artifacts"
        writer = writer_cls(
            session_id="smoke-session",
            artifact_root=root,
        )
        watcher = watcher_cls(
            run_id="smoke-run",
            writer=writer,
            dry_run=True,
            enable_poke_handoff=True,
        )
        payloads = [
            {
                "symbol": "AAPL",
                "side": "buy",
                "confidence": 0.75,
                "strategy": "smoke",
            },
            {
                "symbol": "",
                "side": "sell",
                "confidence": 1.2,
                "strategy": "invalid",
            },
        ]
        result = watcher.run_once(payloads)
        summary = result["summary"]
        assert summary["dry_run"] is True
        assert summary["network_enabled"] is False
        assert summary["processed"] == 2
        assert summary["ok"] == 1
        assert summary["needs_human"] == 1

        candidates = _read_jsonl(writer.candidate_path)
        needs_human = _read_jsonl(writer.needs_human_path)
        poke = _read_jsonl(writer.poke_queue_path)
        runs = _read_jsonl(writer.run_summary_path)

        assert len(candidates) == 1
        assert candidates[0]["symbol"] == "AAPL"
        assert candidates[0]["dry_run"] is True
        assert len(needs_human) == 1
        assert needs_human[0]["reason_code"] == "CANDIDATE_VALIDATION_ERROR"
        assert needs_human[0]["dry_run"] is True
        assert len(poke) == 1
        assert poke[0]["destination"] == "poke_bridge_local_queue"
        assert poke[0]["dry_run"] is True
        assert len(runs) == 1
        assert runs[0]["network_enabled"] is False

        bad_handoff = overnight_module.PokeBridgeHandoff(
            handoff_id="bad-handoff",
            run_id="smoke-run",
            created_at="2026-06-13T00:00:00Z",
            candidate_event_id="bad-candidate",
            dry_run=True,
            metadata={"symbol": "AAPL", "side": "hold", "confidence": 0.5},
        )
        assert writer.enqueue_poke_handoff(bad_handoff) == ""
        needs_human = _read_jsonl(writer.needs_human_path)
        poke = _read_jsonl(writer.poke_queue_path)
        assert len(needs_human) == 2
        assert needs_human[-1]["reason_code"] == "POKE_HANDOFF_VALIDATION_ERROR"
        assert len(poke) == 1, "invalid handoff must not enter poke queue"

    print("watcher scaffold smoke: ok")


if __name__ == "__main__":
    main()
