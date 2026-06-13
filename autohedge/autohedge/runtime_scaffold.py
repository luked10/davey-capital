"""Persistent engine + scheduler scaffolding for local overnight builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import Lock
from typing import Any, Callable


EngineFactory = Callable[[], Any]
JobCallable = Callable[[], Any]
SCHEDULER_INTERVAL_SECONDS = 5 * 60
SCHEDULER_JOB_ID = "tier0-market-feed-watcher"
_ACTIVE_SCHEDULER: Any | None = None


def _utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    configured = os.getenv("DAVEY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if any(os.getenv(name) for name in ("FLY_APP_NAME", "FLY_MACHINE_ID", "FLY_REGION")):
        return Path("/app")
    return Path(__file__).resolve().parents[2]


def scheduler_enabled_from_env() -> bool:
    """Opt-in scheduler gate; disabled unless explicitly set to ``1``."""
    return os.getenv("DAVEY_SCHEDULER_ENABLED", "").strip() == "1"


def _load_local_module(name: str, relative_path: str):
    module_path = Path(__file__).resolve().parent / relative_path
    spec = importlib.util.spec_from_file_location(name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def runtime_state_path(repo_root: str | Path | None = None) -> Path:
    """Return the shared runtime_state.json path for MCP/Poke readers."""
    root = Path(repo_root).expanduser().resolve() if repo_root is not None else _repo_root()
    return root / "runtime_state.json"


def write_runtime_state(
    state: Any | None = None,
    *,
    updated_at: str | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    """Write runtime_state.json under DAVEY_ROOT by default."""
    try:
        from autohedge.runtime.runtime_state import default_runtime_state, save_runtime_state
    except Exception:
        runtime_state = _load_local_module(
            "davey_runtime_state_scaffold",
            "runtime/runtime_state.py",
        )
        default_runtime_state = runtime_state.default_runtime_state
        save_runtime_state = runtime_state.save_runtime_state

    runtime_state_value = (
        state if state is not None else default_runtime_state(updated_at=updated_at)
    )
    return save_runtime_state(
        runtime_state_value,
        runtime_state_path(repo_root),
        updated_at=updated_at,
    )


def _load_runtime_state_helpers():
    try:
        from autohedge.runtime.runtime_state import default_runtime_state
    except Exception:
        runtime_state = _load_local_module(
            "davey_runtime_state_scaffold",
            "runtime/runtime_state.py",
        )
        default_runtime_state = runtime_state.default_runtime_state
    return default_runtime_state


def _load_circuit_breaker_helpers():
    try:
        from autohedge.risk.circuit_breaker import (
            CircuitBreakerConfig,
            evaluate_circuit_breaker,
        )
        from autohedge.risk.observations import build_observations
    except Exception:
        circuit_breaker = _load_local_module(
            "davey_runtime_circuit_breaker",
            "risk/circuit_breaker.py",
        )
        observations = _load_local_module(
            "davey_runtime_observations",
            "risk/observations.py",
        )
        CircuitBreakerConfig = circuit_breaker.CircuitBreakerConfig
        evaluate_circuit_breaker = circuit_breaker.evaluate_circuit_breaker
        build_observations = observations.build_observations
    return CircuitBreakerConfig, evaluate_circuit_breaker, build_observations


def _load_circuit_breaker_config(repo_root: Path) -> Any:
    CircuitBreakerConfig, _, _ = _load_circuit_breaker_helpers()
    config_path = repo_root / "circuit_breaker_config.json"
    if not config_path.exists():
        return CircuitBreakerConfig(
            enabled=True,
            max_consecutive_losses=3,
            max_daily_loss_pct=0.02,
            max_open_trades=5,
        )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CircuitBreakerConfig(enabled="malformed")  # type: ignore[arg-type]
    if not isinstance(payload, dict):
        return CircuitBreakerConfig(enabled="malformed")  # type: ignore[arg-type]
    enabled = payload.get("enabled")
    max_losses = payload.get("max_consecutive_losses")
    max_daily_loss_pct = payload.get("max_daily_loss_pct")
    max_open_trades = payload.get("max_open_trades")
    if (
        not isinstance(enabled, bool)
        or isinstance(max_losses, bool)
        or not isinstance(max_losses, int)
        or max_losses < 0
        or isinstance(max_daily_loss_pct, bool)
        or not isinstance(max_daily_loss_pct, (int, float))
        or float(max_daily_loss_pct) < 0
        or isinstance(max_open_trades, bool)
        or not isinstance(max_open_trades, int)
        or max_open_trades < 0
    ):
        return CircuitBreakerConfig(enabled="malformed")  # type: ignore[arg-type]
    return CircuitBreakerConfig(
        enabled=enabled,
        max_consecutive_losses=max_losses,
        max_daily_loss_pct=float(max_daily_loss_pct),
        max_open_trades=max_open_trades,
    )


def _scheduler_circuit_breaker_status(repo_root: Path, symbol: str = "") -> str:
    CircuitBreakerConfig, evaluate_circuit_breaker, build_observations = (
        _load_circuit_breaker_helpers()
    )
    try:
        config = _load_circuit_breaker_config(repo_root)
        if isinstance(config, CircuitBreakerConfig) and config.enabled is False:
            return "disabled"
        observations = build_observations(symbol)
        result = evaluate_circuit_breaker(config, observations)
        return "blocked" if result.blocked else "normal"
    except Exception:
        return "blocked"


def _load_watcher_classes():
    try:
        from autohedge.overnight_scaffold import (
            DeterministicTier0Watcher,
            OvernightArtifactWriter,
        )
    except Exception:
        overnight = _load_local_module(
            "davey_runtime_overnight_scaffold",
            "overnight_scaffold.py",
        )
        DeterministicTier0Watcher = overnight.DeterministicTier0Watcher
        OvernightArtifactWriter = overnight.OvernightArtifactWriter
    return DeterministicTier0Watcher, OvernightArtifactWriter


def _load_fetch_candidates():
    try:
        from autohedge.data.market_feed import fetch_candidates
    except Exception:
        market_feed = _load_local_module(
            "davey_runtime_market_feed",
            "data/market_feed.py",
        )
        fetch_candidates = market_feed.fetch_candidates
    return fetch_candidates


def run_watcher_cycle(
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    repo_root: str | Path | None = None,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Fetch local market candidates and hand them to the deterministic watcher."""
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    clean_session_id = (
        session_id
        or os.getenv("DAVEY_SESSION_ID", "").strip()
        or "scheduler"
    )
    clean_run_id = run_id or f"scheduler-{_utc_now_compact()}"
    fetch_candidates = fetcher or _load_fetch_candidates()
    DeterministicTier0Watcher, OvernightArtifactWriter = _load_watcher_classes()

    fetch_error = ""
    try:
        payloads = fetch_candidates()
    except Exception as exc:
        fetch_error = str(exc)
        payloads = []
    writer = OvernightArtifactWriter(
        session_id=clean_session_id,
        artifact_root=root / "logs" / "overnight",
    )
    watcher = DeterministicTier0Watcher(
        run_id=clean_run_id,
        writer=writer,
        dry_run=True,
        enable_poke_handoff=True,
    )
    result = watcher.run_once(payloads)
    status_symbol = ""
    if payloads and isinstance(payloads[0], dict):
        status_symbol = str(payloads[0].get("symbol", "")).strip().upper()
    runtime_state_path_written = ""
    try:
        default_runtime_state = _load_runtime_state_helpers()
        state = default_runtime_state()
        state.circuit_breaker_status = _scheduler_circuit_breaker_status(
            root,
            status_symbol,
        )
        state.positions_summary = {
            "open_positions": 0,
            "source": "repo-backed audit artifacts",
        }
        state.latest_signal_ids = [
            str(payload.get("signal_id", "") or payload.get("event_id", ""))
            for payload in payloads
            if isinstance(payload, dict)
            and str(payload.get("signal_id", "") or payload.get("event_id", "")).strip()
        ]
        state.last_error = fetch_error
        state.last_health_check = _utc_now_iso()
        runtime_state_path_written = str(write_runtime_state(state, repo_root=root))
    except Exception as exc:
        fetch_error = "; ".join(part for part in (fetch_error, str(exc)) if part)
    result["candidate_count"] = len(payloads)
    result["artifact_dir"] = str(writer.artifact_dir)
    result["fetch_error"] = fetch_error
    result["runtime_state_path"] = runtime_state_path_written
    return result


