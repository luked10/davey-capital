"""Persistent engine + scheduler scaffolding for local overnight builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable


EngineFactory = Callable[[], Any]
JobCallable = Callable[[], Any]


class PersistentAutoHedgeEngine:
    """Reuse a single AutoHedge instance across repeated tasks."""

    def __init__(self, engine_factory: EngineFactory | None = None) -> None:
        self._engine_factory = engine_factory or self._default_engine_factory
        self._engine: Any | None = None
        self._lock = Lock()
        self._create_count = 0
        self._run_count = 0

    @staticmethod
    def _default_engine_factory() -> Any:
        from autohedge.main import AutoHedge

        return AutoHedge()

    def get_engine(self) -> Any:
        with self._lock:
            if self._engine is None:
                self._engine = self._engine_factory()
                self._create_count += 1
            return self._engine

    def run_task(self, task: str, *args: Any, **kwargs: Any) -> Any:
        engine = self.get_engine()
        self._run_count += 1
        return engine.run(task=task, *args, **kwargs)

    def close(self) -> None:
        with self._lock:
            engine = self._engine
            self._engine = None
        if engine is None:
            return
        close_fn = getattr(engine, "close", None)
        if callable(close_fn):
            close_fn()

    @property
    def create_count(self) -> int:
        return self._create_count

    @property
    def run_count(self) -> int:
        return self._run_count


@dataclass(slots=True)
class SchedulerJob:
    job_id: str
    interval_seconds: float
    dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class LocalSchedulerScaffold:
    """Scheduler wrapper that prefers APScheduler, with stdlib fallback."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        dry_run: bool = True,
        prefer_apscheduler: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run)
        self._running = False
        self._jobs: dict[str, tuple[SchedulerJob, JobCallable]] = {}
        self._backend = "stdlib"
        self._scheduler = None
        if prefer_apscheduler:
            try:
                from apscheduler.schedulers.background import BackgroundScheduler

                self._scheduler = BackgroundScheduler()
                self._backend = "apscheduler"
            except Exception:
                self._scheduler = None
                self._backend = "stdlib"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def is_running(self) -> bool:
        return self._running

    def register_interval_job(
        self,
        job_id: str,
        func: JobCallable,
        *,
        seconds: float,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerJob:
        clean_job_id = str(job_id or "").strip()
        if not clean_job_id:
            raise ValueError("job_id must be non-empty")
        if not callable(func):
            raise ValueError("func must be callable")
        interval_seconds = float(seconds)
        if interval_seconds <= 0:
            raise ValueError("seconds must be positive")

        job = SchedulerJob(
            job_id=clean_job_id,
            interval_seconds=interval_seconds,
            dry_run=self.dry_run,
            metadata=dict(metadata or {}),
        )
        self._jobs[clean_job_id] = (job, func)

        if self._scheduler is not None:
            self._scheduler.add_job(
                func=func,
                trigger="interval",
                seconds=interval_seconds,
                id=clean_job_id,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        return job

    def snapshot_jobs(self) -> list[SchedulerJob]:
        return [pair[0] for pair in self._jobs.values()]

    def run_pending_once(self) -> list[dict[str, Any]]:
        executed: list[dict[str, Any]] = []
        for job, func in self._jobs.values():
            result = func()
            executed.append(
                {
                    "job_id": job.job_id,
                    "dry_run": job.dry_run,
                    "result": result,
                }
            )
        return executed

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._running:
            return True
        if self._scheduler is not None:
            self._scheduler.start()
        self._running = True
        return True

    def stop(self) -> None:
        if self._scheduler is not None and self._running:
            self._scheduler.shutdown(wait=False)
        self._running = False
