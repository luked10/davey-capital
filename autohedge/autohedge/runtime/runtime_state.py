"""Machine-readable runtime state scaffold (repo-backed shared memory).

Pure read/write helpers around a runtime_state.json file intended as shared
machine-readable memory for future Poke/Claude/Cursor tooling. This module
performs NO live account reads, NO broker calls, and NO network access; all
values are caller-supplied summaries of repo-backed artifacts.

Malformed state fails closed: loads return a NEEDS_HUMAN-style result instead
of partially-parsed state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


RUNTIME_STATE_SCAFFOLD_VERSION = "0.1.0"

RUNTIME_STATE_REQUIRED_KEYS = (
    "active_broker",
    "dry_run",
    "live_mode",
    "positions_summary",
    "latest_signal_ids",
    "circuit_breaker_status",
    "last_report_at",
    "last_error",
    "last_health_check",
    "updated_at",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class RuntimeState:
    active_broker: str = "paper"
    dry_run: bool = True
    live_mode: bool = False
    positions_summary: dict[str, Any] = field(default_factory=dict)
    latest_signal_ids: list[str] = field(default_factory=list)
    circuit_breaker_status: str = "disabled"
    last_report_at: str = ""
    last_error: str = ""
    last_health_check: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class RuntimeStateLoadResult:
    ok: bool
    needs_human: bool
    reasons: tuple[str, ...] = ()
    state: RuntimeState | None = None

    def require_state(self) -> RuntimeState:
        if not self.ok or self.state is None:
            reason_text = "; ".join(self.reasons) if self.reasons else "runtime state load failed"
            raise ValueError(f"Runtime state unavailable: {reason_text}")
        return self.state


def default_runtime_state(*, updated_at: str | None = None) -> RuntimeState:
    """Safe default state: paper broker, dry_run=True, live_mode=False."""
    return RuntimeState(updated_at=updated_at if updated_at is not None else _utc_now_iso())


def runtime_state_to_dict(state: RuntimeState) -> dict[str, Any]:
    return asdict(state)


def _validate_state_payload(payload: Any, reasons: list[str]) -> RuntimeState | None:
    if not isinstance(payload, dict):
        reasons.append(f"runtime state must be a JSON object, got {type(payload).__name__}")
        return None

    for key in RUNTIME_STATE_REQUIRED_KEYS:
        if key not in payload:
            reasons.append(f"missing required key: {key}")

    active_broker = payload.get("active_broker")
    if "active_broker" in payload and (not isinstance(active_broker, str) or not active_broker.strip()):
        reasons.append("active_broker must be a non-empty string")

    dry_run = payload.get("dry_run")
    if "dry_run" in payload and not isinstance(dry_run, bool):
        reasons.append("dry_run must be boolean")

    live_mode = payload.get("live_mode")
    if "live_mode" in payload and not isinstance(live_mode, bool):
        reasons.append("live_mode must be boolean")

    positions_summary = payload.get("positions_summary")
    if "positions_summary" in payload and not isinstance(positions_summary, dict):
        reasons.append("positions_summary must be a dict")

    latest_signal_ids = payload.get("latest_signal_ids")
    if "latest_signal_ids" in payload:
        if not isinstance(latest_signal_ids, list) or not all(
            isinstance(item, str) for item in latest_signal_ids
        ):
            reasons.append("latest_signal_ids must be a list of strings")

    circuit_breaker_status = payload.get("circuit_breaker_status")
    if "circuit_breaker_status" in payload:
        if circuit_breaker_status not in {"normal", "blocked", "disabled"}:
            reasons.append(
                "circuit_breaker_status must be one of: normal, blocked, disabled"
            )

    for key in ("last_report_at", "last_error", "last_health_check", "updated_at"):
        if key in payload and not isinstance(payload.get(key), str):
            reasons.append(f"{key} must be a string")

    if reasons:
        return None

    return RuntimeState(
        active_broker=active_broker.strip(),
        dry_run=dry_run,
        live_mode=live_mode,
        positions_summary=dict(positions_summary),
        latest_signal_ids=list(latest_signal_ids),
        circuit_breaker_status=circuit_breaker_status,
        last_report_at=payload["last_report_at"],
        last_error=payload["last_error"],
        last_health_check=payload["last_health_check"],
        updated_at=payload["updated_at"],
    )


def load_runtime_state(path: str | Path) -> RuntimeStateLoadResult:
    """Load and validate runtime state from disk. Fails closed when malformed."""
    state_path = Path(path)
    reasons: list[str] = []

    if not state_path.exists():
        return RuntimeStateLoadResult(
            ok=False,
            needs_human=True,
            reasons=(f"runtime state file not found: {state_path}",),
        )

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return RuntimeStateLoadResult(
            ok=False,
            needs_human=True,
            reasons=(f"runtime state is not valid JSON: {exc}",),
        )

    state = _validate_state_payload(payload, reasons)
    if state is None:
        return RuntimeStateLoadResult(ok=False, needs_human=True, reasons=tuple(reasons))
    return RuntimeStateLoadResult(ok=True, needs_human=False, state=state)


def save_runtime_state(
    state: RuntimeState,
    path: str | Path,
    *,
    updated_at: str | None = None,
) -> Path:
    """Validate and write runtime state to disk. Fails closed when malformed.

    ``updated_at`` may be passed explicitly for deterministic tests; otherwise
    the current UTC timestamp is stamped at write time.
    """
    if not isinstance(state, RuntimeState):
        raise ValueError("save_runtime_state requires a RuntimeState instance")

    payload = runtime_state_to_dict(state)
    payload["updated_at"] = updated_at if updated_at is not None else _utc_now_iso()

    reasons: list[str] = []
    if _validate_state_payload(payload, reasons) is None:
        raise ValueError("refusing to write malformed runtime state: " + "; ".join(reasons))

    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return state_path
