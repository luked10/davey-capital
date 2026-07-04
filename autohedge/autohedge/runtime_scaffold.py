"""Persistent engine + scheduler scaffolding for local overnight builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time
from typing import Any, Callable
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


EngineFactory = Callable[[], Any]
JobCallable = Callable[[], Any]
SCHEDULER_INTERVAL_SECONDS = 5 * 60
SCHEDULER_JOB_ID = "tier0-market-feed-watcher"
DAILY_REPORT_JOB_ID = "nova-alpha-daily-report"
DAILY_REPORT_UTC_HOUR = 21
DAILY_REPORT_UTC_MINUTE = 0
POSITION_MONITOR_JOB_ID = "position-monitor"
POSITION_MONITOR_INTERVAL_SECONDS = SCHEDULER_INTERVAL_SECONDS
_ACTIVE_SCHEDULER: Any | None = None
_RUNTIME_STATE_MODULE: Any | None = None
_REPORT_MODULE: Any | None = None
_CIRCUIT_BREAKER_MODULE: Any | None = None
_OBSERVATIONS_MODULE: Any | None = None
_EXIT_PLAN_MODULE: Any | None = None
_REGIME_MODULE: Any | None = None
_TRADE_JOURNAL_MODULE: Any | None = None


def _utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_today_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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


def live_mode_enabled_from_env() -> bool:
    """Execution-mode gate; still separate from real-money Alpaca trading."""
    return os.getenv("DAVEY_LIVE_MODE", "").strip() == "1"


def position_monitor_enabled_from_env() -> bool:
    """Opt-in position monitor gate; disabled unless explicitly set to ``1``."""
    return os.getenv("DAVEY_POSITION_MONITOR_ENABLED", "").strip() == "1"


def _is_us_equity_market_open() -> bool:
    """Check if US equity markets are currently open (9:30 AM - 4:00 PM ET, Mon-Fri)."""
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback if ZoneInfo fails (e.g. missing tzdata)
        return True

    # Weekdays: Mon=0, Fri=4
    if now_et.weekday() > 4:
        return False
    
    current_time = now_et.time()
    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)
    
    # Simple holiday check (matches vibe-trading triggers.py subset)
    today = now_et.date()
    holidays = {
        datetime(2026, 1, 1).date(),   # New Year's
        datetime(2026, 1, 19).date(),  # MLK
        datetime(2026, 2, 16).date(),  # President's
        datetime(2026, 4, 3).date(),   # Good Friday
        datetime(2026, 5, 25).date(),  # Memorial
        datetime(2026, 6, 19).date(),  # Juneteenth
        datetime(2026, 7, 3).date(),   # Independence (obs)
        datetime(2026, 9, 7).date(),   # Labor
        datetime(2026, 11, 26).date(), # Thanksgiving
        datetime(2026, 12, 25).date(), # Christmas
    }
    if today in holidays:
        return False
        
    return market_open <= current_time < market_close


def _load_local_module(name: str, relative_path: str):
    module_path = Path(__file__).resolve().parent / relative_path
    spec = importlib.util.spec_from_file_location(name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_nova_alpha_report_module():
    global _REPORT_MODULE

    if _REPORT_MODULE is not None:
        return _REPORT_MODULE
    module_path = Path(__file__).resolve().parents[2] / "nova-alpha" / "report_scaffold.py"
    spec = importlib.util.spec_from_file_location(
        "davey_runtime_nova_alpha_report",
        str(module_path),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["davey_runtime_nova_alpha_report"] = module
    spec.loader.exec_module(module)
    _REPORT_MODULE = module
    return module


def runtime_state_path(repo_root: str | Path | None = None) -> Path:
    """Return the shared runtime_state.json path for MCP/Poke readers."""
    root = Path(repo_root).expanduser().resolve() if repo_root is not None else _repo_root()
    return root / "runtime_state.json"


def _load_runtime_state_module():
    global _RUNTIME_STATE_MODULE

    if _RUNTIME_STATE_MODULE is not None:
        return _RUNTIME_STATE_MODULE
    try:
        from autohedge.runtime import runtime_state
    except Exception:
        runtime_state = _load_local_module(
            "davey_runtime_state_scaffold",
            "runtime/runtime_state.py",
        )
    _RUNTIME_STATE_MODULE = runtime_state
    return runtime_state


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Atomic JSON write (temp file -> fsync -> os.replace); see runtime_state."""
    return _load_runtime_state_module().atomic_write_json(path, payload)


