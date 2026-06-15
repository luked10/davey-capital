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
import uuid

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
PROPOSAL_STORE_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "state" / "proposal_store.py"
)
OVERNIGHT_WATCHER_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "overnight_scaffold.py"
)
MARKET_FEED_MODULE_PATH = (
    CODE_ROOT / "autohedge" / "autohedge" / "data" / "market_feed.py"
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
proposal_store_module = _load_module(
    "davey_mcp_proposal_store",
    PROPOSAL_STORE_MODULE_PATH,
)
overnight_watcher_module = _load_module(
    "davey_mcp_overnight_watcher_scaffold",
    OVERNIGHT_WATCHER_MODULE_PATH,
)
market_feed_module = _load_module("davey_mcp_market_feed", MARKET_FEED_MODULE_PATH)

AuditArtifactWriter = audit_module.AuditArtifactWriter
default_runtime_state = runtime_state_module.default_runtime_state
load_runtime_state = runtime_state_module.load_runtime_state
SonnetProposalClient = sonnet_module.SonnetProposalClient
CircuitBreakerConfig = circuit_breaker_module.CircuitBreakerConfig
evaluate_circuit_breaker = circuit_breaker_module.evaluate_circuit_breaker
build_observations = observations_module.build_observations
AlpacaLiveBroker = alpaca_live_module.AlpacaLiveBroker
SeenIdsStore = seen_ids_module.SeenIdsStore
ProposalStore = proposal_store_module.ProposalStore
OvernightArtifactWriter = overnight_watcher_module.OvernightArtifactWriter
DeterministicTier0Watcher = overnight_watcher_module.DeterministicTier0Watcher

_BULLISH_KEYWORDS = frozenset({
    "surge", "soar", "jump", "rally", "beat", "record", "strong",
    "growth", "gain", "rise", "higher", "outperform", "bullish",
    "upgrade", "positive", "boost",
})
_BEARISH_KEYWORDS = frozenset({
    "fall", "drop", "crash", "miss", "weak", "loss", "cut",
    "lower", "underperform", "bearish", "decline", "plunge",
    "downgrade", "negative", "warning",
})
_SURFACE_CONFIDENCE_THRESHOLD = 0.65


def _extract_ticker_from_query(query: str, known_symbols: frozenset[str]) -> str | None:
    """Extract the most likely ticker from a free-text query.

    Prefers known WATCH_SYMBOLS (case-insensitive). Falls back to the first
    all-uppercase 1–5 letter token in the original query.
    """
    import re
    tokens = re.findall(r"\b([A-Za-z]{1,5})\b", query)
    for t in tokens:
        if t.upper() in known_symbols:
            return t.upper()
    upper_only = re.findall(r"\b([A-Z]{1,5})\b", query)
    return upper_only[0] if upper_only else None


def _headline_sentiment(title: str) -> tuple[int, int]:
    """Return (bullish_hits, bearish_hits) for a news headline using substring match."""
    lower = title.lower()
    bull = sum(1 for kw in _BULLISH_KEYWORDS if kw in lower)
    bear = sum(1 for kw in _BEARISH_KEYWORDS if kw in lower)
    return bull, bear

from contracts.bridge_contract import (
    ExecutionIntent,
    FillRecord,
    execution_intent_from_dict,
    execution_intent_to_broker_order,
    validate_execution_intent,
)
from contracts.overnight_scaffold import (
    CandidateEvent,
    PokeBridgeHandoff,
    validate_poke_handoff_payload,
)
from autohedge.schemas.models import CandidateSignal, TriageDecision


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _live_mode_enabled() -> bool:
    return os.getenv("DAVEY_LIVE_MODE", "").strip() == "1"


# ---------------------------------------------------------------------------
# Tier 2 research routing (PART 3): enrich the Sonnet proposal by confidence.
#
#   Tier A (0.80–0.84): price data + Poke thesis + PatternTool only (no swarm).
#   Tier B (0.85–0.89): + vibe-trading technical_analysis_panel swarm.
#   Tier C (>= 0.90):   + vibe-trading investment_committee (bull/bear debate).
#
# vibe-trading is best-effort enrichment only: if VIBE_TRADING_URL is unset or
# any call fails/times out we skip silently and NEVER block the pipeline.
# ---------------------------------------------------------------------------

_RESEARCH_TIER_B_MIN = 0.85
_RESEARCH_TIER_C_MIN = 0.90
_SWARM_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _research_tier_label(confidence: float) -> str:
    if confidence >= _RESEARCH_TIER_C_MIN:
        return "C"
    if confidence >= _RESEARCH_TIER_B_MIN:
        return "B"
    return "A"


def _select_research_preset(confidence: float) -> str | None:
    """Map a candidate confidence to the vibe-trading swarm preset to run.

    Tier A returns None (no swarm). Tier B runs the technical_analysis_panel.
    Tier C runs the investment_committee. Below 0.80 also returns None.
    """
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return None
    if conf >= _RESEARCH_TIER_C_MIN:
        return "investment_committee"
    if conf >= _RESEARCH_TIER_B_MIN:
        return "technical_analysis_panel"
    return None


def _vibe_trading_base_url() -> str:
    return os.getenv("VIBE_TRADING_URL", "").strip().rstrip("/")


def _swarm_user_vars(preset_name: str, symbol: str) -> dict[str, str]:
    """Map a symbol onto the variables each preset declares (see preset YAML)."""
    if preset_name == "investment_committee":
        return {"target": symbol, "market": "US"}
    # technical_analysis_panel (and any default) take target + timeframe.
    return {"target": symbol, "timeframe": "daily"}


def call_vibe_trading_swarm(
    preset_name: str,
    user_vars: dict[str, str],
    *,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Best-effort vibe-trading swarm call. Returns a research dict or None.

    Starts a swarm run (POST /swarm/runs), then polls run status until it
    reaches a terminal state or the timeout elapses, returning the final
    report. Skips silently (returns None) when VIBE_TRADING_URL is unset or on
    ANY error/timeout. This is research enrichment only — it must never raise
    into or block the proposal pipeline.
    """
    base = _vibe_trading_base_url()
    if not base or not preset_name:
        return None

    import time
    import urllib.request

    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("VIBE_TRADING_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        timeout = max(1.0, float(timeout))
    except (TypeError, ValueError):
        timeout = 60.0
    per_request_timeout = min(15.0, timeout)
    deadline = time.monotonic() + timeout

    try:
        body = json.dumps(
            {"preset_name": preset_name, "user_vars": dict(user_vars or {})}
        ).encode("utf-8")
        create_req = urllib.request.Request(
            f"{base}/swarm/runs", data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(create_req, timeout=per_request_timeout) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        run_id = str(created.get("id") or "").strip()
        if not run_id:
            return None

        # Poll run detail until terminal or timeout. The /swarm/runs/{id}/events
        # SSE stream is the realtime alternative; the detail endpoint is polled
        # here because it returns the final_report we attach to the proposal.
        status = ""
        final_report = ""
        detail_url = f"{base}/swarm/runs/{run_id}"
        while True:
            poll_req = urllib.request.Request(
                detail_url, headers=headers, method="GET"
            )
            with urllib.request.urlopen(poll_req, timeout=per_request_timeout) as resp:
                detail = json.loads(resp.read().decode("utf-8"))
            status = str(detail.get("status") or "").strip().lower()
            report = detail.get("final_report")
            if isinstance(report, str) and report.strip():
                final_report = report
            if status in _SWARM_TERMINAL_STATUSES:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(2.0)

        return {
            "preset_name": preset_name,
            "run_id": run_id,
            "status": status,
            "report": final_report,
        }
    except Exception as exc:
        print(f"vibe-trading swarm skipped ({preset_name}): {exc}", flush=True)
        return None


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
        self.proposal_store = ProposalStore(davey_root=self.repo_root)
        try:
            existing = self.proposal_store.count_proposals()
        except Exception as exc:
            existing = -1
            print(
                f"ProposalStore startup count failed: {exc}",
                flush=True,
            )
        print(
            f"PokeBridgeService: repo_root={self.repo_root} "
            f"proposal_store db_path={self.proposal_store.db_path} "
            f"existing_rows={existing}",
            flush=True,
        )

    def _forget_proposal(self, handoff_id: str) -> None:
        """Remove a resolved proposal from the persistent store."""
        self.proposal_store.delete_proposal(handoff_id)

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
                cb_rationale = "Circuit breaker requires human review: " + cb_result.reason
                proposal_payload = {
                    "intent_id": "",
                    "intent": None,
                    "validation": None,
                    "token_meta": {},
                    "raw": "",
                    "needs_human": True,
                    "error": cb_result.reason,
                    "circuit_breaker": cb_payload,
                    "rationale": cb_rationale,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=None,
                        rationale=cb_rationale,
                    ),
                }
                # Persist a needs_human marker so fly logs / DB confirm the path
                # was reached. intent_json=None signals no executable intent.
                self.proposal_store.save_proposal(
                    handoff_id=handoff_id,
                    session_id=session_id,
                    candidate=candidate,
                    proposal_payload=proposal_payload,
                    intent_json=None,
                )
                print(
                    f"proposal saved to db: {handoff_id} "
                    "(needs_human=circuit_breaker)",
                    flush=True,
                )
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

            will_call_sonnet = confidence >= 0.80
            print(
                f"confidence={confidence} threshold=0.80 "
                f"will_call_sonnet={will_call_sonnet}",
                flush=True,
            )

            if not will_call_sonnet:
                print(
                    f"Skipping Sonnet proposal for {handoff_id}: confidence {confidence} is below 0.80 threshold",
                    flush=True,
                )
                reason_str = (
                    f"Auto-skipped: confidence {confidence} below 0.80 threshold"
                )
                proposal_payload = {
                    "intent_id": "",
                    "intent": None,
                    "validation": None,
                    "token_meta": {},
                    "raw": "",
                    "needs_human": True,
                    "error": reason_str,
                    "circuit_breaker": cb_payload,
                    "rationale": reason_str,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=confidence,
                        intent=None,
                        rationale=reason_str,
                    ),
                }
                # Persist a needs_human marker for the low-confidence path so
                # the path's reach is visible in the DB / fly logs.
                self.proposal_store.save_proposal(
                    handoff_id=handoff_id,
                    session_id=session_id,
                    candidate=candidate,
                    proposal_payload=proposal_payload,
                    intent_json=None,
                )
                print(
                    f"proposal saved to db: {handoff_id} "
                    "(needs_human=low_confidence)",
                    flush=True,
                )
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

            # Tier A/B/C research routing: enrich the proposal by confidence.
            # Tier B/C may call the vibe-trading swarm (best-effort, silent on
            # failure). Price data + Poke thesis + PatternTool are always
            # included from the candidate metadata (no extra network call).
            research_package = self._build_research_package(
                symbol=candidate["symbol"],
                confidence=confidence,
                candidate_metadata=candidate.get("metadata"),
            )
            print(
                f"research routing: {handoff_id} tier={research_package['research_tier']} "
                f"vibe_preset={research_package.get('vibe_trading_preset') or 'none'}",
                flush=True,
            )

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
                        "research_package": research_package,
                    },
                }
            )
            intent = proposal_result.intent
            if intent is not None:
                # The Sonnet response carries its rationale in
                # intent.metadata["rationale"] (see sonnet_client validation).
                # Read it from there; proposal_result.error is empty on success
                # and must not be used as the rationale source.
                intent_metadata = (
                    intent.metadata if isinstance(intent.metadata, dict) else {}
                )
                rationale = str(intent_metadata.get("rationale") or "").strip()
                if not rationale:
                    rationale = "Dry-run proposal generated for human review."
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
                    "rationale": rationale,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=intent,
                        rationale=rationale,
                    ),
                }
                # Persist the full proposal so record_approval_decision can
                # reconstruct the intent after a Fly machine restart.
                self.proposal_store.save_proposal(
                    handoff_id=handoff_id,
                    session_id=session_id,
                    candidate=candidate,
                    proposal_payload=proposal_payload,
                    intent_json=json.dumps(audit_module.to_dict(intent)),
                )
                print(
                    f"proposal saved to db: {handoff_id} "
                    "(sonnet_success)",
                    flush=True,
                )
            else:
                rationale = (
                    proposal_result.error
                    or "Proposal generation requires human review."
                )
                proposal_payload = {
                    "intent_id": "",
                    "intent": None,
                    "validation": None,
                    "token_meta": dict(proposal_result.token_meta),
                    "raw": proposal_result.raw,
                    "error": proposal_result.error,
                    "needs_human": True,
                    "circuit_breaker": cb_payload,
                    "rationale": rationale,
                    "proposal_text": _proposal_text(
                        symbol=candidate["symbol"],
                        side=candidate["side"],
                        confidence=candidate["confidence"],
                        intent=None,
                        rationale=rationale,
                    ),
                }
                # Persist a needs_human marker (no executable intent) so the
                # Sonnet-failure path is visible in fly logs / DB.  Try to
                # recover an intent from the raw output as a best-effort; if
                # it can't be parsed intent_json stays None.
                recovered_intent_json: str | None = None
                try:
                    raw_intent_dict = json.loads(proposal_result.raw)
                    recovered_intent_json = json.dumps(raw_intent_dict)
                except (ValueError, TypeError):
                    pass

                self.proposal_store.save_proposal(
                    handoff_id=handoff_id,
                    session_id=session_id,
                    candidate=candidate,
                    proposal_payload=proposal_payload,
                    intent_json=recovered_intent_json,
                )
                print(
                    f"proposal saved to db: {handoff_id} "
                    "(needs_human=sonnet_error)",
                    flush=True,
                )

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

        # Read proposal from the persistent store (survives Fly machine restarts).
        # A persisted marker with intent_json=None means needs_human — there is
        # no executable intent and we should surface a clear message rather than
        # the generic "proposal expired or not found".
        proposal = self.proposal_store.get_proposal(handoff_id)
        intent_id = ""
        if approved:
            proposed_intent: ExecutionIntent | None = None
            persisted_needs_human = proposal is not None and not proposal.get("intent_json")
            if proposal and proposal.get("intent_json"):
                try:
                    intent_dict = json.loads(proposal["intent_json"])
                    proposed_intent = execution_intent_from_dict(intent_dict)
                except (ValueError, TypeError):
                    proposed_intent = None

            if proposed_intent is None:
                reason_text = (
                    "approval requested but the proposal was flagged "
                    "needs_human and has no executable intent"
                    if persisted_needs_human
                    else "approval requested but no validated proposal is available"
                )
                writer.append_approval_decision(
                    handoff_id=handoff_id,
                    approved=False,
                    approved_by=approved_by,
                    reason=reason_text,
                )
                if persisted_needs_human:
                    return (
                        f"Approval for {handoff_id} blocked: proposal is "
                        "needs_human, no executable intent. Re-triage required."
                    )
                return (
                    f"Approval for {handoff_id} blocked: proposal expired or not "
                    "found, please re-triage. No intent artifact written."
                )

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

                # Write fill artifact. The trade already executed; if the write
                # fails we log it and continue — audit failure must not trigger
                # a retry or block the confirmation returned to Poke.
                try:
                    fill_result = writer.write_fill_artifact(
                        fill, allow_live_fill=True
                    )
                    if fill_result.ok:
                        print(
                            f"fill artifact written: fill_id={fill.fill_id} "
                            f"symbol={fill.symbol}",
                            flush=True,
                        )
                    else:
                        print(
                            f"fill artifact write incomplete for {fill.fill_id}: "
                            + "; ".join(fill_result.reasons),
                            flush=True,
                        )
                except Exception as exc:
                    print(
                        f"fill artifact write failed for {fill.fill_id} "
                        f"(non-blocking): {exc}",
                        flush=True,
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
                self._forget_proposal(handoff_id)
                return (
                    f"Approved {handoff_id}; submitted Alpaca order "
                    f"{fill.order_id} (fill_id={fill.fill_id}) "
                    f"with status {fill.status}."
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
            self._forget_proposal(handoff_id)
            return (
                f"Approved {handoff_id}; wrote dry-run approved intent artifact "
                f"{intent_id}. No broker order was created."
            )
        print(
            f"approval path: rejected by human approval gate for {handoff_id}",
            flush=True,
        )
        self._forget_proposal(handoff_id)
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

    def research_and_surface_candidates(self, query: str) -> dict[str, Any]:
        """Research a symbol via yfinance news and surface a candidate if warranted."""
        query = str(query or "").strip()
        known: frozenset[str] = frozenset(market_feed_module.WATCH_SYMBOLS)
        symbol = _extract_ticker_from_query(query, known)
        if not symbol:
            return {"error": "No ticker symbol found in query", "query": query, "queued": False}

        try:
            import yfinance as yf
        except ImportError:
            return {"error": "yfinance not installed", "symbol": symbol, "queued": False}

        ticker = yf.Ticker(symbol)

        news: list[dict[str, Any]] = []
        try:
            news = list(ticker.news or [])
        except Exception:
            pass

        bullish_total = 0
        bearish_total = 0
        relevant_headlines: list[dict[str, Any]] = []
        for item in news[:10]:
            title = str(item.get("title") or "")
            bull, bear = _headline_sentiment(title)
            if bull > 0 or bear > 0:
                relevant_headlines.append({
                    "title": title,
                    "signal": "bullish" if bull > bear else ("bearish" if bear > bull else "neutral"),
                })
            bullish_total += bull
            bearish_total += bear

        side = "buy" if bullish_total >= bearish_total else "sell"
        net_score = abs(bullish_total - bearish_total)

        price_move: float | None = None
        latest_price: float | None = None
        volume_ratio: float | None = None

        try:
            hourly = ticker.history(period="2d", interval="60m", auto_adjust=False)
            if hourly is not None and not getattr(hourly, "empty", True):
                closes = [float(v) for v in hourly["Close"].dropna().tolist() if v == v]
                if len(closes) >= 2 and closes[-2] > 0:
                    price_move = (closes[-1] - closes[-2]) / closes[-2]
                    latest_price = closes[-1]
        except Exception:
            pass

        try:
            daily = ticker.history(period="30d", interval="1d", auto_adjust=False)
            if daily is not None and not getattr(daily, "empty", True):
                vols = [
                    float(v) for v in daily["Volume"].dropna().tolist()
                    if v == v and v > 0
                ]
                if len(vols) >= 2:
                    avg_vol = sum(vols[:-1]) / len(vols[:-1])
                    if avg_vol > 0:
                        volume_ratio = vols[-1] / avg_vol
        except Exception:
            pass

        confidence: float = 0.55
        confidence += min(net_score * 0.03, 0.15)
        if price_move is not None and abs(price_move) > 0.02:
            confidence += 0.10
            if bullish_total == bearish_total:
                side = "buy" if price_move > 0 else "sell"
        if volume_ratio is not None and volume_ratio > 1.5:
            confidence += min((volume_ratio - 1.5) * 0.05, 0.10)
        confidence = round(min(confidence, 0.95), 4)

        queued = False
        handoff_id = ""
        queue_reason = ""
        if confidence >= _SURFACE_CONFIDENCE_THRESHOLD:
            try:
                session_date = datetime.now(timezone.utc).strftime("%Y%m%d")
                writer = OvernightArtifactWriter(
                    session_id=f"research-{session_date}",
                    artifact_root=self.overnight_root,
                )
                watcher = DeterministicTier0Watcher(
                    run_id=f"research-{_utc_now_iso()}",
                    writer=writer,
                    dry_run=True,
                )
                result = watcher.process_payload({
                    "symbol": symbol,
                    "side": side,
                    "confidence": confidence,
                    "strategy": "research_news_sentiment",
                    "source": "research_and_surface_candidates",
                    "dry_run": True,
                    "metadata": {
                        "query": query,
                        "bullish_headlines": bullish_total,
                        "bearish_headlines": bearish_total,
                        "price_move_pct": round(price_move * 100, 2) if price_move is not None else None,
                        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
                        "latest_price": round(latest_price, 2) if latest_price is not None else None,
                        "relevant_headlines": relevant_headlines[:5],
                    },
                })
                if result.get("status") == "ok":
                    queued = True
                    handoff_id = str(result.get("handoff_id", ""))
                else:
                    queue_reason = result.get("reason", "handoff failed")
            except Exception as exc:
                queue_reason = f"queue write failed: {exc}"
        else:
            queue_reason = (
                f"confidence {confidence:.4f} below surface threshold "
                f"{_SURFACE_CONFIDENCE_THRESHOLD}"
            )

        return {
            "symbol": symbol,
            "side": side,
            "confidence": confidence,
            "queued": queued,
            "handoff_id": handoff_id,
            "queue_reason": queue_reason,
            "headlines_analyzed": len(news),
            "relevant_headlines": relevant_headlines[:5],
            "bullish_count": bullish_total,
            "bearish_count": bearish_total,
            "price_move_pct": round(price_move * 100, 2) if price_move is not None else None,
            "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "latest_price": round(latest_price, 2) if latest_price is not None else None,
            "message": (
                f"Queued {symbol} {side} candidate "
                f"(confidence={confidence}) handoff_id={handoff_id}"
                if queued
                else f"No candidate queued for {symbol}: {queue_reason}"
            ),
        }

    def inject_researched_candidate(
        self,
        symbol: str,
        thesis: str,
        confidence: float,
        trigger_reason: str,
        direction: str,
    ) -> dict[str, Any]:
        """Queue a Poke-researched candidate (Tier 1 → Tier 2 handoff).

        Poke calls this AFTER doing its own free web research. The supplied
        confidence is trusted as-is: Poke-injected candidates BYPASS composite
        scoring entirely (see market_feed.composite_confidence). The circuit
        breaker is checked before queuing; a tripped breaker blocks injection.
        Writes to the same overnight poke queue the scheduler uses.
        """
        symbol = str(symbol or "").strip().upper()
        direction = str(direction or "").strip().lower()
        thesis = str(thesis or "").strip()
        trigger_reason = str(trigger_reason or "").strip()

        universe = frozenset(market_feed_module.ALL_SYMBOLS)
        if symbol not in universe:
            return {"queued": False, "error": f"symbol {symbol!r} not in ticker universe"}
        if direction not in {"buy", "sell"}:
            return {
                "queued": False,
                "error": f"direction must be buy/sell, got {direction!r}",
            }
        try:
            confidence_val = float(confidence)
        except (TypeError, ValueError):
            return {"queued": False, "error": "confidence must be a number"}
        if not (0.0 <= confidence_val <= 1.0):
            return {"queued": False, "error": "confidence must be within [0, 1]"}

        # Circuit breaker gate — fail closed. A tripped breaker blocks queuing so
        # nothing unsafe ever reaches Sonnet.
        observations = build_observations(symbol)
        cb_config, cb_config_error = _load_circuit_breaker_config(self.repo_root)
        cb_result = evaluate_circuit_breaker(cb_config, observations)
        if cb_result.blocked:
            return {
                "queued": False,
                "error": "circuit breaker tripped: " + cb_result.reason,
                "circuit_breaker": _circuit_breaker_payload(
                    cb_result, config_error=cb_config_error
                ),
            }

        # Build the candidate + handoff directly so Poke's thesis and confidence
        # are preserved verbatim (no composite re-scoring).
        # TODO(pydantic-schemas): validate PokeBridgeHandoff with Pydantic v2 here
        session_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        created_at = _utc_now_iso()
        run_id = f"poke-research-{session_date}"
        uid = uuid.uuid4().hex[:12]
        candidate_event_id = f"{run_id}-candidate-{uid}"
        handoff_id = f"{run_id}-handoff-{uid}"
        handoff_metadata = {
            "symbol": symbol,
            "side": direction,
            "confidence": confidence_val,
            "thesis": thesis,
            "trigger_reason": trigger_reason,
            "source": "poke_research",
            "direction": direction,
        }

        try:
            writer = OvernightArtifactWriter(
                session_id=f"research-{session_date}",
                artifact_root=self.overnight_root,
            )
            writer.write_candidate(
                CandidateEvent(
                    event_id=candidate_event_id,
                    run_id=run_id,
                    created_at=created_at,
                    symbol=symbol,
                    side=direction,
                    confidence=confidence_val,
                    source="poke_research",
                    strategy="poke_research",
                    dry_run=True,
                    metadata=dict(handoff_metadata),
                )
            )
            enqueued = writer.enqueue_poke_handoff(
                PokeBridgeHandoff(
                    handoff_id=handoff_id,
                    run_id=run_id,
                    created_at=created_at,
                    candidate_event_id=candidate_event_id,
                    destination="poke_bridge_local_queue",
                    dry_run=True,
                    metadata=dict(handoff_metadata),
                )
            )
        except Exception as exc:
            return {"queued": False, "error": f"queue write failed: {exc}"}

        if not enqueued:
            return {"queued": False, "error": "handoff failed local schema validation"}

        print(
            f"poke research injected: {handoff_id} {symbol} {direction} "
            f"confidence={confidence_val} (composite bypassed)",
            flush=True,
        )
        return {
            "queued": True,
            "candidate_id": handoff_id,
            "handoff_id": handoff_id,
            "symbol": symbol,
            "direction": direction,
            "confidence": confidence_val,
            "source": "poke_research",
            "message": (
                f"Queued Poke-researched {symbol} {direction} candidate "
                f"(confidence={confidence_val}) handoff_id={handoff_id}"
            ),
        }

    def _build_research_package(
        self,
        *,
        symbol: str,
        confidence: float,
        candidate_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble the Tier 2 research context attached to the Sonnet proposal.

        Always includes price data (read from candidate metadata — no extra
        network call), the Poke thesis if present, and PatternTool triggers.
        Tier B adds the technical_analysis_panel swarm; Tier C adds the
        investment_committee swarm. vibe-trading is best-effort and skipped
        silently on any failure.
        """
        metadata = candidate_metadata if isinstance(candidate_metadata, dict) else {}
        package: dict[str, Any] = {
            "symbol": symbol,
            "confidence": confidence,
            "research_tier": _research_tier_label(confidence),
            "poke_thesis": str(metadata.get("thesis") or "").strip(),
            "trigger_reason": str(metadata.get("trigger_reason") or "").strip(),
            "price_data": {
                key: metadata.get(key)
                for key in ("latest_price", "price_move_pct", "volume_ratio")
                if metadata.get(key) is not None
            },
            "pattern": metadata.get("trigger_reasons") or metadata.get("pattern"),
            "vibe_trading_preset": None,
            "vibe_trading": None,
        }
        preset = _select_research_preset(confidence)
        if preset is not None:
            package["vibe_trading_preset"] = preset
            package["vibe_trading"] = call_vibe_trading_swarm(
                preset, _swarm_user_vars(preset, symbol)
            )
        return package

    def get_market_overview(self) -> dict[str, Any]:
        """Return current price, % change, and volume ratio for all watched tickers."""
        symbols = list(market_feed_module.WATCH_SYMBOLS)

        try:
            import yfinance as yf
        except ImportError:
            return {"error": "yfinance not installed", "symbols": symbols, "overview": {}}

        overview: dict[str, Any] = {}
        for symbol in symbols:
            try:
                fast = yf.Ticker(symbol).fast_info

                price: float | None = None
                try:
                    price = float(fast["last_price"])
                except (KeyError, TypeError, ValueError):
                    pass

                prev_close: float | None = None
                try:
                    prev_close = float(fast["previous_close"])
                except (KeyError, TypeError, ValueError):
                    pass

                pct_change: float | None = None
                if price is not None and prev_close and prev_close > 0:
                    pct_change = round((price - prev_close) / prev_close * 100, 2)

                volume_ratio: float | None = None
                try:
                    last_vol = float(fast.get("last_volume") or 0)
                    avg_vol = float(fast.get("three_month_average_volume") or 0)
                    if last_vol > 0 and avg_vol > 0:
                        volume_ratio = round(last_vol / avg_vol, 2)
                except Exception:
                    pass

                overview[symbol] = {
                    "price": round(price, 2) if price is not None else None,
                    "pct_change_today": pct_change,
                    "prev_close": round(prev_close, 2) if prev_close is not None else None,
                    "volume_ratio_3m": volume_ratio,
                    "side": "buy" if (pct_change or 0) >= 0 else "sell",
                    "status": "ok",
                }
            except Exception as exc:
                overview[symbol] = {"status": "error", "error": str(exc)}

        return {
            "symbols": symbols,
            "snapshot_at": _utc_now_iso(),
            "overview": overview,
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


def research_and_surface_candidates(query: str) -> dict[str, Any]:
    return SERVICE.research_and_surface_candidates(query)


def get_market_overview() -> dict[str, Any]:
    return SERVICE.get_market_overview()


def inject_researched_candidate(
    symbol: str,
    thesis: str,
    confidence: float,
    trigger_reason: str,
    direction: str,
) -> dict[str, Any]:
    return SERVICE.inject_researched_candidate(
        symbol=symbol,
        thesis=thesis,
        confidence=confidence,
        trigger_reason=trigger_reason,
        direction=direction,
    )


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
    app.tool()(research_and_surface_candidates)
    app.tool()(get_market_overview)
    app.tool()(inject_researched_candidate)
    return app


def main() -> None:
    start_scheduler_background()
    app = build_mcp_app()
    app.run(transport="sse")


if __name__ == "__main__":
    main()
