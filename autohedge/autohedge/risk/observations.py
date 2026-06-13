"""Local audit-backed observations for circuit breaker evaluation.

This module performs NO broker calls and NO network access. It derives a small
set of risk observations from repo-backed audit fill artifacts only, returning
safe zeros when files are missing or malformed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    configured = os.getenv("DAVEY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if any(os.getenv(name) for name in ("FLY_APP_NAME", "FLY_MACHINE_ID", "FLY_REGION")):
        return Path("/app")
    return Path(__file__).resolve().parents[3]


def _safe_zero_observations(symbol: str) -> dict[str, Any]:
    return {
        "symbol": str(symbol or "").strip().upper(),
        "consecutive_losses": 0,
        "daily_loss_pct": 0.0,
        "open_trades": 0,
    }


def _latest_session_dir(audit_root: Path) -> Path | None:
    if not audit_root.exists():
        return None
    try:
        candidates = [path for path in audit_root.iterdir() if path.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def _json_objects(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix == ".jsonl":
            rows: list[dict[str, Any]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
            return rows
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [payload] if isinstance(payload, dict) else []


def _fill_payload(row: dict[str, Any]) -> dict[str, Any]:
    fill = row.get("fill")
    return fill if isinstance(fill, dict) else row


def _created_at(fill: dict[str, Any], row: dict[str, Any]) -> str:
    for key in ("filled_at", "created_at"):
        value = fill.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = row.get("created_at")
    return value.strip() if isinstance(value, str) else ""


def _parse_day(value: str) -> str:
    if not value:
        return ""
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return ""


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _metadata_pnl_pct(fill: dict[str, Any]) -> float | None:
    metadata = fill.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    for key in (
        "realized_pnl_pct",
        "pnl_pct",
        "return_pct",
        "daily_loss_pct",
    ):
        parsed = _finite_float(metadata.get(key))
        if parsed is not None:
            return parsed
    return None


def _quantity(fill: dict[str, Any]) -> float:
    parsed = _finite_float(fill.get("quantity"))
    return parsed if parsed is not None and parsed > 0 else 0.0


def _iter_fill_rows(session_dir: Path) -> list[dict[str, Any]]:
    fill_dir = session_dir / "fill_artifacts"
    paths: list[Path] = []
    if fill_dir.exists():
        try:
            paths.extend(path for path in fill_dir.iterdir() if path.is_file())
        except OSError:
            return []
    else:
        try:
            paths.extend(path for path in session_dir.glob("fill-*.json") if path.is_file())
        except OSError:
            return []

    rows: list[dict[str, Any]] = []
    for path in sorted(paths):
        rows.extend(_json_objects(path))
    return rows


def build_observations(symbol: str) -> dict[str, Any]:
    """Build circuit-breaker observations from the latest local audit session.

    Missing files, malformed JSON, unexpected shapes, and parse errors all
    return safe zeros. The output keys intentionally match
    ``evaluate_circuit_breaker`` observation inputs.
    """
    observations = _safe_zero_observations(symbol)
    clean_symbol = observations["symbol"]
    if not clean_symbol:
        return observations

    try:
        latest_session = _latest_session_dir(_repo_root() / "logs" / "audit")
        if latest_session is None:
            return observations

        fills: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for row in _iter_fill_rows(latest_session):
            fill = _fill_payload(row)
            if str(fill.get("symbol", "")).strip().upper() != clean_symbol:
                continue
            fills.append((_created_at(fill, row), fill, row))
        if not fills:
            return observations

        fills.sort(key=lambda item: item[0])
        consecutive_losses = 0
        for _, fill, _ in reversed(fills):
            pnl_pct = _metadata_pnl_pct(fill)
            if pnl_pct is None:
                continue
            if pnl_pct < 0:
                consecutive_losses += 1
                continue
            break

        latest_day = _parse_day(fills[-1][0])
        daily_loss_pct = 0.0
        net_quantity = 0.0
        for created_at, fill, _ in fills:
            pnl_pct = _metadata_pnl_pct(fill)
            if latest_day and _parse_day(created_at) == latest_day and pnl_pct is not None and pnl_pct < 0:
                daily_loss_pct += abs(pnl_pct)

            side = str(fill.get("side", "")).strip().lower()
            quantity = _quantity(fill)
            if side == "buy":
                net_quantity += quantity
            elif side == "sell":
                net_quantity -= quantity

        observations["consecutive_losses"] = consecutive_losses
        observations["daily_loss_pct"] = float(daily_loss_pct)
        observations["open_trades"] = 1 if net_quantity > 0 else 0
        return observations
    except Exception:
        return _safe_zero_observations(symbol)
