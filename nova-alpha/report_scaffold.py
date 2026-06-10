"""Local daily markdown report generator scaffold for nova-alpha.

Consumes only local fixture/tmpdir artifacts (watcher JSONL files, audit
artifact JSON files, optional runtime_state.json) and renders a deterministic
markdown report. NO network calls, NO scheduler wiring, NO live broker or
account reads: every number in the report comes from repo-backed files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


NOVA_ALPHA_REPORT_SCAFFOLD_VERSION = "0.1.0"

CANDIDATE_EVENTS_FILE = "candidate_events.jsonl"
NEEDS_HUMAN_EVENTS_FILE = "needs_human_events.jsonl"
POKE_QUEUE_FILE = "poke_bridge_queue.jsonl"
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


def load_local_artifacts(artifact_root: str | Path) -> dict[str, Any]:
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
    for jsonl_path in sorted(root.rglob(CANDIDATE_EVENTS_FILE)):
        candidates.extend(_read_jsonl(jsonl_path))
    for jsonl_path in sorted(root.rglob(NEEDS_HUMAN_EVENTS_FILE)):
        needs_human.extend(_read_jsonl(jsonl_path))
    for jsonl_path in sorted(root.rglob(POKE_QUEUE_FILE)):
        poke_queue.extend(_read_jsonl(jsonl_path))

    decisions = _read_json_artifacts(root, "decision")
    intents = _read_json_artifacts(root, "intent")
    needs_human.extend(_read_json_artifacts(root, "needs_human"))

    circuit_breaker_status: dict[str, Any] = {}
    runtime_state_path = root / RUNTIME_STATE_FILE
    if runtime_state_path.exists():
        try:
            state_payload = json.loads(runtime_state_path.read_text(encoding="utf-8"))
        except ValueError:
            state_payload = None
        if isinstance(state_payload, dict):
            status = state_payload.get("circuit_breaker_status")
            if isinstance(status, dict):
                circuit_breaker_status = status

    return {
        "candidates": candidates,
        "needs_human": needs_human,
        "poke_queue": poke_queue,
        "triage_decisions": decisions,
        "proposals": intents,
        "circuit_breaker_status": circuit_breaker_status,
    }


def _fmt_confidence(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.2f}"
    return "n/a"


def render_daily_report(artifacts: dict[str, Any], *, report_date: str) -> str:
    """Render a deterministic local markdown report from loaded artifacts."""
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be a dict produced by load_local_artifacts")
    clean_date = str(report_date or "").strip()
    if not clean_date:
        raise ValueError("report_date must be non-empty")

    candidates = artifacts.get("candidates") or []
    needs_human = artifacts.get("needs_human") or []
    poke_queue = artifacts.get("poke_queue") or []
    triage_decisions = artifacts.get("triage_decisions") or []
    proposals = artifacts.get("proposals") or []
    breaker = artifacts.get("circuit_breaker_status") or {}

    lines: list[str] = []
    lines.append(f"# Nova Alpha Local Daily Report - {clean_date}")
    lines.append("")
    lines.append("> Generated from local repo-backed artifacts only.")
    lines.append("> No live broker data. No network calls. Dry-run scaffolding.")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Candidates seen: {len(candidates)}")
    lines.append(f"- Poke queue handoffs (local): {len(poke_queue)}")
    lines.append(f"- Triage decisions: {len(triage_decisions)}")
    lines.append(f"- Proposals recorded: {len(proposals)}")
    lines.append(f"- NEEDS_HUMAN events: {len(needs_human)}")
    lines.append("")

    lines.append("## Candidates Seen")
    lines.append("")
    if candidates:
        lines.append("| Event ID | Symbol | Side | Confidence | Strategy |")
        lines.append("|----------|--------|------|------------|----------|")
        for event in candidates:
            lines.append(
                "| {event_id} | {symbol} | {side} | {confidence} | {strategy} |".format(
                    event_id=event.get("event_id", "unknown"),
                    symbol=event.get("symbol", "?"),
                    side=event.get("side", "?"),
                    confidence=_fmt_confidence(event.get("confidence")),
                    strategy=event.get("strategy", "") or "-",
                )
            )
    else:
        lines.append("No candidates recorded.")
    lines.append("")

    lines.append("## Poke Triage Decisions")
    lines.append("")
    if triage_decisions:
        for decision in triage_decisions:
            lines.append(
                "- **{decision}** ({source}): {rationale}".format(
                    decision=decision.get("decision", "unknown"),
                    source=decision.get("source", "") or "unattributed",
                    rationale=decision.get("rationale", "no rationale recorded"),
                )
            )
    else:
        lines.append("No triage decisions recorded.")
    lines.append("")

    lines.append("## Proposal Summaries")
    lines.append("")
    if proposals:
        for proposal in proposals:
            intent = proposal.get("intent") or {}
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
    if breaker:
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
    else:
        lines.append("No circuit breaker status recorded (default: disabled no-op).")
    lines.append("")

    return "\n".join(lines)


def write_daily_report(
    artifact_root: str | Path,
    output_path: str | Path,
    *,
    report_date: str,
) -> Path:
    """Load local artifacts, render the report, and write it to output_path."""
    artifacts = load_local_artifacts(artifact_root)
    report = render_daily_report(artifacts, report_date=report_date)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    return out
