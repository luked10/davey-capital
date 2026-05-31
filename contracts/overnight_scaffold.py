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
