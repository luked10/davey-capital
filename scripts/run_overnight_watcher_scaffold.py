#!/usr/bin/env python3
"""Run deterministic local watcher scaffold once."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

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


def _build_payloads(include_invalid: bool) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = [
        {
            "symbol": "AAPL",
            "side": "buy",
            "confidence": 0.82,
            "strategy": "overnight-breakout",
            "source": "deterministic-fixture",
            "metadata": {"batch": "nightly"},
        }
    ]
    if include_invalid:
        payloads.append(
            {
                "symbol": "",
                "side": "hold",
                "confidence": "n/a",
                "strategy": "bad-payload",
            }
        )
    return payloads


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic local tier0 watcher scaffold runner."
    )
    parser.add_argument(
        "--session-id",
        default="overnight-local-session",
        help="Session identifier used for artifact file paths.",
    )
    parser.add_argument(
        "--run-id",
        default="overnight-local-run",
        help="Run identifier for candidate events.",
    )
    parser.add_argument(
        "--artifact-root",
        default="logs/overnight",
        help="Artifact root directory.",
    )
    parser.add_argument(
        "--include-invalid",
        action="store_true",
        help="Include a deterministic invalid payload to emit NEEDS_HUMAN.",
    )
    parser.add_argument(
        "--disable-poke-handoff",
        action="store_true",
        help="Disable local poke bridge queue writes.",
    )
    args = parser.parse_args()

    overnight_module = _load_overnight_module()
    writer_cls = overnight_module.OvernightArtifactWriter
    watcher_cls = overnight_module.DeterministicTier0Watcher

    writer = writer_cls(
        session_id=args.session_id,
        artifact_root=args.artifact_root,
    )
    watcher = watcher_cls(
        run_id=args.run_id,
        writer=writer,
        dry_run=True,
        enable_poke_handoff=not args.disable_poke_handoff,
    )
    result = watcher.run_once(_build_payloads(args.include_invalid))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
