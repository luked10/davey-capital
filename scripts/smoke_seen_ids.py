#!/usr/bin/env python3
"""Offline smoke for persistent seen handoff ids."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "state" / "seen_ids.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("seen_ids_smoke", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["seen_ids_smoke"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    mod = _load_module()
    SeenIdsStore = mod.SeenIdsStore

    with tempfile.TemporaryDirectory(prefix="seen-ids-smoke-") as tmp:
        root = Path(tmp)
        store = SeenIdsStore(davey_root=root)
        assert store.is_seen("handoff-0001") is False
        store.mark_seen("handoff-0001")
        assert store.is_seen("handoff-0001") is True

        restarted = SeenIdsStore(davey_root=root)
        assert restarted.is_seen("handoff-0001") is True
        assert (root / "state" / "seen_ids.db").exists()

        ids = [f"handoff-concurrent-{idx:04d}" for idx in range(200)]

        def mark_seen(handoff_id: str) -> None:
            SeenIdsStore(davey_root=root).mark_seen(handoff_id)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(mark_seen, ids))

        verifier = SeenIdsStore(davey_root=root)
        assert all(verifier.is_seen(handoff_id) for handoff_id in ids)

    print("seen ids smoke: ok")


if __name__ == "__main__":
    main()
