"""Local-only overnight scaffolding contracts for build-order #3.

This module intentionally contains deterministic, dry-run-safe payloads for
watcher candidates, NEEDS_HUMAN escalations, and poke bridge handoff records.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any


OVERNIGHT_SCAFFOLD_VERSION = "0.1.0"


@dataclass(slots=True)
class CandidateEvent:
    event_id: str
    run_id: str
    created_at: str
    symbol: str
    side: str
    confidence: float
    source: str = "tier0_watcher_scaffold"
    strategy: str = ""
    dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NeedsHumanEvent:
    needs_human_id: str
    run_id: str
    created_at: str
    reason_code: str
    reason: str
    source_event_id: str = ""
    dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PokeBridgeHandoff:
    handoff_id: str
    run_id: str
    created_at: str
    candidate_event_id: str
    destination: str = "poke_bridge_local_queue"
    dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


def to_dict(instance: Any) -> dict[str, Any]:
    return asdict(instance)


def to_json(instance: Any, *, indent: int | None = 2) -> str:
    return json.dumps(to_dict(instance), ensure_ascii=False, indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Schema validation (fail-closed, local-only).
#
# These validators harden the JSONL payload schemas written by the watcher
# scaffold. They accept raw dict payloads, apply explicit safe coercions
# (case normalization, numeric parsing), and fail closed on anything
# ambiguous: missing keys, wrong types, non-strict booleans, or non-local
# poke destinations. They never perform network or broker calls.
# ---------------------------------------------------------------------------

LOCAL_POKE_DESTINATION = "poke_bridge_local_queue"

CANDIDATE_EVENT_REQUIRED_KEYS = (
    "event_id",
    "run_id",
    "created_at",
    "symbol",
    "side",
    "confidence",
)
NEEDS_HUMAN_EVENT_REQUIRED_KEYS = (
    "needs_human_id",
    "run_id",
    "created_at",
    "reason_code",
    "reason",
)
POKE_HANDOFF_REQUIRED_KEYS = (
    "handoff_id",
    "run_id",
    "created_at",
    "candidate_event_id",
)


@dataclass(slots=True)
class SchemaValidationResult:
    valid: bool
    reasons: tuple[str, ...] = ()
    normalized: dict[str, Any] | None = None

    def require_valid(self) -> dict[str, Any]:
        if not self.valid or self.normalized is None:
            reason_text = "; ".join(self.reasons) if self.reasons else "schema validation failed"
            raise ValueError(f"Payload failed schema validation: {reason_text}")
        return self.normalized


def _schema_clean_text(value: Any) -> str:
    return str(value or "").strip()


def _require_payload_dict(payload: Any, reasons: list[str]) -> bool:
    if not isinstance(payload, dict):
        reasons.append(f"payload must be a dict, got {type(payload).__name__}")
        return False
    return True


def _require_keys(payload: dict[str, Any], required: tuple[str, ...], reasons: list[str]) -> None:
    for key in required:
        if key not in payload:
            reasons.append(f"missing required key: {key}")


def _require_nonempty_str(payload: dict[str, Any], key: str, reasons: list[str]) -> str:
    raw = payload.get(key)
    if raw is not None and not isinstance(raw, str):
        reasons.append(f"{key} must be a string")
        return ""
    cleaned = _schema_clean_text(raw)
    if key in payload and not cleaned:
        reasons.append(f"{key} must be non-empty")
    return cleaned


def _strict_schema_bool(payload: dict[str, Any], key: str, reasons: list[str], *, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if not isinstance(value, bool):
        reasons.append(f"{key} must be boolean")
        return default
    return value


def _schema_metadata(payload: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    raw = payload.get("metadata")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        reasons.append("metadata must be a dict when provided")
        return {}
    return dict(raw)


def validate_candidate_event_payload(payload: Any) -> SchemaValidationResult:
    """Validate a candidate event payload. Fails closed on malformed input."""
    reasons: list[str] = []
    if not _require_payload_dict(payload, reasons):
        return SchemaValidationResult(valid=False, reasons=tuple(reasons))

    _require_keys(payload, CANDIDATE_EVENT_REQUIRED_KEYS, reasons)

    event_id = _require_nonempty_str(payload, "event_id", reasons)
    run_id = _require_nonempty_str(payload, "run_id", reasons)
    created_at = _require_nonempty_str(payload, "created_at", reasons)
    symbol = _require_nonempty_str(payload, "symbol", reasons).upper()

    side_raw = payload.get("side")
    side = _schema_clean_text(side_raw).lower() if isinstance(side_raw, str) else ""
    if "side" in payload and side not in {"buy", "sell"}:
        reasons.append(f"side must be buy/sell, got {side_raw!r}")

    confidence: float | None = None
    if "confidence" in payload:
        confidence_raw = payload.get("confidence")
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float, str)):
            reasons.append("confidence must be a number")
        else:
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                reasons.append("confidence must be a number")
        if confidence is not None and not (0.0 <= confidence <= 1.0):
            reasons.append("confidence must be within [0, 1]")
            confidence = None

    dry_run = _strict_schema_bool(payload, "dry_run", reasons, default=True)
    metadata = _schema_metadata(payload, reasons)
    source = _schema_clean_text(payload.get("source")) or "tier0_watcher_scaffold"
    strategy = _schema_clean_text(payload.get("strategy"))

    if reasons:
        return SchemaValidationResult(valid=False, reasons=tuple(reasons))

    normalized = {
        "event_id": event_id,
        "run_id": run_id,
        "created_at": created_at,
        "symbol": symbol,
        "side": side,
        "confidence": confidence,
        "source": source,
        "strategy": strategy,
        "dry_run": dry_run,
        "metadata": metadata,
    }
    return SchemaValidationResult(valid=True, normalized=normalized)


def validate_needs_human_event_payload(payload: Any) -> SchemaValidationResult:
    """Validate a NEEDS_HUMAN event payload. Fails closed on malformed input."""
    reasons: list[str] = []
    if not _require_payload_dict(payload, reasons):
        return SchemaValidationResult(valid=False, reasons=tuple(reasons))

    _require_keys(payload, NEEDS_HUMAN_EVENT_REQUIRED_KEYS, reasons)

    needs_human_id = _require_nonempty_str(payload, "needs_human_id", reasons)
    run_id = _require_nonempty_str(payload, "run_id", reasons)
    created_at = _require_nonempty_str(payload, "created_at", reasons)
    reason_code = _require_nonempty_str(payload, "reason_code", reasons)
    reason = _require_nonempty_str(payload, "reason", reasons)

    source_event_id_raw = payload.get("source_event_id", "")
    if not isinstance(source_event_id_raw, str):
        reasons.append("source_event_id must be a string")
        source_event_id = ""
    else:
        source_event_id = _schema_clean_text(source_event_id_raw)

    dry_run = _strict_schema_bool(payload, "dry_run", reasons, default=True)
    metadata = _schema_metadata(payload, reasons)

    if reasons:
        return SchemaValidationResult(valid=False, reasons=tuple(reasons))

    normalized = {
        "needs_human_id": needs_human_id,
        "run_id": run_id,
        "created_at": created_at,
        "reason_code": reason_code,
        "reason": reason,
        "source_event_id": source_event_id,
        "dry_run": dry_run,
        "metadata": metadata,
    }
    return SchemaValidationResult(valid=True, normalized=normalized)


def validate_poke_handoff_payload(payload: Any) -> SchemaValidationResult:
    """Validate a local poke queue payload. Fails closed on malformed input.

    Destination is pinned to the local queue: any other destination (for
    example an external SMS bridge) fails validation. This keeps poke
    delivery local-only at the schema layer.
    """
    reasons: list[str] = []
    if not _require_payload_dict(payload, reasons):
        return SchemaValidationResult(valid=False, reasons=tuple(reasons))

    _require_keys(payload, POKE_HANDOFF_REQUIRED_KEYS, reasons)

    handoff_id = _require_nonempty_str(payload, "handoff_id", reasons)
    run_id = _require_nonempty_str(payload, "run_id", reasons)
    created_at = _require_nonempty_str(payload, "created_at", reasons)
    candidate_event_id = _require_nonempty_str(payload, "candidate_event_id", reasons)

    destination_raw = payload.get("destination", LOCAL_POKE_DESTINATION)
    if not isinstance(destination_raw, str):
        reasons.append("destination must be a string")
        destination = ""
    else:
        destination = _schema_clean_text(destination_raw)
    if destination != LOCAL_POKE_DESTINATION:
        reasons.append(
            f"destination must be {LOCAL_POKE_DESTINATION!r} (local-only), got {destination_raw!r}"
        )

    dry_run = _strict_schema_bool(payload, "dry_run", reasons, default=True)
    metadata = _schema_metadata(payload, reasons)

    if reasons:
        return SchemaValidationResult(valid=False, reasons=tuple(reasons))

    normalized = {
        "handoff_id": handoff_id,
        "run_id": run_id,
        "created_at": created_at,
        "candidate_event_id": candidate_event_id,
        "destination": destination,
        "dry_run": dry_run,
        "metadata": metadata,
    }
    return SchemaValidationResult(valid=True, normalized=normalized)
