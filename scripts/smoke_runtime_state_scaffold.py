#!/usr/bin/env python3
"""Deterministic smoke for the runtime state scaffold (Slice D).

No live account reads, no broker calls, no network access. Writes only to a
temporary directory and validates the committed example file.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "runtime" / "runtime_state.py"
EXAMPLE_PATH = REPO_ROOT / "runtime_state.example.json"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("runtime_state", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_state"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    mod = _load_module()
    RuntimeState = mod.RuntimeState
    default_runtime_state = mod.default_runtime_state
    load_runtime_state = mod.load_runtime_state
    save_runtime_state = mod.save_runtime_state

    # Safe defaults: paper broker, dry_run=True, live_mode=False.
    default = default_runtime_state(updated_at="2026-06-10T00:00:00Z")
    assert default.active_broker == "paper"
    assert default.dry_run is True
    assert default.live_mode is False
    assert default.updated_at == "2026-06-10T00:00:00Z"

    with tempfile.TemporaryDirectory(prefix="runtime-state-smoke-") as tmp:
        state_path = Path(tmp) / "runtime_state.json"

        # Round trip: save then load, deterministically stamped.
        state = RuntimeState(
            active_broker="paper",
            dry_run=True,
            live_mode=False,
            positions_summary={"open_positions": 2, "source": "repo-backed artifacts"},
            latest_signal_ids=["sig-0001", "sig-0002"],
            circuit_breaker_status="normal",
            last_error="",
            last_health_check="2026-06-10T00:00:00Z",
        )
        written = save_runtime_state(state, state_path, updated_at="2026-06-10T00:00:01Z")
        assert written == state_path and state_path.exists()

        loaded = load_runtime_state(state_path)
        assert loaded.ok is True and loaded.needs_human is False, loaded.reasons
        roundtrip = loaded.require_state()
        assert roundtrip.active_broker == "paper"
        assert roundtrip.dry_run is True and roundtrip.live_mode is False
        assert roundtrip.latest_signal_ids == ["sig-0001", "sig-0002"]
        assert roundtrip.updated_at == "2026-06-10T00:00:01Z"

        # Missing file fails closed with NEEDS_HUMAN-style result.
        missing = load_runtime_state(Path(tmp) / "does_not_exist.json")
        assert missing.ok is False and missing.needs_human is True
        assert missing.state is None

        # Invalid JSON fails closed.
        bad_json_path = Path(tmp) / "bad.json"
        bad_json_path.write_text("{not json", encoding="utf-8")
        bad_json = load_runtime_state(bad_json_path)
        assert bad_json.ok is False and bad_json.needs_human is True
        assert any("not valid JSON" in r for r in bad_json.reasons), bad_json.reasons

        # Missing keys fail closed.
        partial_path = Path(tmp) / "partial.json"
        partial_path.write_text(json.dumps({"active_broker": "paper"}), encoding="utf-8")
        partial = load_runtime_state(partial_path)
        assert partial.ok is False and partial.needs_human is True
        assert any("missing required key: dry_run" in r for r in partial.reasons)

        # Wrong types fail closed (string booleans are never coerced).
        wrong_types_path = Path(tmp) / "wrong_types.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["dry_run"] = "true"
        payload["latest_signal_ids"] = "sig-0001"
        wrong_types_path.write_text(json.dumps(payload), encoding="utf-8")
        wrong = load_runtime_state(wrong_types_path)
        assert wrong.ok is False and wrong.needs_human is True
        assert any("dry_run must be boolean" in r for r in wrong.reasons), wrong.reasons
        assert any("latest_signal_ids" in r for r in wrong.reasons), wrong.reasons

        # live_mode=True requires human review even when structurally valid.
        live_path = Path(tmp) / "live.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["live_mode"] = True
        payload["dry_run"] = False
        live_path.write_text(json.dumps(payload), encoding="utf-8")
        live = load_runtime_state(live_path)
        assert live.ok is False and live.needs_human is True
        assert any("human review" in r for r in live.reasons), live.reasons

        # Writing a malformed state fails closed.
        try:
            save_runtime_state(
                RuntimeState(active_broker="", dry_run=True, live_mode=False),
                Path(tmp) / "never_written.json",
            )
        except ValueError as exc:
            assert "malformed" in str(exc)
        else:
            raise AssertionError("malformed state write must fail closed")
        assert not (Path(tmp) / "never_written.json").exists()

        # require_state raises for failed loads.
        try:
            missing.require_state()
        except ValueError:
            pass
        else:
            raise AssertionError("require_state must raise on failed load")

    # The committed example file is valid, credential-free, and safe-by-default.
    example = load_runtime_state(EXAMPLE_PATH)
    assert example.ok is True, example.reasons
    example_state = example.require_state()
    assert example_state.dry_run is True and example_state.live_mode is False
    example_text = EXAMPLE_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("api_key", "secret", "token", "password"):
        assert forbidden not in example_text, f"example file contains {forbidden!r}"

    print("runtime state scaffold smoke: ok")


if __name__ == "__main__":
    main()
