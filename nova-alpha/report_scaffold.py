"""Local daily markdown report generator scaffold for nova-alpha.

Consumes only local fixture/tmpdir artifacts (watcher JSONL files, audit
artifact JSON files, optional runtime_state.json) and renders a deterministic
markdown report. NO network calls, NO live broker or account reads: every
number in the report comes from repo-backed files.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


NOVA_ALPHA_REPORT_SCAFFOLD_VERSION = "0.1.0"

CANDIDATE_EVENTS_FILE = "candidate_events.jsonl"
NEEDS_HUMAN_EVENTS_FILE = "needs_human_events.jsonl"
POKE_QUEUE_FILE = "poke_bridge_queue.jsonl"
TRIAGE_DECISIONS_FILE = "triage_decisions.jsonl"
APPROVAL_DECISIONS_FILE = "approval_decisions.jsonl"
RUNTIME_STATE_FILE = "runtime_state.json"


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


def _read_json_artifacts(root: Path, prefix: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(root.rglob(f"{prefix}-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            artifacts.append(payload)
    return artifacts


def _utc_today_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_utc_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    clean = value.strip()
    try:
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.fromisoformat(clean).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else ""


def _artifact_date(payload: dict[str, Any]) -> str:
    for key in ("created_at", "approved_at", "updated_at", "last_health_check"):
        parsed = _parse_utc_date(payload.get(key))
        if parsed:
            return parsed
    intent = payload.get("intent")
    if isinstance(intent, dict):
        parsed = _parse_utc_date(intent.get("created_at") or intent.get("approved_at"))
        if parsed:
            return parsed
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        parsed = _parse_utc_date(candidate.get("created_at"))
        if parsed:
            return parsed
    return ""


def _filter_rows_for_date(rows: list[dict[str, Any]], report_date: str) -> list[dict[str, Any]]:
    return [row for row in rows if _artifact_date(row) == report_date]


def _filter_artifacts_for_date(
    artifacts: dict[str, Any],
    report_date: str,
) -> dict[str, Any]:
    filtered = dict(artifacts)
    for key in (
        "candidates",
        "needs_human",
        "poke_queue",
        "triage_decisions",
        "proposals",
        "approval_decisions",
        "errors",
    ):
        rows = filtered.get(key) or []
        if isinstance(rows, list):
            filtered[key] = _filter_rows_for_date(rows, report_date)
    return filtered


def load_local_artifacts(
    artifact_root: str | Path,
    *,
    today_only: bool = False,
    report_date: str | None = None,
) -> dict[str, Any]:
    """Load watcher/audit/runtime artifacts from a local directory tree.

    Missing files are simply treated as empty sections; a malformed root
    fails closed.
    """
    root = Path(artifact_root)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"artifact root must be an existing directory: {root}")

    candidates: list[dict[str, Any]] = []
    needs_human: list[dict[str, Any]] = []
    poke_queue: list[dict[str, Any]] = []
    triage_decisions_jsonl: list[dict[str, Any]] = []
    approval_decisions: list[dict[str, Any]] = []
    for jsonl_path in sorted(root.rglob(CANDIDATE_EVENTS_FILE)):
        candidates.extend(_read_jsonl(jsonl_path))
    for jsonl_path in sorted(root.rglob(NEEDS_HUMAN_EVENTS_FILE)):
        needs_human.extend(_read_jsonl(jsonl_path))
    for jsonl_path in sorted(root.rglob(POKE_QUEUE_FILE)):
        poke_queue.extend(_read_jsonl(jsonl_path))
    for jsonl_path in sorted(root.rglob(TRIAGE_DECISIONS_FILE)):
        triage_decisions_jsonl.extend(_read_jsonl(jsonl_path))
    for jsonl_path in sorted(root.rglob(APPROVAL_DECISIONS_FILE)):
        approval_decisions.extend(_read_jsonl(jsonl_path))

    decisions = _read_json_artifacts(root, "decision")
    intents = _read_json_artifacts(root, "intent")
    needs_human.extend(_read_json_artifacts(root, "needs_human"))
    errors = _read_json_artifacts(root, "error")

    circuit_breaker_status: Any = {}
    runtime_state_path = root / RUNTIME_STATE_FILE
    if runtime_state_path.exists():
        try:
            state_payload = json.loads(runtime_state_path.read_text(encoding="utf-8"))
        except ValueError:
            state_payload = None
        if isinstance(state_payload, dict):
            status = state_payload.get("circuit_breaker_status")
            if isinstance(status, (dict, str)):
                circuit_breaker_status = status
            if state_payload.get("last_error"):
                errors.append(
                    {
                        "artifact_kind": "runtime_state",
                        "created_at": state_payload.get("updated_at", ""),
                        "error": state_payload.get("last_error"),
                    }
                )

    artifacts = {
        "candidates": candidates,
        "needs_human": needs_human,
        "poke_queue": poke_queue,
        "triage_decisions": triage_decisions_jsonl + decisions,
        "proposals": intents,
        "approval_decisions": approval_decisions,
        "circuit_breaker_status": circuit_breaker_status,
        "errors": errors,
    }
    if today_only:
        return _filter_artifacts_for_date(artifacts, report_date or _utc_today_date())
    return artifacts


def _fmt_confidence(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.2f}"
    return "n/a"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def _candidate_field(event: dict[str, Any], key: str, default: Any = "") -> Any:
    if key in event:
        return event.get(key, default)
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    return default


def _proposal_intent(proposal: dict[str, Any]) -> dict[str, Any]:
    intent = proposal.get("intent")
    return intent if isinstance(intent, dict) else {}


def _is_circuit_breaker_block(payload: dict[str, Any]) -> bool:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    breaker = context.get("circuit_breaker")
    if not isinstance(breaker, dict) and isinstance(payload.get("circuit_breaker"), dict):
        breaker = payload.get("circuit_breaker")
    if isinstance(breaker, dict) and breaker.get("blocked") is True:
        return True
    return (
        payload.get("decision") == "blocked"
        and "circuit_breaker" in str(payload.get("source", "")).lower()
    )


def summarize_daily_counts(artifacts: dict[str, Any]) -> dict[str, int | str]:
    """Return report counts plus the SMS-ready one-line summary."""
    candidates = artifacts.get("candidates") or []
    triage_decisions = artifacts.get("triage_decisions") or []
    proposals = artifacts.get("proposals") or []
    approval_decisions = artifacts.get("approval_decisions") or []

    n_candidates = len(candidates) if isinstance(candidates, list) else 0
    n_proceeded = sum(
        1
        for decision in triage_decisions
        if isinstance(decision, dict) and decision.get("proceed") is True
    )
    n_proposals = len(proposals) if isinstance(proposals, list) else 0
    n_approved = sum(
        1
        for decision in approval_decisions
        if isinstance(decision, dict) and decision.get("approved") is True
    )
    n_rejected = sum(
        1
        for decision in approval_decisions
        if isinstance(decision, dict) and decision.get("approved") is False
    )
    n_circuit_breaker_blocks = sum(
        1
        for decision in triage_decisions
        if isinstance(decision, dict) and _is_circuit_breaker_block(decision)
    )
    n_errors = len(artifacts.get("errors") or [])

    summary_line = (
        f"Today: {n_candidates} {_plural(n_candidates, 'candidate')}, "
        f"{n_proposals} {_plural(n_proposals, 'proposal')}, "
        f"{n_approved} approved, {n_rejected} rejected, "
        f"{n_circuit_breaker_blocks} {_plural(n_circuit_breaker_blocks, 'block')}"
    )
    return {
        "n_candidates": n_candidates,
        "n_proceeded": n_proceeded,
        "n_proposals": n_proposals,
        "n_approved": n_approved,
        "n_rejected": n_rejected,
        "n_circuit_breaker_blocks": n_circuit_breaker_blocks,
        "n_errors": n_errors,
        "summary_line": summary_line,
    }


def has_report_activity(artifacts: dict[str, Any]) -> bool:
    """Return True when the filtered artifact bundle has activity to report."""
    for key in (
        "candidates",
        "needs_human",
        "poke_queue",
        "triage_decisions",
        "proposals",
        "approval_decisions",
        "errors",
    ):
        if artifacts.get(key):
            return True
    return False


def render_daily_report(
    artifacts: dict[str, Any],
    *,
    report_date: str | None = None,
    today_only: bool = False,
) -> str:
    """Render a deterministic local markdown report from loaded artifacts."""
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be a dict produced by load_local_artifacts")
    clean_date = _utc_today_date() if report_date is None else str(report_date).strip()
    if not clean_date:
        raise ValueError("report_date must be non-empty")
    if today_only:
        artifacts = _filter_artifacts_for_date(artifacts, clean_date)

    candidates = artifacts.get("candidates") or []
    needs_human = artifacts.get("needs_human") or []
    poke_queue = artifacts.get("poke_queue") or []
    triage_decisions = artifacts.get("triage_decisions") or []
    proposals = artifacts.get("proposals") or []
    approval_decisions = artifacts.get("approval_decisions") or []
    breaker = artifacts.get("circuit_breaker_status") or {}
    errors = artifacts.get("errors") or []
    counts = summarize_daily_counts(artifacts)

    lines: list[str] = []
    lines.append(f"# Nova Alpha Local Daily Report - {clean_date}")
    lines.append("")
    lines.append("> Generated from local repo-backed artifacts only.")
    lines.append("> No live broker data. No network calls. Dry-run scaffolding.")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(str(counts["summary_line"]))
    lines.append("")
    lines.append(f"- Candidates seen: {len(candidates)}")
    lines.append(f"- Poke queue handoffs (local): {len(poke_queue)}")
    lines.append(f"- Triage decisions: {len(triage_decisions)}")
    lines.append(f"- Proposals recorded: {len(proposals)}")
    lines.append(f"- Approval decisions: {len(approval_decisions)}")
    lines.append(f"- NEEDS_HUMAN events: {len(needs_human)}")
    lines.append(f"- n_candidates: {counts['n_candidates']}")
    lines.append(f"- n_proceeded: {counts['n_proceeded']}")
    lines.append(f"- n_proposals: {counts['n_proposals']}")
    lines.append(f"- n_approved: {counts['n_approved']}")
    lines.append(f"- n_rejected: {counts['n_rejected']}")
    lines.append(f"- n_circuit_breaker_blocks: {counts['n_circuit_breaker_blocks']}")
    lines.append(f"- Errors recorded: {counts['n_errors']}")
    lines.append("")

    lines.append("## Candidates Seen")
    lines.append("")
    if candidates:
        lines.append("| Event ID | Symbol | Side | Confidence | Strategy |")
        lines.append("|----------|--------|------|------------|----------|")
        for event in candidates:
            lines.append(
                "| {event_id} | {symbol} | {side} | {confidence} | {strategy} |".format(
                    event_id=event.get("event_id")
                    or event.get("candidate_event_id")
                    or "unknown",
                    symbol=_candidate_field(event, "symbol", "?"),
                    side=_candidate_field(event, "side", "?"),
                    confidence=_fmt_confidence(_candidate_field(event, "confidence")),
                    strategy=_candidate_field(event, "strategy", "") or "-",
                )
            )
    else:
        lines.append("No candidates recorded.")
    lines.append("")

    lines.append("## Poke Triage Decisions")
    lines.append("")
    if triage_decisions:
        for decision in triage_decisions:
            if "proceed" in decision:
                decision_text = "proceed" if decision.get("proceed") is True else "reject"
                source = decision.get("provider") or decision.get("source") or "poke_mcp"
                rationale = decision.get("reason", "no rationale recorded")
            else:
                decision_text = decision.get("decision", "unknown")
                source = decision.get("source", "") or "unattributed"
                rationale = decision.get("rationale", "no rationale recorded")
            lines.append(
                "- **{decision}** ({source}): {rationale}".format(
                    decision=decision_text,
                    source=source,
                    rationale=rationale,
                )
            )
    else:
        lines.append("No triage decisions recorded.")
    lines.append("")

    lines.append("## Proposal Summaries")
    lines.append("")
    if proposals:
        for proposal in proposals:
            intent = _proposal_intent(proposal)
            validation = proposal.get("validation") or {}
            lines.append(
                "- `{intent_id}` {side} {quantity} {symbol} via {broker}"
                " | status: {status} | allowed: {allowed} | executed: {executed}".format(
                    intent_id=intent.get("intent_id", "unknown"),
                    side=intent.get("side", "?"),
                    quantity=intent.get("quantity", "?"),
                    symbol=intent.get("symbol", "?"),
                    broker=intent.get("broker", "?"),
                    status=validation.get("status", "unknown"),
                    allowed=validation.get("allowed", False),
                    executed=proposal.get("executed", False),
                )
            )
    else:
        lines.append("No proposals recorded.")
    lines.append("")

    lines.append("## Approval Decisions")
    lines.append("")
    if approval_decisions:
        for decision in approval_decisions:
            status = "approved" if decision.get("approved") is True else "rejected"
            lines.append(
                "- **{status}** `{handoff_id}` intent `{intent_id}` by {approved_by}: {reason}".format(
                    status=status,
                    handoff_id=decision.get("handoff_id", "unknown"),
                    intent_id=decision.get("intent_id", "") or "-",
                    approved_by=decision.get("approved_by", "") or "unknown",
                    reason=decision.get("reason", "") or "no reason recorded",
                )
            )
    else:
        lines.append("No approval decisions recorded.")
    lines.append("")

    lines.append("## NEEDS_HUMAN Events")
    lines.append("")
    if needs_human:
        for event in needs_human:
            lines.append(
                "- `{code}`: {reason}".format(
                    code=event.get("reason_code", "UNKNOWN"),
                    reason=event.get("reason", "no reason recorded"),
                )
            )
    else:
        lines.append("No NEEDS_HUMAN events recorded.")
    lines.append("")

    lines.append("## Circuit Breaker Status")
    lines.append("")
    if isinstance(breaker, dict) and breaker:
        lines.append(f"- Enabled: {breaker.get('enabled', False)}")
        lines.append(f"- Allowed: {breaker.get('allowed', True)}")
        lines.append(f"- Reason: {breaker.get('reason', 'n/a')}")
        triggered = breaker.get("triggered_rules") or []
        if triggered:
            lines.append("- Triggered rules:")
            for rule in triggered:
                lines.append(f"  - {rule}")
        else:
            lines.append("- Triggered rules: none")
    elif isinstance(breaker, str) and breaker:
        lines.append(f"- Status: {breaker}")
    else:
        lines.append("No circuit breaker status recorded (default: disabled no-op).")
    lines.append("")

    lines.append("## Errors")
    lines.append("")
    if errors:
        for error in errors:
            lines.append(
                "- {kind}: {error}".format(
                    kind=error.get("artifact_kind", "error"),
                    error=error.get("error") or error.get("message") or "unknown error",
                )
            )
    else:
        lines.append("No errors recorded.")
    lines.append("")

    return "\n".join(lines)


def write_daily_report(
    artifact_root: str | Path,
    output_path: str | Path,
    *,
    report_date: str | None = None,
    today_only: bool = False,
) -> Path:
    """Load local artifacts, render the report, and write it to output_path."""
    clean_date = report_date or _utc_today_date()
    artifacts = load_local_artifacts(
        artifact_root,
        today_only=today_only,
        report_date=clean_date,
    )
    report = render_daily_report(
        artifacts,
        report_date=clean_date,
        today_only=today_only,
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    return out
