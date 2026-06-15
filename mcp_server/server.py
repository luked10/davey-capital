"""MCP server for Poke triage and human approval of local trade proposals.

The server exposes the local watcher queue to Poke over MCP/SSE:

- get_pending_candidates reads watcher PokeBridgeHandoff JSONL rows.
- submit_triage_decision records Poke's proceed/reject decision and, when
  requested, asks the Sonnet proposal client for a dry-run ExecutionIntent.
- record_approval_decision records Luke's final approval/rejection. Dry-run
  approvals stay audit-only; explicitly non-dry-run approved intents may execute
  only after live-mode, validation, and circuit-breaker gates pass.
- get_system_status reads runtime_state.json.

Safety boundary: all proposal intents pass through validate_execution_intent
before being stored or executed. Unapproved intents never execute.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import Thread
from typing import Any

from pydantic import ValidationError


CODE_ROOT = Path(__file__).resolve().parents[1]
AUTOHEDGE_PACKAGE_ROOT = CODE_ROOT / "autohedge"


def _default_davey_root() -> Path:
    configured = os.getenv("DAVEY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if any(os.getenv(name) for name in ("FLY_APP_NAME", "FLY_MACHINE_ID", "FLY_REGION")):
        return Path("/app")
    return CODE_ROOT.resolve()


DAVEY_ROOT = _default_davey_root()
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))
if str(AUTOHEDGE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOHEDGE_PACKAGE_ROOT))

AUDIT_MODULE_PATH = CODE_ROOT / "autohedge" / "autohedge" / "audit" / "artifacts.py"
RUNTIME_STATE_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "runtime" / "runtime_state.py"
)
SONNET_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "proposal" / "sonnet_client.py"
)
RUNTIME_SCAFFOLD_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "runtime_scaffold.py"
)
REPORT_MODULE_PATH = CODE_ROOT / "nova-alpha" / "report_scaffold.py"
CIRCUIT_BREAKER_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "risk" / "circuit_breaker.py"
)
OBSERVATIONS_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "risk" / "observations.py"
)
ALPACA_LIVE_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "brokers" / "alpaca_live.py"
)
SEEN_IDS_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "state" / "seen_ids.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit_module = _load_module("davey_mcp_audit_artifacts", AUDIT_MODULE_PATH)
runtime_state_module = _load_module("davey_mcp_runtime_state", RUNTIME_STATE_MODULE_PATH)
sonnet_module = _load_module("davey_mcp_sonnet_client", SONNET_MODULE_PATH)
runtime_scaffold_module = _load_module(
    "davey_mcp_runtime_scaffold",
    RUNTIME_SCAFFOLD_MODULE_PATH,
)
report_module = _load_module("davey_mcp_nova_alpha_report", REPORT_MODULE_PATH)
circuit_breaker_module = _load_module(
    "davey_mcp_circuit_breaker",
    CIRCUIT_BREAKER_MODULE_PATH,
)
observations_module = _load_module("davey_mcp_observations", OBSERVATIONS_MODULE_PATH)
alpaca_live_module = _load_module("davey_mcp_alpaca_live", ALPACA_LIVE_MODULE_PATH)
seen_ids_module = _load_module("davey_mcp_seen_ids", SEEN_IDS_MODULE_PATH)

AuditArtifactWriter = audit_module.AuditArtifactWriter
default_runtime_state = runtime_state_module.default_runtime_state
load_runtime_state = runtime_state_module.load_runtime_state
SonnetProposalClient = sonnet_module.SonnetProposalClient
CircuitBreakerConfig = circuit_breaker_module.CircuitBreakerConfig
evaluate_circuit_breaker = circuit_breaker_module.evaluate_circuit_breaker
build_observations = observations_module.build_observations
AlpacaLiveBroker = alpaca_live_module.AlpacaLiveBroker
SeenIdsStore = seen_ids_module.SeenIdsStore

from contracts.bridge_contract import (
    ExecutionIntent,
    FillRecord,
    execution_intent_from_dict,
    execution_intent_to_broker_order,
    validate_execution_intent,
)
from contracts.overnight_scaffold import validate_poke_handoff_payload
from autohedge.schemas.models import CandidateSignal, TriageDecision


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _live_mode_enabled() -> bool:
    return os.getenv("DAVEY_LIVE_MODE", "").strip() == "1"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _safe_enabled_circuit_breaker_config() -> Any:
    return CircuitBreakerConfig(
        enabled=True,
        max_consecutive_losses=3,
        max_daily_loss_pct=0.02,
        max_open_trades=5,
    )


def _malformed_circuit_breaker_config() -> Any:
    return CircuitBreakerConfig(enabled="malformed")  # type: ignore[arg-type]


def _load_circuit_breaker_config(repo_root: Path) -> tuple[Any, str]:
    config_path = repo_root / "circuit_breaker_config.json"
    if not config_path.exists():
        return _safe_enabled_circuit_breaker_config(), ""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _malformed_circuit_breaker_config(), f"malformed config JSON: {exc}"
    if not isinstance(payload, dict):
        return _malformed_circuit_breaker_config(), "malformed config: expected JSON object"

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
        return _malformed_circuit_breaker_config(), "malformed config fields"
    return (
        CircuitBreakerConfig(
            enabled=enabled,
            max_consecutive_losses=max_losses,
            max_daily_loss_pct=float(max_daily_loss_pct),
            max_open_trades=max_open_trades,
        ),
        "",
    )


def _circuit_breaker_payload(result: Any, *, config_error: str = "") -> dict[str, Any]:
    return {
        "allowed": bool(getattr(result, "allowed", False)),
        "blocked": bool(getattr(result, "blocked", True)),
        "needs_human": bool(getattr(result, "needs_human", True)),
        "reason": str(getattr(result, "reason", "")),
        "triggered_rules": list(getattr(result, "triggered_rules", [])),
        "observed": dict(getattr(result, "observed", {})),
        "config_error": config_error,
    }


def _runtime_state_with_env(state: Any) -> Any:
    live_mode = _live_mode_enabled()
    state.live_mode = live_mode
    state.dry_run = not live_mode
    state.active_broker = "alpaca" if live_mode else "paper"
    return state


def _candidate_signal(row: dict[str, Any], *, session_id: str) -> CandidateSignal:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return CandidateSignal.model_validate(
        {
            "handoff_id": row.get("handoff_id", ""),
            "symbol": metadata.get("symbol", ""),
            "side": metadata.get("side", ""),
            "confidence": metadata.get("confidence"),
            "created_at": row.get("created_at", ""),
            "dry_run": row.get("dry_run", True),
            "metadata": {
                **metadata,
                "session_id": session_id,
                "run_id": row.get("run_id", ""),
                "candidate_event_id": row.get("candidate_event_id", ""),
            },
        }
    )


def _validation_error_payload(*, handoff_id: str, error: ValidationError) -> dict[str, Any]:
    return {
        "handoff_id": handoff_id,
        "status": "validation_error",
        "needs_human": True,
        "error": str(error),
        "message": f"Validation failed for {handoff_id}: {error}",
    }


def _proposal_text(
    *,
    symbol: str,
    side: str,
    confidence: Any,
    intent: ExecutionIntent | None,
    rationale: str,
) -> str:
    quantity = intent.quantity if intent is not None else "n/a"
    order_type = intent.order_type if intent is not None else "n/a"
    return (
        "TRADE PROPOSAL\n"
        f"Symbol: {symbol} | Side: {side} | Qty: {quantity}\n"
        f"Order: {order_type} | Confidence: {confidence}\n"
        f"Rationale: {rationale}\n"
        "Reply APPROVE or REJECT"
    )


class PokeBridgeService:
    """Stateful local bridge service for one running MCP server process."""

    def __init__(self, *, repo_root: Path = DAVEY_ROOT) -> None:
        self.repo_root = Path(repo_root)
        self.seen_ids = SeenIdsStore(davey_root=self.repo_root)
        self.proposals_by_handoff: dict[str, dict[str, Any]] = {}

    @property
    def overnight_root(self) -> Path:
        return self.repo_root / "logs" / "overnight"

    @property
    def audit_root(self) -> Path:
        return self.repo_root / "logs" / "audit"

    def _queue_paths(self) -> list[Path]:
        if not self.overnight_root.exists():
            return []
        try:
            return sorted(self.overnight_root.rglob("poke_bridge_queue.jsonl"))
        except OSError:
            return []

    def _find_handoff(self, handoff_id: str) -> tuple[dict[str, Any], str] | None:
        for path in self._queue_paths():
            session_id = path.parent.name
            for row in _read_jsonl(path):
                if row.get("handoff_id") == handoff_id:
                    validation = validate_poke_handoff_payload(row)
                    if not validation.valid or validation.normalized is None:
                        return None
                    return validation.normalized, session_id
        return None

    def get_pending_candidates(self) -> list[CandidateSignal]:
        pending: list[CandidateSignal] = []
        for path in self._queue_paths():
            session_id = path.parent.name
            for row in _read_jsonl(path):
                validation = validate_poke_handoff_payload(row)
                if not validation.valid or validation.normalized is None:
                    continue
                normalized = validation.normalized
                handoff_id = normalized["handoff_id"]
                if self.seen_ids.is_seen(handoff_id):
                    print(f"candidate already seen: {handoff_id}", flush=True)
                    continue
                try:
                    candidate = _candidate_signal(normalized, session_id=session_id)
                except ValidationError:
                    continue
                pending.append(candidate)
                self.seen_ids.mark_seen(handoff_id)
                print(f"new candidate found: {handoff_id}", flush=True)
        return pending

    def submit_triage_decision(
        self,
        handoff_id: str,
        proceed: bool,
        reason: str,
        decided_at: str | None = None,
    ) -> dict[str, Any]:
        try:
            decision = TriageDecision.model_validate(
                {
                    "handoff_id": handoff_id,
                    "proceed": proceed,
                    "reason": reason,
                    "decided_at": decided_at or _utc_now_iso(),
                }
            )
        except ValidationError as exc:
            return _validation_error_payload(handoff_id=str(handoff_id or ""), error=exc)

        handoff_id = decision.handoff_id
        found = self._find_handoff(handoff_id)
        if found is None:
            raise ValueError(f"handoff_id not found or invalid: {handoff_id}")
        handoff, session_id = found
        try:
            candidate = _candidate_signal(handoff, session_id=session_id).model_dump()
        except ValidationError as exc:
            return _validation_error_payload(handoff_id=decision.handoff_id, error=exc)

        writer = AuditArtifactWriter(
            session_id=session_id,
            artifact_root=self.audit_root,
            model="",
            provider="poke_mcp",
        )

        proposal_payload: dict[str, Any] = {}
        if decision.proceed:
            observations = build_observations(candidate["symbol"])
            cb_config, cb_config_error = _load_circuit_breaker_config(self.repo_root)
            cb_result = evaluate_circuit_breaker(cb_config, observations)
            cb_payload = _circuit_breaker_payload(
                cb_result,
                config_error=cb_config_error,
            )
            writer.write_decision_artifact(
                decision_id=f"circuit-breaker-{handoff_id}",
                decision="blocked" if cb_result.blocked else "normal",
                rationale=cb_result.reason,
                source="circuit_breaker",
                context={
                    "handoff_id": handoff_id,
                    "candidate": candidate,
                    "circuit_breaker": cb_payload,
                },
            )
            if cb_result.blocked:
                proposal_payload = {
                    "intent_id": "",
                    "intent": None,
                    "validation": None,
                    "token_meta": {},
                    "raw": "",
                    "needs_human": True,
                    "error": cb_result.reason,
                    "circuit_breaker": cb_payload,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=None,
                        rationale="Circuit breaker requires human review: "
                        + cb_result.reason,
                    ),
                }
                writer.append_triage_decision(
                    handoff_id=handoff_id,
                    proceed=decision.proceed,
                    reason=decision.reason,
                    candidate=candidate,
                    proposal=proposal_payload,
                )
                return {
                    "handoff_id": handoff_id,
                    "status": "proposal_needs_human",
                    "needs_human": True,
                    "candidate": candidate,
                    "proposal": proposal_payload,
                    "message": proposal_payload["proposal_text"],
                }

            confidence = candidate.get("confidence")
            if confidence is None:
                confidence = 0.0
            else:
                try:
                    confidence = float(confidence)
                except (ValueError, TypeError):
                    confidence = 0.0

            if confidence < 0.80:
                print(
                    f"Skipping Sonnet proposal for {handoff_id}: confidence {confidence} is below 0.80 threshold",
                    flush=True,
                )
                reason_str = f"Confidence {confidence} below 0.80 threshold, skipping Sonnet"
                proposal_payload = {
                    "intent_id": "",
                    "intent": None,
                    "validation": None,
                    "token_meta": {},
                    "raw": "",
                    "needs_human": True,
                    "error": reason_str,
                    "circuit_breaker": cb_payload,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=confidence,
                        intent=None,
                        rationale=reason_str,
                    ),
                }
                writer.append_triage_decision(
                    handoff_id=handoff_id,
                    proceed=decision.proceed,
                    reason=decision.reason,
                    candidate=candidate,
                    proposal=proposal_payload,
                )
                return {
                    "handoff_id": handoff_id,
                    "status": "proposal_needs_human",
                    "needs_human": True,
                    "candidate": candidate,
                    "proposal": proposal_payload,
                    "message": proposal_payload["proposal_text"],
                }

            client = SonnetProposalClient()
            proposal_result = client.propose(
                {
                    "event_id": handoff.get("candidate_event_id", ""),
                    "signal_id": handoff.get("candidate_event_id", ""),
                    "symbol": candidate["symbol"],
                    "side": candidate["side"],
                    "confidence": candidate["confidence"],
                    "created_at": handoff.get("created_at", ""),
                    "dry_run": True,
                    "metadata": {
                        "handoff_id": handoff_id,
                        "run_id": handoff.get("run_id", ""),
                        "source": "poke_mcp_server",
                    },
                }
            )
            intent = proposal_result.intent
            rationale = proposal_result.error or "Dry-run proposal generated for human review."
            if intent is not None:
                proposal_payload = {
                    "intent_id": intent.intent_id,
                    "intent": audit_module.to_dict(intent),
                    "validation": {
                        "allowed": proposal_result.validation.allowed
                        if proposal_result.validation is not None
                        else False,
                        "needs_human": proposal_result.validation.needs_human
                        if proposal_result.validation is not None
                        else True,
                        "status": proposal_result.validation.status
                        if proposal_result.validation is not None
                        else "needs_human",
                        "reasons": list(proposal_result.validation.reasons)
                        if proposal_result.validation is not None
                        else [proposal_result.error],
                    },
                    "token_meta": dict(proposal_result.token_meta),
                    "raw": proposal_result.raw,
                    "circuit_breaker": cb_payload,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=intent,
                        rationale=rationale,
                    ),
                }
                self.proposals_by_handoff[handoff_id] = {
                    "intent": intent,
                    "session_id": session_id,
                    "candidate": candidate,
                    "proposal": proposal_payload,
                }
            else:
                proposal_payload = {
                    "intent_id": "",
                    "intent": None,
                    "validation": None,
                    "token_meta": dict(proposal_result.token_meta),
                    "raw": proposal_result.raw,
                    "error": proposal_result.error,
                    "needs_human": True,
                    "circuit_breaker": cb_payload,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=None,
                        rationale=proposal_result.error
                        or "Proposal generation requires human review.",
                    ),
                }
                # Fix: Store raw proposal even when intent is None so human can still approve.
                # Attempt to extract intent from raw if possible to help downstream record_approval_decision.
                try:
                    raw_intent_dict = json.loads(proposal_result.raw)
                    intent_from_raw = execution_intent_from_dict(raw_intent_dict)
                except (ValueError, TypeError):
                    intent_from_raw = None
                
                self.proposals_by_handoff[handoff_id] = {
                    "intent": intent_from_raw,
                    "session_id": session_id,
                    "candidate": candidate,
                    "proposal": proposal_payload,
                }

        writer.append_triage_decision(
            handoff_id=handoff_id,
            proceed=decision.proceed,
            reason=decision.reason,
            candidate=candidate,
            proposal=proposal_payload,
        )

        if not decision.proceed:
            return {
                "handoff_id": handoff_id,
                "status": "rejected_by_triage",
                "candidate": candidate,
                "message": f"Triage rejected {handoff_id}: {decision.reason}",
            }

        return {
            "handoff_id": handoff_id,
            "status": "proposal_ready"
            if proposal_payload.get("intent") is not None
            else "proposal_needs_human",
            "candidate": candidate,
            "proposal": proposal_payload,
            "message": proposal_payload.get("proposal_text", ""),
        }

    def _write_live_block(
        self,
        *,
        writer: Any,
        handoff_id: str,
        approved_by: str,
        intent_id: str,
        reason_code: str,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        writer.write_needs_human_artifact(
            needs_human_id=f"live-execution-{handoff_id}",
            reason_code=reason_code,
            reason=reason,
            source_event_id=handoff_id,
            context=dict(context or {}),
        )
        writer.append_approval_decision(
            handoff_id=handoff_id,
            approved=False,
            approved_by=approved_by,
            intent_id=intent_id,
            reason=reason,
        )
        print(
            f"approval path: live execution blocked for {handoff_id}: {reason}",
            flush=True,
        )
        return f"Approval for {handoff_id} blocked: {reason}"

    def _update_runtime_state_after_fill(self, fill: FillRecord) -> None:
        result = load_runtime_state(self.repo_root / "runtime_state.json")
        state = result.state if result.ok and result.state is not None else default_runtime_state()
        state.active_broker = "alpaca"
        state.dry_run = fill.dry_run
        state.live_mode = _live_mode_enabled()
        state.circuit_breaker_status = "normal"
        state.positions_summary = {
            "open_positions": 0,
            "source": "alpaca fill artifact",
            "latest_fill": audit_module.to_dict(fill),
        }
        state.latest_signal_ids = [fill.intent_id]
        state.last_error = ""
        state.last_health_check = _utc_now_iso()
        runtime_state_module.save_runtime_state(
            state,
            self.repo_root / "runtime_state.json",
        )

    def record_approval_decision(
        self,
        handoff_id: str,
        approved: bool,
        approved_by: str = "luke_poke",
    ) -> str:
        if not isinstance(approved, bool):
            raise ValueError("approved must be boolean")
        found = self._find_handoff(handoff_id)
        if found is None:
            raise ValueError(f"handoff_id not found or invalid: {handoff_id}")
        _, session_id = found
        writer = AuditArtifactWriter(
            session_id=session_id,
            artifact_root=self.audit_root,
            model="",
            provider="poke_mcp",
        )

        proposal = self.proposals_by_handoff.get(handoff_id)
        intent_id = ""
        if approved:
            if proposal is None or not isinstance(proposal.get("intent"), ExecutionIntent):
                writer.append_approval_decision(
                    handoff_id=handoff_id,
                    approved=False,
                    approved_by=approved_by,
                    reason="approval requested but no validated proposal is available",
                )
                return (
                    f"Approval for {handoff_id} blocked: no validated proposal is "
                    "available. No intent artifact written."
                )

            proposed_intent = proposal["intent"]
            live_mode = _live_mode_enabled()
            approved_intent = replace(
                proposed_intent,
                dry_run=proposed_intent.dry_run if live_mode else True,
                approved=True,
                approved_by=approved_by or "luke_poke",
                approved_at=_utc_now_iso(),
            )
            if proposed_intent.dry_run is False and not live_mode:
                print(
                    "approval path: forced dry-run audit only for "
                    f"{handoff_id}; DAVEY_LIVE_MODE is not 1",
                    flush=True,
                )
                writer.write_decision_artifact(
                    decision_id=f"live-mode-forced-dry-run-{handoff_id}",
                    decision="forced_dry_run",
                    rationale="DAVEY_LIVE_MODE is not 1; approval recorded as dry-run only",
                    source="poke_mcp_live_gate",
                    context={
                        "handoff_id": handoff_id,
                        "intent_id": proposed_intent.intent_id,
                    },
                )

            validation = validate_execution_intent(approved_intent)
            if not validation.allowed or validation.normalized_intent is None:
                writer.append_approval_decision(
                    handoff_id=handoff_id,
                    approved=False,
                    approved_by=approved_by,
                    intent_id=approved_intent.intent_id,
                    reason="approval intent failed validation: "
                    + "; ".join(validation.reasons),
                )
                return (
                    f"Approval for {handoff_id} blocked by validation: "
                    + "; ".join(validation.reasons)
                )

            normalized_intent = validation.normalized_intent
            write_result = writer.write_intent_artifact(normalized_intent)
            if not write_result.ok:
                writer.append_approval_decision(
                    handoff_id=handoff_id,
                    approved=False,
                    approved_by=approved_by,
                    intent_id=approved_intent.intent_id,
                    reason="intent artifact write failed: "
                    + "; ".join(write_result.reasons),
                )
                return (
                    f"Approval for {handoff_id} blocked: intent artifact write failed."
                )
            intent_id = normalized_intent.intent_id

            if normalized_intent.dry_run is False:
                print(
                    f"approval path: live Alpaca execution for {handoff_id}",
                    flush=True,
                )
                observations = build_observations(normalized_intent.symbol)
                cb_config, cb_config_error = _load_circuit_breaker_config(self.repo_root)
                cb_result = evaluate_circuit_breaker(cb_config, observations)
                cb_payload = _circuit_breaker_payload(
                    cb_result,
                    config_error=cb_config_error,
                )
                writer.write_decision_artifact(
                    decision_id=f"approval-circuit-breaker-{handoff_id}",
                    decision="blocked" if cb_result.blocked else "normal",
                    rationale=cb_result.reason,
                    source="circuit_breaker",
                    context={
                        "handoff_id": handoff_id,
                        "intent_id": intent_id,
                        "circuit_breaker": cb_payload,
                    },
                )
                if cb_result.blocked:
                    return self._write_live_block(
                        writer=writer,
                        handoff_id=handoff_id,
                        approved_by=approved_by,
                        intent_id=intent_id,
                        reason_code="CIRCUIT_BREAKER_BLOCKED",
                        reason="circuit breaker blocked live execution: "
                        + cb_result.reason,
                        context={"circuit_breaker": cb_payload},
                    )

                try:
                    order_payload = execution_intent_to_broker_order(normalized_intent)
                except Exception as exc:
                    return self._write_live_block(
                        writer=writer,
                        handoff_id=handoff_id,
                        approved_by=approved_by,
                        intent_id=intent_id,
                        reason_code="BROKER_ORDER_CONVERSION_FAILED",
                        reason=f"broker order conversion failed: {exc}",
                    )

                try:
                    fill = AlpacaLiveBroker(
                        session_id=session_id,
                        artifact_root=self.audit_root,
                    ).submit_order(normalized_intent)
                except Exception as exc:
                    return self._write_live_block(
                        writer=writer,
                        handoff_id=handoff_id,
                        approved_by=approved_by,
                        intent_id=intent_id,
                        reason_code="ALPACA_EXECUTION_BLOCKED",
                        reason=str(exc),
                        context={"order": order_payload},
                    )

                self._update_runtime_state_after_fill(fill)
                writer.append_approval_decision(
                    handoff_id=handoff_id,
                    approved=True,
                    approved_by=approved_by,
                    intent_id=intent_id,
                    reason="approved and submitted to Alpaca",
                )
                print(
                    "approval path: live Alpaca execution complete for "
                    f"{handoff_id}: order_id={fill.order_id} status={fill.status}",
                    flush=True,
                )
                return (
                    f"Approved {handoff_id}; submitted Alpaca order "
                    f"{fill.order_id} with status {fill.status}."
                )

        writer.append_approval_decision(
            handoff_id=handoff_id,
            approved=approved,
            approved_by=approved_by,
            intent_id=intent_id,
            reason="" if approved else "rejected by human approval gate",
        )
        if approved:
            print(
                f"approval path: dry-run audit only for {handoff_id}; "
                "no broker order created",
                flush=True,
            )
            return (
                f"Approved {handoff_id}; wrote dry-run approved intent artifact "
                f"{intent_id}. No broker order was created."
            )
        print(
            f"approval path: rejected by human approval gate for {handoff_id}",
            flush=True,
        )
        return f"Rejected {handoff_id}; no intent artifact or broker action taken."

    def get_system_status(self) -> dict[str, Any]:
        result = load_runtime_state(self.repo_root / "runtime_state.json")
        if not result.ok or result.state is None:
            state = _runtime_state_with_env(default_runtime_state(updated_at=""))
            try:
                runtime_state_module.save_runtime_state(
                    state,
                    self.repo_root / "runtime_state.json",
                )
            except Exception:
                pass
            missing_only = result.reasons == (
                f"runtime state file not found: {self.repo_root / 'runtime_state.json'}",
            )
            return {
                "active_broker": state.active_broker,
                "dry_run": state.dry_run,
                "live_mode": state.live_mode,
                "circuit_breaker_status": state.circuit_breaker_status,
                "last_error": "" if missing_only else "; ".join(result.reasons),
            }
        state = _runtime_state_with_env(result.state)
        try:
            runtime_state_module.save_runtime_state(
                state,
                self.repo_root / "runtime_state.json",
            )
        except Exception:
            pass
        return {
            "active_broker": state.active_broker,
            "dry_run": state.dry_run,
            "live_mode": state.live_mode,
            "circuit_breaker_status": state.circuit_breaker_status,
            "last_error": state.last_error,
        }

    def get_daily_report(self) -> str:
        """Return today's local nova-alpha report for Poke/SMS delivery."""
        try:
            report_date = datetime.now(timezone.utc).date().isoformat()
            artifacts = report_module.load_local_artifacts(
                self.repo_root,
                today_only=True,
                report_date=report_date,
            )
            if not report_module.has_report_activity(artifacts):
                return "No activity today"
            return report_module.render_daily_report(
                artifacts,
                report_date=report_date,
                today_only=True,
            )
        except Exception as exc:
            return f"Daily report unavailable: {exc}"


