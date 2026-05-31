"""Shared bridge contract models and JSON helpers.

This module defines the canonical payload types used between signal
generation, risk review, and execution wiring. It is intentionally
dataclass-based for lightweight interoperability across modules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
from typing import Any


BRIDGE_CONTRACT_VERSION = "1.0.0"


@dataclass(slots=True)
class SignalPayload:
    signal_id: str
    symbol: str
    side: str
    confidence: float
    generated_at: str
    strategy: str = ""
    timeframe: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskSummary:
    risk_id: str
    signal_id: str
    risk_score: float
    max_position_size: float
    max_notional: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    needs_human: bool = False
    needs_human_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionIntent:
    intent_id: str
    signal_id: str
    broker: str
    symbol: str
    side: str
    quantity: float
    created_at: str
    order_type: str = "market"
    limit_price: float | None = None
    time_in_force: str = "day"
    dry_run: bool = True
    approved: bool = False
    approved_by: str = ""
    approved_at: str = ""
    status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FillRecord:
    fill_id: str
    intent_id: str
    broker: str
    order_id: str
    symbol: str
    side: str
    quantity: float
    filled_at: str
    price: float | None = None
    fee: float | None = None
    status: str = "filled"
    dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunMetadata:
    run_id: str
    source_system: str
    created_at: str
    bridge_contract_version: str = BRIDGE_CONTRACT_VERSION
    dry_run: bool = True
    needs_human: bool = False
    needs_human_reason: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BridgeEnvelope:
    run: RunMetadata
    signal: SignalPayload
    risk: RiskSummary
    intent: ExecutionIntent
    fill: FillRecord | None = None


@dataclass(slots=True)
class ExecutionValidationResult:
    allowed: bool
    needs_human: bool
    status: str
    reasons: tuple[str, ...] = ()
    normalized_intent: ExecutionIntent | None = None

    def require_allowed(self) -> ExecutionIntent:
        if not self.allowed or self.normalized_intent is None:
            reason_text = "; ".join(self.reasons) if self.reasons else "validation failed"
            raise ValueError(f"Execution intent blocked: {reason_text}")
        return self.normalized_intent


def _load_json_object(raw_json: str) -> dict[str, Any]:
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


def to_dict(instance: Any) -> dict[str, Any]:
    return asdict(instance)


def to_json(instance: Any, *, indent: int | None = 2) -> str:
    return json.dumps(to_dict(instance), ensure_ascii=False, indent=indent, sort_keys=True)


def signal_payload_from_dict(payload: dict[str, Any]) -> SignalPayload:
    return SignalPayload(**payload)


def risk_summary_from_dict(payload: dict[str, Any]) -> RiskSummary:
    return RiskSummary(**payload)


def execution_intent_from_dict(payload: dict[str, Any]) -> ExecutionIntent:
    return ExecutionIntent(**payload)


def fill_record_from_dict(payload: dict[str, Any]) -> FillRecord:
    return FillRecord(**payload)


def run_metadata_from_dict(payload: dict[str, Any]) -> RunMetadata:
    return RunMetadata(**payload)


def bridge_envelope_from_dict(payload: dict[str, Any]) -> BridgeEnvelope:
    fill_payload = payload.get("fill")
    return BridgeEnvelope(
        run=run_metadata_from_dict(payload["run"]),
        signal=signal_payload_from_dict(payload["signal"]),
        risk=risk_summary_from_dict(payload["risk"]),
        intent=execution_intent_from_dict(payload["intent"]),
        fill=fill_record_from_dict(fill_payload) if isinstance(fill_payload, dict) else None,
    )


def signal_payload_from_json(raw_json: str) -> SignalPayload:
    return signal_payload_from_dict(_load_json_object(raw_json))


def risk_summary_from_json(raw_json: str) -> RiskSummary:
    return risk_summary_from_dict(_load_json_object(raw_json))


def execution_intent_from_json(raw_json: str) -> ExecutionIntent:
    return execution_intent_from_dict(_load_json_object(raw_json))


def fill_record_from_json(raw_json: str) -> FillRecord:
    return fill_record_from_dict(_load_json_object(raw_json))


def run_metadata_from_json(raw_json: str) -> RunMetadata:
    return run_metadata_from_dict(_load_json_object(raw_json))


def bridge_envelope_from_json(raw_json: str) -> BridgeEnvelope:
    return bridge_envelope_from_dict(_load_json_object(raw_json))


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _positive_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_execution_intent(
    intent: ExecutionIntent,
    risk: RiskSummary | None = None,
    run: RunMetadata | None = None,
) -> ExecutionValidationResult:
    reasons: list[str] = []
    needs_human = False

    broker = _clean_text(intent.broker).lower()
    symbol = _clean_text(intent.symbol).upper()
    side = _clean_text(intent.side).lower()
    order_type = _clean_text(intent.order_type).lower() or "market"

    if not broker:
        reasons.append("missing broker")
    if not symbol:
        reasons.append("missing symbol")
    if side not in {"buy", "sell"}:
        reasons.append(f"invalid side: {intent.side!r}")
    if order_type not in {"market", "limit"}:
        reasons.append(f"invalid order_type: {intent.order_type!r}")

    quantity = _positive_float(intent.quantity)
    if quantity is None or quantity <= 0:
        reasons.append("quantity must be positive")

    metadata = dict(intent.metadata or {})
    if "notional" in metadata:
        notional = _positive_float(metadata.get("notional"))
        if notional is None or notional <= 0:
            reasons.append("notional must be positive when provided")

    if order_type == "limit":
        limit_price = _positive_float(intent.limit_price)
        if limit_price is None or limit_price <= 0:
            reasons.append("limit_price must be positive for limit order")

    if not intent.dry_run:
        if not intent.approved:
            reasons.append("dry_run=False requires approved=True")
        if not _clean_text(intent.approved_by):
            reasons.append("dry_run=False requires approved_by")
        if not _clean_text(intent.approved_at):
            reasons.append("dry_run=False requires approved_at")

    if intent.approved and (not _clean_text(intent.approved_by) or not _clean_text(intent.approved_at)):
        reasons.append("approved intents must include approved_by and approved_at")

    if risk is not None and risk.needs_human:
        needs_human = True
        reasons.append(
            f"risk requires NEEDS_HUMAN: {_clean_text(risk.needs_human_reason) or 'unspecified'}"
        )
    if run is not None and run.needs_human:
        needs_human = True
        reasons.append(
            f"run requires NEEDS_HUMAN: {_clean_text(run.needs_human_reason) or 'unspecified'}"
        )

    status = "ok"
    if reasons:
        status = "needs_human" if needs_human else "blocked"

    normalized_intent: ExecutionIntent | None = None
    if not reasons:
        normalized_intent = replace(
            intent,
            broker=broker,
            symbol=symbol,
            side=side,
            order_type=order_type,
        )

    return ExecutionValidationResult(
        allowed=not reasons,
        needs_human=needs_human,
        status=status,
        reasons=tuple(reasons),
        normalized_intent=normalized_intent,
    )


def execution_intent_to_broker_order(
    intent: ExecutionIntent,
    risk: RiskSummary | None = None,
    run: RunMetadata | None = None,
) -> dict[str, Any]:
    validation = validate_execution_intent(intent, risk=risk, run=run)
    normalized = validation.require_allowed()
    asset_class = _clean_text(normalized.metadata.get("asset_class")) or "stock"
    return {
        "symbol": normalized.symbol,
        "side": normalized.side,
        "quantity": float(normalized.quantity),
        "order_type": normalized.order_type,
        "limit_price": normalized.limit_price,
        "time_in_force": normalized.time_in_force,
        "asset_class": asset_class.lower(),
        "metadata": dict(normalized.metadata),
    }

