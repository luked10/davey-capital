"""MCP server for Poke triage and human approval of local trade proposals.

The server exposes the local watcher queue to Poke over MCP/SSE:

- get_pending_candidates reads watcher PokeBridgeHandoff JSONL rows.
- submit_triage_decision records Poke's proceed/reject decision and, when
  requested, asks the Sonnet proposal client for a dry-run ExecutionIntent.
- record_approval_decision records Luke's final approval/rejection and writes an
  approved dry-run intent artifact only. It never converts or executes orders.
- get_system_status reads runtime_state.json.

Safety boundary: this module never imports broker adapters, never creates
broker order payloads, and never executes anything. All proposal intents pass
through validate_execution_intent before being stored.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(os.getenv("DAVEY_ROOT", Path(__file__).resolve().parents[1])).resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AUDIT_MODULE_PATH = REPO_ROOT / "autohedge" / "autohedge" / "audit" / "artifacts.py"
RUNTIME_STATE_MODULE_PATH = (
    REPO_ROOT / "autohedge" / "autohedge" / "runtime" / "runtime_state.py"
)
SONNET_MODULE_PATH = (
    REPO_ROOT / "autohedge" / "autohedge" / "proposal" / "sonnet_client.py"
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

AuditArtifactWriter = audit_module.AuditArtifactWriter
load_runtime_state = runtime_state_module.load_runtime_state
SonnetProposalClient = sonnet_module.SonnetProposalClient

from contracts.bridge_contract import ExecutionIntent, validate_execution_intent
from contracts.overnight_scaffold import validate_poke_handoff_payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _candidate_summary(row: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        "handoff_id": row.get("handoff_id", ""),
        "symbol": metadata.get("symbol", ""),
        "side": metadata.get("side", ""),
        "confidence": metadata.get("confidence"),
        "created_at": row.get("created_at", ""),
        "session_id": session_id,
        "candidate_event_id": row.get("candidate_event_id", ""),
    }


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "handoff_id": candidate["handoff_id"],
        "symbol": candidate["symbol"],
        "side": candidate["side"],
        "confidence": candidate["confidence"],
        "created_at": candidate["created_at"],
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

    def __init__(self, *, repo_root: Path = REPO_ROOT) -> None:
        self.repo_root = repo_root
        self.seen_ids: set[str] = set()
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
        return sorted(self.overnight_root.glob("*/poke_bridge_queue.jsonl"))

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

    def get_pending_candidates(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for path in self._queue_paths():
            session_id = path.parent.name
            for row in _read_jsonl(path):
                validation = validate_poke_handoff_payload(row)
                if not validation.valid or validation.normalized is None:
                    continue
                normalized = validation.normalized
                handoff_id = normalized["handoff_id"]
                if handoff_id in self.seen_ids:
                    continue
                pending.append(
                    _public_candidate(
                        _candidate_summary(normalized, session_id=session_id)
                    )
                )
                self.seen_ids.add(handoff_id)
        return pending

    def submit_triage_decision(
        self,
        handoff_id: str,
        proceed: bool,
        reason: str,
    ) -> dict[str, Any]:
        if not isinstance(proceed, bool):
            raise ValueError("proceed must be boolean")
        found = self._find_handoff(handoff_id)
        if found is None:
            raise ValueError(f"handoff_id not found or invalid: {handoff_id}")
        handoff, session_id = found
        candidate = _candidate_summary(handoff, session_id=session_id)
        writer = AuditArtifactWriter(
            session_id=session_id,
            artifact_root=self.audit_root,
            model="",
            provider="poke_mcp",
        )

        proposal_payload: dict[str, Any] = {}
        if proceed:
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
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=None,
                        rationale=proposal_result.error
                        or "Proposal generation requires human review.",
                    ),
                }

        writer.append_triage_decision(
            handoff_id=handoff_id,
            proceed=proceed,
            reason=reason,
            candidate=candidate,
            proposal=proposal_payload,
        )

        if not proceed:
            return {
                "handoff_id": handoff_id,
                "status": "rejected_by_triage",
                "candidate": candidate,
                "message": f"Triage rejected {handoff_id}: {reason}",
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
            approved_intent = replace(
                proposed_intent,
                dry_run=True,
                approved=True,
                approved_by=approved_by or "luke_poke",
                approved_at=_utc_now_iso(),
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

            write_result = writer.write_intent_artifact(validation.normalized_intent)
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
            intent_id = validation.normalized_intent.intent_id

        writer.append_approval_decision(
            handoff_id=handoff_id,
            approved=approved,
            approved_by=approved_by,
            intent_id=intent_id,
            reason="" if approved else "rejected by human approval gate",
        )
        if approved:
            return (
                f"Approved {handoff_id}; wrote dry-run approved intent artifact "
                f"{intent_id}. No broker order was created."
            )
        return f"Rejected {handoff_id}; no intent artifact or broker action taken."

    def get_system_status(self) -> dict[str, Any]:
        result = load_runtime_state(self.repo_root / "runtime_state.json")
        if not result.ok or result.state is None:
            return {
                "active_broker": "",
                "dry_run": True,
                "live_mode": False,
                "circuit_breaker_status": {},
                "last_error": "; ".join(result.reasons),
            }
        state = result.state
        return {
            "active_broker": state.active_broker,
            "dry_run": state.dry_run,
            "live_mode": state.live_mode,
            "circuit_breaker_status": state.circuit_breaker_status,
            "last_error": state.last_error,
        }


SERVICE = PokeBridgeService()


def get_pending_candidates() -> list[dict[str, Any]]:
    return SERVICE.get_pending_candidates()


def submit_triage_decision(
    handoff_id: str,
    proceed: bool,
    reason: str,
) -> dict[str, Any]:
    return SERVICE.submit_triage_decision(
        handoff_id=handoff_id,
        proceed=proceed,
        reason=reason,
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
    return app


def main() -> None:
    app = build_mcp_app()
    app.run(transport="sse")


if __name__ == "__main__":
    main()