SERVICE = PokeBridgeService()
_SCHEDULER_THREAD: Thread | None = None


def _scheduler_enabled() -> bool:
    return os.getenv("DAVEY_SCHEDULER_ENABLED", "").strip() == "1"


def _run_scheduler_start() -> None:
    try:
        print("scheduler background start requested", flush=True)
        result = runtime_scaffold_module.start()
        print(
            "scheduler background start result: "
            + json.dumps(result, sort_keys=True, default=str),
            flush=True,
        )
    except Exception as exc:
        print(f"scheduler start failed safely: {exc}", file=sys.stderr, flush=True)


def start_scheduler_background() -> bool:
    """Start the opt-in scheduler without blocking the MCP/SSE server."""
    global _SCHEDULER_THREAD

    if not _scheduler_enabled():
        print("scheduler disabled: DAVEY_SCHEDULER_ENABLED is not 1", flush=True)
        return False
    if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
        print("scheduler already running", flush=True)
        return True
    _SCHEDULER_THREAD = Thread(
        target=_run_scheduler_start,
        name="davey-scheduler-start",
        daemon=True,
    )
    _SCHEDULER_THREAD.start()
    print("scheduler background thread launched", flush=True)
    return True


def get_pending_candidates() -> list[CandidateSignal]:
    return SERVICE.get_pending_candidates()


