#!/usr/bin/env python3
"""Deterministic parity smoke for REPL/task runtime lifecycle.

Reproduces the wiped semantic regression in the AutoHedge CLI/REPL runtime:

  - DEFAULT behavior must use a FRESH AutoHedge instance per task, so there is
    NO cross-task conversation/state bleed.
  - Persistent engine reuse must be OPT-IN only (e.g. CLI ``--persist``).

Verifies ``build_repl_runner`` / ``EphemeralAutoHedgeRunner`` /
``PersistentAutoHedgeEngine`` behavior without constructing a real engine or
making any network/broker calls (a fake factory is injected).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "runtime_scaffold.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location("runtime_scaffold", str(RUNTIME_MODULE))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {RUNTIME_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_scaffold"] = module
    spec.loader.exec_module(module)
    return module


class _FakeEngine:
    def __init__(self, registry: list) -> None:
        self.tasks: list[str] = []
        self.closed = False
        registry.append(self)

    def run(self, task: str, *args, **kwargs):
        # If state bled across tasks, this list would contain more than one task.
        self.tasks.append(task)
        return {"task": task, "history_len": len(self.tasks), "dry_run": True}

    def close(self) -> None:
        self.closed = True


def _make_factory(instances: list):
    def factory():
        return _FakeEngine(instances)

    return factory


def _assert_default_is_fresh_per_task(runtime) -> None:
    instances: list[_FakeEngine] = []
    runner = runtime.build_repl_runner(engine_factory=_make_factory(instances))

    # Default must be the ephemeral, fresh-per-task runner.
    assert isinstance(runner, runtime.EphemeralAutoHedgeRunner), (
        "default REPL runner must be EphemeralAutoHedgeRunner (fresh per task)"
    )

    r1 = runner.run_task("first-task")
    r2 = runner.run_task("second-task")

    # A fresh engine per task: two distinct instances, each seeing exactly one task.
    assert len(instances) == 2, f"expected 2 fresh engines, got {len(instances)}"
    assert instances[0] is not instances[1]
    assert instances[0].tasks == ["first-task"]
    assert instances[1].tasks == ["second-task"]
    # No cross-task history bleed.
    assert r1["history_len"] == 1 and r2["history_len"] == 1
    assert r1["dry_run"] is True and r2["dry_run"] is True
    # Each ephemeral engine is closed immediately after its task.
    assert instances[0].closed and instances[1].closed
    assert runner.create_count == 2 and runner.run_count == 2

    runner.close()  # no-op, must not raise


def _assert_persist_is_opt_in(runtime) -> None:
    instances: list[_FakeEngine] = []
    runner = runtime.build_repl_runner(
        persist=True,
        engine_factory=_make_factory(instances),
    )

    assert isinstance(runner, runtime.PersistentAutoHedgeEngine), (
        "persist=True must yield the persistent (reuse) engine"
    )

    runner.run_task("first-task")
    runner.run_task("second-task")

    # Persistent mode reuses a single engine across tasks (state DOES persist).
    assert len(instances) == 1, f"persistent mode must reuse one engine, got {len(instances)}"
    assert instances[0].tasks == ["first-task", "second-task"]
    assert runner.create_count == 1 and runner.run_count == 2

    runner.close()
    assert instances[0].closed


def _assert_cli_default_does_not_persist() -> None:
    # The CLI must default persist=False (fresh per task); --persist is opt-in.
    cli_module = REPO_ROOT / "autohedge" / "autohedge" / "cli.py"
    source = cli_module.read_text(encoding="utf-8")
    assert "def run_repl(persist: bool = False)" in source, (
        "run_repl must default to non-persistent (fresh-per-task) behavior"
    )
    assert "build_repl_runner(persist=persist)" in source, (
        "run_repl must route through build_repl_runner with the persist flag"
    )
    assert '"--persist"' in source, "CLI must expose an explicit --persist opt-in flag"


def main() -> None:
    runtime = _load_runtime_module()
    _assert_default_is_fresh_per_task(runtime)
    _assert_persist_is_opt_in(runtime)
    _assert_cli_default_does_not_persist()
    print("repl lifecycle parity smoke: ok")


if __name__ == "__main__":
    main()