def write_runtime_state(
    state: Any | None = None,
    *,
    updated_at: str | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    """Write runtime_state.json under DAVEY_ROOT by default."""
    runtime_state = _load_runtime_state_module()
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
    return _load_runtime_state_module().default_runtime_state


def _load_circuit_breaker_helpers():
    global _CIRCUIT_BREAKER_MODULE, _OBSERVATIONS_MODULE

    if _CIRCUIT_BREAKER_MODULE is not None and _OBSERVATIONS_MODULE is not None:
        return (
            _CIRCUIT_BREAKER_MODULE.CircuitBreakerConfig,
            _CIRCUIT_BREAKER_MODULE.evaluate_circuit_breaker,
            _OBSERVATIONS_MODULE.build_observations,
        )
    try:
        from autohedge.risk import circuit_breaker, observations
    except Exception:
        circuit_breaker = _load_local_module(
            "davey_runtime_circuit_breaker",
            "risk/circuit_breaker.py",
        )
        observations = _load_local_module(
            "davey_runtime_observations",
            "risk/observations.py",
        )
    _CIRCUIT_BREAKER_MODULE = circuit_breaker
    _OBSERVATIONS_MODULE = observations
    return (
        circuit_breaker.CircuitBreakerConfig,
        circuit_breaker.evaluate_circuit_breaker,
        observations.build_observations,
    )


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


def _load_exit_plan_module():
    global _EXIT_PLAN_MODULE

    if _EXIT_PLAN_MODULE is not None:
        return _EXIT_PLAN_MODULE
    try:
        from autohedge.runtime import exit_plan
    except Exception:
        exit_plan = _load_local_module(
            "davey_runtime_exit_plan",
            "runtime/exit_plan.py",
        )
    _EXIT_PLAN_MODULE = exit_plan
    return exit_plan


def _load_regime_module():
    global _REGIME_MODULE

    if _REGIME_MODULE is not None:
        return _REGIME_MODULE
    try:
        from autohedge.risk import regime
    except Exception:
        regime = _load_local_module(
            "davey_runtime_regime",
            "risk/regime.py",
        )
    _REGIME_MODULE = regime
    return regime


def _load_trade_journal_module():
    global _TRADE_JOURNAL_MODULE

    if _TRADE_JOURNAL_MODULE is not None:
        return _TRADE_JOURNAL_MODULE
    try:
        from autohedge.audit import trade_journal
    except Exception:
        trade_journal = _load_local_module(
            "davey_runtime_trade_journal",
            "audit/trade_journal.py",
        )
    _TRADE_JOURNAL_MODULE = trade_journal
    return trade_journal


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
    regime_provider: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch local market candidates and hand them to the deterministic watcher.

    The market-hours gate and the network-backed regime snapshot only apply to
    the real market feed (``fetcher is None``); injected fetchers run offline
    and deterministically unless an explicit ``regime_provider`` is supplied.
    When the regime disallows new longs, buy candidates are suppressed with
    reason "regime_suppressed" before they can reach the Poke queue.
    """
    if fetcher is None and not _is_us_equity_market_open():
        print(f"skipping watcher cycle at {datetime.now().isoformat()}: US equity market is closed", flush=True)
        return {"status": "skipped", "reason": "market_closed"}

    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    clean_session_id = (
        session_id
        or os.getenv("DAVEY_SESSION_ID", "").strip()
        or "scheduler"
    )
    clean_run_id = run_id or f"scheduler-{_utc_now_compact()}"
    print(
        f"scheduler cycle start: run_id={clean_run_id} session_id={clean_session_id} root={root}",
        flush=True,
    )
    fetch_candidates = fetcher or _load_fetch_candidates()
    DeterministicTier0Watcher, OvernightArtifactWriter = _load_watcher_classes()

    # Regime check at the start of every cycle (spec: Module 4). Offline runs
    # with an injected fetcher skip the network fetch unless a provider is
    # injected too.
    regime_snapshot: dict[str, Any] | None = None
    try:
        if regime_provider is not None:
            regime_snapshot = regime_provider()
        elif fetcher is None:
            regime_snapshot = _load_regime_module().get_regime_snapshot()
    except Exception as exc:
        print(f"regime snapshot failed safely: {exc}", flush=True)
        regime_snapshot = None
    if regime_snapshot is not None:
        try:
            _load_trade_journal_module().TradeJournal(davey_root=root).record_event(
                event_type="regime_checked",
                symbol="SPY",
                payload={"regime": regime_snapshot, "run_id": clean_run_id},
            )
        except Exception as exc:
            print(f"regime journal write failed safely: {exc}", flush=True)

    fetch_error = ""
    try:
        payloads = fetch_candidates()
    except Exception as exc:
        fetch_error = str(exc)
        payloads = []

    regime_suppressed_count = 0
    if (
        isinstance(regime_snapshot, dict)
        and regime_snapshot.get("allow_new_longs") is False
    ):
        kept_payloads = []
        for payload in payloads:
            if (
                isinstance(payload, dict)
                and str(payload.get("side", "")).strip().lower() == "buy"
            ):
                regime_suppressed_count += 1
                print(
                    "regime_suppressed: dropping buy candidate "
                    f"{payload.get('symbol')!r} ({regime_snapshot.get('regime')}: "
                    f"{regime_snapshot.get('reason')})",
                    flush=True,
                )
            else:
                kept_payloads.append(payload)
        payloads = kept_payloads
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
        runtime_state = _load_runtime_state_module()
        state = runtime_state.load_runtime_state_or_default(runtime_state_path(root))
        state.live_mode = live_mode_enabled_from_env()
        state.dry_run = not state.live_mode
        state.active_broker = "alpaca" if state.live_mode else "paper"
        state.circuit_breaker_status = _scheduler_circuit_breaker_status(
            root,
            status_symbol,
        )
        # Preserve position/cooldown bookkeeping owned by the position monitor.
        previous_summary = (
            state.positions_summary if isinstance(state.positions_summary, dict) else {}
        )
        positions = dict(previous_summary.get("positions") or {})
        state.positions_summary = {
            "open_positions": len(positions),
            "source": "repo-backed audit artifacts",
            "positions": positions,
            "cooldowns": dict(previous_summary.get("cooldowns") or {}),
        }
        if regime_snapshot is not None:
            state.positions_summary["regime"] = regime_snapshot
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
    result["regime"] = regime_snapshot
    result["regime_suppressed_count"] = regime_suppressed_count
    print(
        "scheduler cycle complete: "
        f"run_id={clean_run_id} candidates={len(payloads)} "
        f"runtime_state_path={runtime_state_path_written} error={fetch_error!r}",
        flush=True,
    )
    return result


def _default_price_fetcher(symbol: str) -> float | None:
    """Best-effort last price from yfinance; None on any failure."""
    try:
        import yfinance as yf

        price = float(yf.Ticker(symbol).fast_info["last_price"])
        return price if price == price and price > 0 else None
    except Exception:
        return None


def _journal_event_safe(
    journal: Any,
    *,
    event_type: str,
    symbol: str,
    payload: dict[str, Any],
    side: str | None = None,
) -> None:
    if journal is None:
        return
    try:
        journal.record_event(
            event_type=event_type,
            symbol=symbol,
            payload=payload,
            side=side,
        )
    except Exception as exc:
        print(f"trade journal write failed safely ({event_type}): {exc}", flush=True)


def _inject_exit_sell_candidate(
    *,
    root: Path,
    symbol: str,
    action: str,
    plan_dict: dict[str, Any],
    position: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    """Queue a sell candidate for Poke approval (TP1 / trailing / time stop).

    This is the approval path: nothing executes until a human approves. Only
    the hard stop bypasses this queue.
    """
    DeterministicTier0Watcher, OvernightArtifactWriter = _load_watcher_classes()
    writer = OvernightArtifactWriter(
        session_id=session_id,
        artifact_root=root / "logs" / "overnight",
    )
    watcher = DeterministicTier0Watcher(
        run_id=f"position-monitor-{_utc_now_compact()}",
        writer=writer,
        dry_run=True,
        enable_poke_handoff=True,
    )
    return watcher.process_payload(
        {
            "symbol": symbol,
            "side": "sell",
            "confidence": 0.95,
            "strategy": "exit_plan_monitor",
            "source": "position_monitor",
            "dry_run": True,
            "metadata": {
                "exit_action": action,
                "exit_reason": plan_dict.get("last_reason", ""),
                "exit_state": plan_dict.get("state", ""),
                "entry_price": position.get("entry_price"),
                "original_thesis": position.get("original_thesis", ""),
                "original_handoff_id": position.get("original_handoff_id", ""),
            },
        }
    )


def run_position_monitor(
    *,
    repo_root: str | Path | None = None,
    session_id: str | None = None,
    price_fetcher: Callable[[str], float | None] | None = None,
    journal: Any | None = None,
    as_of: Any | None = None,
) -> dict[str, Any]:
    """Evaluate every open position's exit plan against current prices.

    Actions per position:
      exit_stop      -> the ONLY no-approval path: journal stop_hit, close the
                        position record immediately, apply a 2-trading-day
                        cooldown. Live broker submission (when enabled) is
                        routed through the approval-exempt hard-stop record;
                        this scaffold never places orders itself.
      take_profit_1 /
      exit_trailing  -> inject a sell candidate into the Poke queue for human
                        approval; the position record stays until it closes.
      hold           -> refresh peak_price in runtime_state.json.

    All writes to runtime_state.json are atomic. Prices come from the injected
    ``price_fetcher`` (offline smokes) or yfinance (runtime).
    """
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    clean_session_id = (
        session_id
        or os.getenv("DAVEY_SESSION_ID", "").strip()
        or "position-monitor"
    )
    fetch_price = price_fetcher or _default_price_fetcher
    exit_plan_module = _load_exit_plan_module()
    runtime_state = _load_runtime_state_module()

    if journal is None:
        try:
            journal = _load_trade_journal_module().TradeJournal(davey_root=root)
        except Exception as exc:
            print(f"trade journal unavailable (non-blocking): {exc}", flush=True)
            journal = None

    state = runtime_state.load_runtime_state_or_default(runtime_state_path(root))
    summary = state.positions_summary if isinstance(state.positions_summary, dict) else {}
    positions: dict[str, Any] = dict(summary.get("positions") or {})
    cooldowns: dict[str, Any] = dict(summary.get("cooldowns") or {})

    actions: list[dict[str, Any]] = []
    for symbol in sorted(positions):
        position = positions[symbol]
        if not isinstance(position, dict):
            actions.append({"symbol": symbol, "action": "skipped", "reason": "malformed position entry"})
            continue
        try:
            plan = exit_plan_module.ExitPlan.from_dict(position.get("exit_plan") or {})
        except (ValueError, KeyError) as exc:
            actions.append({"symbol": symbol, "action": "skipped", "reason": f"malformed exit plan: {exc}"})
            continue
        if plan.state is exit_plan_module.ExitState.EXITED:
            actions.append({"symbol": symbol, "action": "skipped", "reason": "already exited"})
            continue
        price = fetch_price(symbol)
        if price is None:
            actions.append({"symbol": symbol, "action": "skipped", "reason": "no price available"})
            continue

        action = plan.update(price, as_of=as_of)
        plan_dict = plan.to_dict()
        position["exit_plan"] = plan_dict
        record = {
            "symbol": symbol,
            "action": action,
            "price": price,
            "unrealized_pnl_pct": round(plan.unrealized_pnl_pct(price), 4),
            "reason": plan.last_reason,
            "state": plan_dict["state"],
        }

        if action == "exit_stop":
            # Hard stop: immediate, no Poke approval — journal + audit + close.
            _journal_event_safe(
                journal,
                event_type="stop_hit",
                symbol=symbol,
                side="sell",
                payload={**record, "approval_bypassed": True, "path": "hard_stop"},
            )
            _journal_event_safe(
                journal,
                event_type="exit_triggered",
                symbol=symbol,
                side="sell",
                payload={**record, "requires_approval": False},
            )
            _journal_event_safe(
                journal,
                event_type="closed",
                symbol=symbol,
                side="sell",
                payload={**record, "closed_by": "position_monitor_hard_stop"},
            )
            cooldowns[symbol] = {
                "until": exit_plan_module.cooldown_until(reason="stop_out"),
                "reason": "stop_out",
            }
            del positions[symbol]
        elif action in ("take_profit_1", "exit_trailing"):
            event_type = "take_profit" if action == "take_profit_1" else "trailing_exit"
            _journal_event_safe(
                journal,
                event_type=event_type,
                symbol=symbol,
                side="sell",
                payload={**record, "requires_approval": True},
            )
            _journal_event_safe(
                journal,
                event_type="exit_triggered",
                symbol=symbol,
                side="sell",
                payload={**record, "requires_approval": True},
            )
            try:
                injection = _inject_exit_sell_candidate(
                    root=root,
                    symbol=symbol,
                    action=action,
                    plan_dict=plan_dict,
                    position=position,
                    session_id=clean_session_id,
                )
                record["handoff"] = injection
            except Exception as exc:
                record["handoff"] = {"status": "error", "reason": str(exc)}
        actions.append(record)

    state.positions_summary = {
        **summary,
        "open_positions": len(positions),
        "positions": positions,
        "cooldowns": cooldowns,
        "last_position_monitor_at": _utc_now_iso(),
    }
    state.last_health_check = _utc_now_iso()
    written_path = ""
    try:
        written_path = str(
            runtime_state.save_runtime_state(state, runtime_state_path(root))
        )
    except Exception as exc:
        print(f"position monitor state write failed safely: {exc}", flush=True)

    result = {
        "status": "ok",
        "checked": len(actions),
        "open_positions": len(positions),
        "actions": actions,
        "runtime_state_path": written_path,
    }
    print(
        f"position monitor complete: checked={len(actions)} open={len(positions)}",
        flush=True,
    )
    return result


def run_daily_report(
    *,
    repo_root: str | Path | None = None,
    report_date: str | None = None,
) -> dict[str, Any]:
    """Render and persist the local daily report, then stamp runtime state."""
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    clean_date = report_date or _utc_today_date()
    report_module = _load_nova_alpha_report_module()
    output_path = root / "reports" / f"daily_{clean_date}.md"
    last_report_at = _utc_now_iso()
    error = ""

    try:
        artifacts = report_module.load_local_artifacts(
            root,
            today_only=True,
            report_date=clean_date,
        )
        report = report_module.render_daily_report(
            artifacts,
            report_date=clean_date,
            today_only=True,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except Exception as exc:
        error = str(exc)

    runtime_state_path_written = ""
    try:
        runtime_state = _load_runtime_state_module()
        load_runtime_state = runtime_state.load_runtime_state
        default_runtime_state = runtime_state.default_runtime_state
        save_runtime_state = runtime_state.save_runtime_state

        result = load_runtime_state(runtime_state_path(root))
        state = result.state if result.ok and result.state is not None else default_runtime_state()
        state.live_mode = live_mode_enabled_from_env()
        state.dry_run = not state.live_mode
        state.active_broker = "alpaca" if state.live_mode else "paper"
        state.last_report_at = last_report_at
        if error:
            state.last_error = error
        else:
            state.last_error = ""
        runtime_state_path_written = str(
            save_runtime_state(
                state,
                runtime_state_path(root),
                updated_at=last_report_at,
            )
        )
    except Exception as exc:
        error = "; ".join(part for part in (error, str(exc)) if part)

    return {
        "ok": error == "",
        "report_path": str(output_path) if output_path.exists() else "",
        "report_date": clean_date,
        "last_report_at": last_report_at,
        "runtime_state_path": runtime_state_path_written,
        "error": error,
    }


def build_scheduler(
    *,
    enabled: bool | None = None,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
    repo_root: str | Path | None = None,
    session_id: str | None = None,
    prefer_apscheduler: bool = True,
    position_monitor_enabled: bool | None = None,
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
    scheduler.register_daily_utc_job(
        DAILY_REPORT_JOB_ID,
        lambda: run_daily_report(repo_root=repo_root),
        hour=DAILY_REPORT_UTC_HOUR,
        minute=DAILY_REPORT_UTC_MINUTE,
        metadata={"source": "nova_alpha_report", "output": "reports/daily_{date}.md"},
    )
    # Position monitor: same 5-minute cadence as tier0. Opt-in via
    # DAVEY_POSITION_MONITOR_ENABLED=1 (or explicit flag) so default scheduler
    # shape stays unchanged for existing deterministic smokes.
    if (
        position_monitor_enabled
        if position_monitor_enabled is not None
        else position_monitor_enabled_from_env()
    ):
        scheduler.register_interval_job(
            POSITION_MONITOR_JOB_ID,
            lambda: run_position_monitor(
                repo_root=repo_root,
                session_id=session_id,
            ),
            seconds=POSITION_MONITOR_INTERVAL_SECONDS,
            metadata={"source": "exit_plan_monitor"},
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
    schedule: str = "interval"
    utc_hour: int | None = None
    utc_minute: int | None = None


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
        self._stop_event = Event()
        self._thread: Thread | None = None
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

    def register_daily_utc_job(
        self,
        job_id: str,
        func: JobCallable,
        *,
        hour: int,
        minute: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerJob:
        clean_job_id = str(job_id or "").strip()
        if not clean_job_id:
            raise ValueError("job_id must be non-empty")
        if not callable(func):
            raise ValueError("func must be callable")
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("hour/minute must be a valid UTC time")

        job = SchedulerJob(
            job_id=clean_job_id,
            interval_seconds=24 * 60 * 60,
            dry_run=self.dry_run,
            metadata=dict(metadata or {}),
            schedule="daily_utc",
            utc_hour=hour,
            utc_minute=minute,
        )
        self._jobs[clean_job_id] = (job, func)

        if self._scheduler is not None:
            self._scheduler.add_job(
                func=func,
                trigger="cron",
                hour=hour,
                minute=minute,
                timezone="UTC",
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
            print(f"scheduler job start: job_id={job.job_id}", flush=True)
            result = func()
            print(f"scheduler job complete: job_id={job.job_id}", flush=True)
            executed.append(
                {
                    "job_id": job.job_id,
                    "dry_run": job.dry_run,
                    "result": result,
                }
            )
        return executed

    def _run_stdlib_loop(self) -> None:
        next_run_by_job: dict[str, float] = {}
        now = time.monotonic()
        for job, _ in self._jobs.values():
            next_run_by_job[job.job_id] = now + job.interval_seconds

        while not self._stop_event.wait(1.0):
            now = time.monotonic()
            for job, func in list(self._jobs.values()):
                next_run = next_run_by_job.get(job.job_id, now)
                if now < next_run:
                    continue
                try:
                    print(f"scheduler job start: job_id={job.job_id}", flush=True)
                    func()
                    print(f"scheduler job complete: job_id={job.job_id}", flush=True)
                except Exception as exc:
                    print(
                        f"scheduler job failed safely: job_id={job.job_id} error={exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                finally:
                    next_run_by_job[job.job_id] = now + job.interval_seconds

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._running:
            return True
        if self._scheduler is not None:
            self._scheduler.start()
        else:
            self._stop_event.clear()
            self._thread = Thread(
                target=self._run_stdlib_loop,
                name="davey-stdlib-scheduler",
                daemon=True,
            )
            self._thread.start()
        self._running = True
        return True

    def stop(self) -> None:
        if self._scheduler is not None and self._running:
            self._scheduler.shutdown(wait=False)
        if self._thread is not None and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=2)
        self._thread = None
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
                "schedule": job.schedule,
                "utc_hour": job.utc_hour,
                "utc_minute": job.utc_minute,
                "dry_run": job.dry_run,
                "metadata": job.metadata,
            }
            for job in scheduler.snapshot_jobs()
        ],
        "initial_result": initial_result,
    }
