#!/usr/bin/env python3
"""Smoke checks for persistent engine + scheduler scaffolding."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULE = REPO_ROOT / "autohedge" / "autohedge" / "runtime_scaffold.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location(
        "runtime_scaffold",
        str(RUNTIME_MODULE),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {RUNTIME_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_scaffold"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    runtime = _load_runtime_module()
    engine_cls = runtime.PersistentAutoHedgeEngine
    scheduler_cls = runtime.LocalSchedulerScaffold

    factory_calls = {"count": 0}

    class FakeAutoHedge:
        def __init__(self) -> None:
            self.task_log: list[str] = []

        def run(self, task: str, *args, **kwargs):
            self.task_log.append(task)
            return {"task": task, "dry_run": True}

    def fake_factory():
        factory_calls["count"] += 1
        return FakeAutoHedge()

    engine = engine_cls(engine_factory=fake_factory)
    result_one = engine.run_task("first-task")
    result_two = engine.run_task("second-task")
    assert result_one["task"] == "first-task"
    assert result_two["task"] == "second-task"
    assert result_one["dry_run"] is True
    assert result_two["dry_run"] is True
    assert engine.create_count == 1
    assert engine.run_count == 2
    assert factory_calls["count"] == 1
    engine.close()

    invocations: list[str] = []

    def fake_job():
        invocations.append("ran")
        return {"ok": True, "dry_run": True}

    scheduler = scheduler_cls(
        enabled=False,
        dry_run=True,
        prefer_apscheduler=False,
    )
    assert scheduler.backend == "stdlib"
    job = scheduler.register_interval_job(
        "smoke-job",
        fake_job,
        seconds=30,
        metadata={"scope": "smoke"},
    )
    assert job.job_id == "smoke-job"
    assert job.dry_run is True
    assert len(scheduler.snapshot_jobs()) == 1
    assert scheduler.start() is False
    ran = scheduler.run_pending_once()
    assert len(ran) == 1
    assert ran[0]["job_id"] == "smoke-job"
    assert ran[0]["dry_run"] is True
    assert invocations == ["ran"]
    scheduler.stop()
    assert scheduler.is_running is False

    print("engine scheduler step5 smoke: ok")


if __name__ == "__main__":
    main()
