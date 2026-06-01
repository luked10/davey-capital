"""Deterministic local watcher + poke handoff scaffolding.

The scaffolding here must remain local-only and dry-run by default.
No network calls are performed in this module.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from contracts.overnight_scaffold import (
    CandidateEvent,
    NeedsHumanEvent,
    PokeBridgeHandoff,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


class OvernightArtifactWriter:
    """Append-only artifact writer for deterministic local watcher output."""

    def __init__(
        self,
        *,
        session_id: str,
        artifact_root: str | Path = "logs/overnight",
    ) -> None:
        clean_session_id = _clean_text(session_id) or "default-session"
        self.session_id = clean_session_id
        self.artifact_dir = Path(artifact_root) / clean_session_id
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.candidate_path = self.artifact_dir / "candidate_events.jsonl"
        self.needs_human_path = self.artifact_dir / "needs_human_events.jsonl"
        self.poke_queue_path = self.artifact_dir / "poke_bridge_queue.jsonl"
        self.run_summary_path = self.artifact_dir / "watcher_runs.jsonl"

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def write_candidate(self, event: CandidateEvent) -> None:
        self._append_jsonl(self.candidate_path, asdict(event))

    def write_needs_human(self, event: NeedsHumanEvent) -> None:
        self._append_jsonl(self.needs_human_path, asdict(event))

    def enqueue_poke_handoff(self, event: PokeBridgeHandoff) -> None:
        self._append_jsonl(self.poke_queue_path, asdict(event))

    def write_run_summary(self, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.run_summary_path, payload)


class DeterministicTier0Watcher:
    """Tier0 watcher scaffold that processes local payloads only."""

    def __init__(
        self,
        *,
        run_id: str,
        writer: OvernightArtifactWriter,
        dry_run: bool = True,
        enable_poke_handoff: bool = True,
    ) -> None:
        self.run_id = _clean_text(run_id) or "watcher-run"
        self.writer = writer
        self.dry_run = bool(dry_run)
        self.enable_poke_handoff = bool(enable_poke_handoff)
        self._candidate_seq = 0
        self._needs_human_seq = 0
        self._handoff_seq = 0

    def _next_candidate_id(self) -> str:
        self._candidate_seq += 1
        return f"{self.run_id}-candidate-{self._candidate_seq:04d}"

    def _next_needs_human_id(self) -> str:
        self._needs_human_seq += 1
        return f"{self.run_id}-needs-human-{self._needs_human_seq:04d}"

    def _next_handoff_id(self) -> str:
        self._handoff_seq += 1
        return f"{self.run_id}-handoff-{self._handoff_seq:04d}"

    def _build_candidate_event(self, payload: dict[str, Any]) -> CandidateEvent:
        symbol = _clean_text(payload.get("symbol")).upper()
        side = _clean_text(payload.get("side")).lower()
        strategy = _clean_text(payload.get("strategy"))
        source = _clean_text(payload.get("source")) or "tier0_watcher_scaffold"
        confidence_raw = payload.get("confidence")

        if not symbol:
            raise ValueError("symbol is required")
        if side not in {"buy", "sell"}:
            raise ValueError(f"side must be buy/sell, got {payload.get('side')!r}")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be a number") from exc
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be within [0, 1]")

        return CandidateEvent(
            event_id=self._next_candidate_id(),
            run_id=self.run_id,
            created_at=_utc_now_iso(),
            symbol=symbol,
            side=side,
            confidence=confidence,
            source=source,
            strategy=strategy,
            dry_run=self.dry_run,
            metadata=dict(payload.get("metadata") or {}),
        )

    def _build_needs_human_event(
        self,
        *,
        reason_code: str,
        reason: str,
        source_event_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> NeedsHumanEvent:
        return NeedsHumanEvent(
            needs_human_id=self._next_needs_human_id(),
            run_id=self.run_id,
            created_at=_utc_now_iso(),
            reason_code=reason_code,
            reason=reason,
            source_event_id=source_event_id,
            dry_run=self.dry_run,
            metadata={"payload": payload or {}},
        )

    def _build_poke_handoff(self, event: CandidateEvent) -> PokeBridgeHandoff:
        return PokeBridgeHandoff(
            handoff_id=self._next_handoff_id(),
            run_id=self.run_id,
            created_at=_utc_now_iso(),
            candidate_event_id=event.event_id,
            destination="poke_bridge_local_queue",
            dry_run=self.dry_run,
            metadata={
                "symbol": event.symbol,
                "side": event.side,
                "confidence": event.confidence,
            },
        )

    def process_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = self._build_candidate_event(payload)
        except Exception as exc:
            needs_human = self._build_needs_human_event(
                reason_code="CANDIDATE_VALIDATION_ERROR",
                reason=str(exc),
                payload=payload,
            )
            self.writer.write_needs_human(needs_human)
            return {
                "status": "needs_human",
                "needs_human_id": needs_human.needs_human_id,
                "reason": needs_human.reason,
            }

        self.writer.write_candidate(event)
        handoff_id = ""
        if self.enable_poke_handoff:
            handoff = self._build_poke_handoff(event)
            self.writer.enqueue_poke_handoff(handoff)
            handoff_id = handoff.handoff_id

        return {
            "status": "ok",
            "event_id": event.event_id,
            "handoff_id": handoff_id,
        }

    def run_once(self, payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
        results = [self.process_payload(payload) for payload in payloads]
        ok_count = sum(1 for result in results if result.get("status") == "ok")
        needs_human_count = sum(
            1 for result in results if result.get("status") == "needs_human"
        )
        summary = {
            "run_id": self.run_id,
            "session_id": self.writer.session_id,
            "created_at": _utc_now_iso(),
            "dry_run": self.dry_run,
            "network_enabled": False,
            "enable_poke_handoff": self.enable_poke_handoff,
            "processed": len(results),
            "ok": ok_count,
            "needs_human": needs_human_count,
        }
        self.writer.write_run_summary(summary)
        return {"summary": summary, "results": results}
