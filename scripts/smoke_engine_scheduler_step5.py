#!/usr/bin/env python3
"""Smoke checks for persistent engine + scheduler scaffolding."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

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
    with tempfile.TemporaryDirectory(prefix="runtime-scaffold-root-") as tmp:
        previous_davey_root = os.environ.get("DAVEY_ROOT")
        os.environ["DAVEY_ROOT"] = tmp
        try:
            runtime = _load_runtime_module()
            written = runtime.write_runtime_state(updated_at="2026-06-12T00:00:00Z")
        finally:
            if previous_davey_root is None:
                os.environ.pop("DAVEY_ROOT", None)
            else:
                os.environ["DAVEY_ROOT"] = previous_davey_root

        assert written == Path(tmp).resolve() / "runtime_state.json"
        assert written.exists()

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

    # The CLI/REPL default runtime is fresh-per-task; persistent reuse is opt-in.
    # Keep these symbols exercised here so they are not orphaned (full lifecycle
    # parity lives in smoke_repl_lifecycle.py).
    build_repl_runner = runtime.build_repl_runner
    ephemeral_cls = runtime.EphemeralAutoHedgeRunner

    default_calls = {"count": 0}

    def default_factory():
        default_calls["count"] += 1
        return FakeAutoHedge()

    default_runner = build_repl_runner(engine_factory=default_factory)
    assert isinstance(default_runner, ephemeral_cls)
    default_runner.run_task("task-a")
    default_runner.run_task("task-b")
    assert default_calls["count"] == 2  # a fresh engine per task
    assert default_runner.create_count == 2
    assert default_runner.run_count == 2

    persistent_runner = build_repl_runner(persist=True, engine_factory=fake_factory)
    assert isinstance(persistent_runner, engine_cls)
    persistent_runner.close()

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

    old_enabled = os.environ.pop("DAVEY_SCHEDULER_ENABLED", None)
    old_root = os.environ.get("DAVEY_ROOT")
    try:
        assert runtime.scheduler_enabled_from_env() is False
        assert runtime.SCHEDULER_INTERVAL_SECONDS == 300
        disabled_scheduler = runtime.build_scheduler(
            fetcher=lambda: [],
            prefer_apscheduler=False,
        )
        assert disabled_scheduler.enabled is False
        disabled_jobs = disabled_scheduler.snapshot_jobs()
        assert len(disabled_jobs) == 2
        assert disabled_jobs[1].job_id == runtime.DAILY_REPORT_JOB_ID
        assert disabled_jobs[1].schedule == "daily_utc"
        assert disabled_jobs[1].utc_hour == 21
        assert disabled_jobs[1].utc_minute == 0
        assert disabled_scheduler.start() is False

        with tempfile.TemporaryDirectory(prefix="scheduler-start-smoke-") as tmp:
            os.environ["DAVEY_ROOT"] = tmp
            os.environ["DAVEY_SCHEDULER_ENABLED"] = "1"

            def fake_fetch_candidates():
                return [
                    {
                        "symbol": "NVDA",
                        "side": "buy",
                        "confidence": 0.8,
                        "strategy": "smoke",
                        "source": "smoke_market_feed",
                        "dry_run": True,
                    }
                ]

            start_result = runtime.start(
                prefer_apscheduler=False,
                fetcher=fake_fetch_candidates,
            )
            assert start_result["enabled"] is True
            assert start_result["started"] is True
            assert start_result["backend"] == "stdlib"
            assert start_result["jobs"][0]["interval_seconds"] == 300
            assert start_result["jobs"][1]["job_id"] == runtime.DAILY_REPORT_JOB_ID
            assert start_result["jobs"][1]["schedule"] == "daily_utc"
            assert start_result["jobs"][1]["utc_hour"] == 21
            assert start_result["jobs"][1]["utc_minute"] == 0
            initial = start_result["initial_result"]
            assert len(initial) == 2
            cycle_result = initial[0]["result"]
            assert cycle_result["candidate_count"] == 1
            assert cycle_result["summary"]["processed"] == 1
            assert cycle_result["summary"]["ok"] == 1
            assert cycle_result["fetch_error"] == ""
            runtime_state_path = Path(cycle_result["runtime_state_path"])
            assert runtime_state_path == Path(tmp).resolve() / "runtime_state.json"
            runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
            assert runtime_state["circuit_breaker_status"] == "normal"
            assert runtime_state["last_report_at"]
            report_result = initial[1]["result"]
            assert report_result["ok"] is True
            report_path = Path(report_result["report_path"])
            assert report_path == Path(tmp).resolve() / "reports" / f"daily_{report_result['report_date']}.md"
            assert "Today:" in report_path.read_text(encoding="utf-8")
            queue_path = (
                Path(tmp)
                / "logs"
                / "overnight"
                / "scheduler"
                / "poke_bridge_queue.jsonl"
            )
            assert queue_path.exists()
    finally:
        if old_enabled is None:
            os.environ.pop("DAVEY_SCHEDULER_ENABLED", None)
        else:
            os.environ["DAVEY_SCHEDULER_ENABLED"] = old_enabled
        if old_root is None:
            os.environ.pop("DAVEY_ROOT", None)
        else:
            os.environ["DAVEY_ROOT"] = old_root

    print("engine scheduler step5 smoke: ok")


if __name__ == "__main__":
    main()