def build_scheduler(
    *,
    enabled: bool | None = None,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
    repo_root: str | Path | None = None,
    session_id: str | None = None,
    prefer_apscheduler: bool = True,
) -> LocalSchedulerScaffold:
    scheduler = LocalSchedulerScaffold(
        enabled=scheduler_enabled_from_env() if enabled is None else enabled,
        dry_run=True,
        prefer_apscheduler=prefer_apscheduler,
    )
    scheduler.register_interval_job(
        SCHEDULER_JOB_ID,
        lambda: run_watcher_cycle(
            session_id=session_id,
            repo_root=repo_root,
            fetcher=fetcher,
        ),
        seconds=SCHEDULER_INTERVAL_SECONDS,
        metadata={"source": "yfinance_market_feed"},
    )
    return scheduler


def default_engine_factory() -> Any:
    """Construct a brand-new AutoHedge instance (shared default factory)."""
    from autohedge.main import AutoHedge

    return AutoHedge()


def _close_engine(engine: Any | None) -> None:
    """Best-effort close of an engine that may expose a close() method."""
    if engine is None:
        return
    close_fn = getattr(engine, "close", None)
    if callable(close_fn):
        close_fn()


class EphemeralAutoHedgeRunner:
    """Create a FRESH AutoHedge instance for every task (no cross-task state).

    This is the DEFAULT runtime for the CLI/REPL: each task gets its own engine
    so there is no conversation/state bleed between tasks. The engine is closed
    immediately after each task completes.
    """

    def __init__(self, engine_factory: EngineFactory | None = None) -> None:
        self._engine_factory = engine_factory or default_engine_factory
        self._create_count = 0
        self._run_count = 0

    def run_task(self, task: str, *args: Any, **kwargs: Any) -> Any:
        engine = self._engine_factory()
        self._create_count += 1
        self._run_count += 1
        try:
            return engine.run(task=task, *args, **kwargs)
        finally:
            _close_engine(engine)

    def close(self) -> None:
        # Nothing persistent is held between tasks.
        return None

    @property
    def create_count(self) -> int:
        return self._create_count

    @property
    def run_count(self) -> int:
        return self._run_count