def submit_triage_decision(
    handoff_id: str,
    proceed: bool,
    reason: str,
    decided_at: str | None = None,
) -> dict[str, Any]:
    return SERVICE.submit_triage_decision(
        handoff_id=handoff_id,
        proceed=proceed,
        reason=reason,
        decided_at=decided_at,
    )


def record_approval_decision(
    handoff_id: str,
    approved: bool,
    approved_by: str = "luke_poke",
) -> str:
    return SERVICE.record_approval_decision(
        handoff_id=handoff_id,
        approved=approved,
        approved_by=approved_by,
    )


def get_system_status() -> dict[str, Any]:
    return SERVICE.get_system_status()


def get_daily_report() -> str:
    return SERVICE.get_daily_report()


def build_mcp_app():
    """Build the MCP app lazily so offline imports do not require mcp."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "mcp_server requires the mcp package. Install mcp_server/requirements.txt."
        ) from exc

    app = FastMCP("davey-capital-mcp", host="0.0.0.0", port=8080)
    app.tool()(get_pending_candidates)
    app.tool()(submit_triage_decision)
    app.tool()(record_approval_decision)
    app.tool()(get_system_status)
    app.tool()(get_daily_report)
    return app


def main() -> None:
    start_scheduler_background()
    app = build_mcp_app()
    app.run(transport="sse")


if __name__ == "__main__":
    main()
