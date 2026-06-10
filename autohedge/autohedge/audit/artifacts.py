"""Repo-backed audit artifact writer (local-only, no live execution required).

Writes decision / error / proposed-intent / fill-shape artifacts as JSON files
under a repo-backed directory (logs/ or sessions/ in real use; tmp dirs in
tests). File names are deterministic: ``{kind}-{artifact_id}.json``.

This module performs NO broker calls and NO network access. Fill artifacts
are shape-only: a fill payload that is not explicitly ``dry_run=True`` fails
closed, so a real fill can never be recorded through this scaffold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from contracts.bridge_contract import (
    ExecutionIntent,
    FillRecord,
    RiskSummary,
    RunMetadata,
    to_dict,
    validate_execution_intent,
)


AUDIT_ARTIFACTS_SCAFFOLD_VERSION = "0.1.0"

_ARTIFACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(slots=True)
class AuditWriteResult:
    ok: bool
    path: Path | None
    artifact_kind: str
    artifact_id: str
    reasons: tuple[str, ...] = ()


class AuditArtifactWriter:
    """Deterministic local writer for repo-backed audit artifacts."""

    def __init__(
        self,
        *,
        session_id: str,
        artifact_root: str | Path = "logs/audit",
        model: str = "",
        provider: str = "",
    ) -> None:
        clean_session_id = _clean_text(session_id) or "default-session"
        self.session_id = clean_session_id
        self.artifact_dir = Path(artifact_root) / clean_session_id
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.model = _clean_text(model)
        self.provider = _clean_text(provider)

    def _envelope(self, artifact_kind: str, *, created_at: str | None = None) -> dict[str, Any]:
        return {
            "artifact_kind": artifact_kind,
            "scaffold_version": AUDIT_ARTIFACTS_SCAFFOLD_VERSION,
            "session_id": self.session_id,
            "created_at": created_at if created_at is not None else _utc_now_iso(),
            "model": self.model,
            "provider": self.provider,
            "dry_run": True,
            "network_enabled": False,
        }

    def _write(
        self,
        *,
        artifact_kind: str,
        artifact_id: str,
        body: dict[str, Any],
        reasons: list[str],
        created_at: str | None = None,
    ) -> AuditWriteResult:
        clean_id = _clean_text(artifact_id)
        if not clean_id or not _ARTIFACT_ID_PATTERN.match(clean_id):
            reasons.append(f"artifact_id must be a safe non-empty slug, got {artifact_id!r}")

        if reasons:
            return AuditWriteResult(
                ok=False,
                path=None,
                artifact_kind=artifact_kind,
                artifact_id=clean_id,
                reasons=tuple(reasons),
            )

        payload = self._envelope(artifact_kind, created_at=created_at)
        payload.update(body)
        path = self.artifact_dir / f"{artifact_kind}-{clean_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return AuditWriteResult(
            ok=True,
            path=path,
            artifact_kind=artifact_kind,
            artifact_id=clean_id,
        )

    def write_decision_artifact(
        self,
        *,
        decision_id: str,
        decision: str,
        rationale: str,
        source: str = "",
        context: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> AuditWriteResult:
        reasons: list[str] = []
        clean_decision = _clean_text(decision)
        clean_rationale = _clean_text(rationale)
        if not clean_decision:
            reasons.append("decision must be non-empty")
        if not clean_rationale:
            reasons.append("rationale must be non-empty")
        if context is not None and not isinstance(context, dict):
            reasons.append("context must be a dict when provided")
            context = None
        return self._write(
            artifact_kind="decision",
            artifact_id=decision_id,
            body={
                "decision": clean_decision,
                "rationale": clean_rationale,
                "source": _clean_text(source),
                "context": dict(context or {}),
            },
            reasons=reasons,
            created_at=created_at,
        )

    def write_needs_human_artifact(
        self,
        *,
        needs_human_id: str,
        reason_code: str,
        reason: str,
        source_event_id: str = "",
        context: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> AuditWriteResult:
        reasons: list[str] = []
        clean_code = _clean_text(reason_code)
        clean_reason = _clean_text(reason)
        if not clean_code:
            reasons.append("reason_code must be non-empty")
        if not clean_reason:
            reasons.append("reason must be non-empty")
        if context is not None and not isinstance(context, dict):
            reasons.append("context must be a dict when provided")
            context = None
        return self._write(
            artifact_kind="needs_human",
            artifact_id=needs_human_id,
            body={
                "reason_code": clean_code,
                "reason": clean_reason,
                "source_event_id": _clean_text(source_event_id),
                "context": dict(context or {}),
            },
            reasons=reasons,
            created_at=created_at,
        )

    def write_intent_artifact(
        self,
        intent: ExecutionIntent,
        *,
        risk: RiskSummary | None = None,
        run: RunMetadata | None = None,
        created_at: str | None = None,
    ) -> AuditWriteResult:
        """Record a PROPOSED ExecutionIntent alongside its validation verdict.

        Recording never executes the intent; validation status is captured so
        the audit trail shows whether the proposal would have been blocked.
        """
        reasons: list[str] = []
        if not isinstance(intent, ExecutionIntent):
            return AuditWriteResult(
                ok=False,
                path=None,
                artifact_kind="intent",
                artifact_id="",
                reasons=("intent must be an ExecutionIntent instance",),
            )

        validation = validate_execution_intent(intent, risk=risk, run=run)
        return self._write(
            artifact_kind="intent",
            artifact_id=intent.intent_id,
            body={
                "intent": to_dict(intent),
                "validation": {
                    "allowed": validation.allowed,
                    "needs_human": validation.needs_human,
                    "status": validation.status,
                    "reasons": list(validation.reasons),
                },
                "executed": False,
            },
            reasons=reasons,
            created_at=created_at,
        )

    def write_fill_artifact(
        self,
        fill: FillRecord,
        *,
        created_at: str | None = None,
    ) -> AuditWriteResult:
        """Record a fake/local fill SHAPE only. Non-dry-run fills fail closed."""
        if not isinstance(fill, FillRecord):
            return AuditWriteResult(
                ok=False,
                path=None,
                artifact_kind="fill",
                artifact_id="",
                reasons=("fill must be a FillRecord instance",),
            )

        reasons: list[str] = []
        if fill.dry_run is not True:
            reasons.append("fill artifacts are shape-only: fill.dry_run must be True (bool)")
        if not _clean_text(fill.intent_id):
            reasons.append("fill.intent_id must be non-empty")
        if not _clean_text(fill.symbol):
            reasons.append("fill.symbol must be non-empty")
        return self._write(
            artifact_kind="fill",
            artifact_id=fill.fill_id,
            body={"fill": to_dict(fill), "fake_local_only": True},
            reasons=reasons,
            created_at=created_at,
        )