class PersistentAutoHedgeEngine:
    """Reuse a single AutoHedge instance across repeated tasks.

    Opt-in only (e.g. CLI `--persist`). NOT the default, because reusing one
    engine across tasks can leak conversation/state between unrelated tasks.
    """

    def __init__(self, engine_factory: EngineFactory | None = None) -> None:
        self._engine_factory = engine_factory or self._default_engine_factory
        self._engine: Any | None = None
        self._lock = Lock()
        self._create_count = 0
        self._run_count = 0

    @staticmethod
    def _default_engine_factory() -> Any:
        return default_engine_factory()

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
        _close_engine(engine)

    @property
    def create_count(self) -> int:
        return self._create_count

    @property
    def run_count(self) -> int:
        return self._run_count


def build_repl_runner(
    *,
    persist: bool = False,
    engine_factory: EngineFactory | None = None,
) -> EphemeralAutoHedgeRunner | PersistentAutoHedgeEngine:
    """Build the runtime backing the REPL.

    Defaults to a fresh-per-task ephemeral runner with no cross-task state.
    Persistent engine reuse is opt-in via ``persist=True``.
    """
    if persist:
        return PersistentAutoHedgeEngine(engine_factory=engine_factory)
    return EphemeralAutoHedgeRunner(engine_factory=engine_factory)


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


def start(
    *,
    run_initial_cycle: bool = True,
    prefer_apscheduler: bool = True,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Register the market watcher job and start only when env-enabled.

    Importing this module has no scheduler side effects. This explicit entry
    point is safe for one-cycle verification and for MCP server startup.
    """
    global _ACTIVE_SCHEDULER

    enabled = scheduler_enabled_from_env()
    scheduler = build_scheduler(
        enabled=enabled,
        fetcher=fetcher,
        prefer_apscheduler=prefer_apscheduler,
    )
    initial_result = None
    if run_initial_cycle:
        initial_result = scheduler.run_pending_once()
    started = scheduler.start()
    if started:
        _ACTIVE_SCHEDULER = scheduler
    return {
        "enabled": enabled,
        "started": started,
        "backend": scheduler.backend,
        "interval_seconds": SCHEDULER_INTERVAL_SECONDS,
        "jobs": [
            {
                "job_id": job.job_id,
                "interval_seconds": job.interval_seconds,
                "dry_run": job.dry_run,
                "metadata": job.metadata,
            }
            for job in scheduler.snapshot_jobs()
        ],
        "initial_result": initial_result,
    }
