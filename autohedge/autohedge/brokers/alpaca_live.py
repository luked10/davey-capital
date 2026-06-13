"""Reviewed Alpaca paper/live execution wrapper.

This module is intentionally small and gate-heavy. It is the only Alpaca path
that may submit non-dry-run orders, and it requires ``DAVEY_LIVE_MODE=1`` even
for Alpaca paper trading. Real-money routing additionally requires
``ALPACA_LIVE_TRADING=1``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
from typing import Any, Callable
from urllib import error, request

from contracts.bridge_contract import (
    ExecutionIntent,
    FillRecord,
    execution_intent_to_broker_order,
    validate_execution_intent,
)


MAX_ORDER_NOTIONAL_USD = 200.0
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"


Submitter = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


def _load_local_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_broker_dependencies():
    try:
        from autohedge.audit.artifacts import AuditArtifactWriter
        from autohedge.brokers.alpaca_agent import AlpacaBrokerAgent
        from autohedge.brokers.base_agent import BrokerOrderAgent

        return AuditArtifactWriter, AlpacaBrokerAgent, BrokerOrderAgent
    except Exception:
        package_root = Path(__file__).resolve().parents[1]
        autohedge_pkg = sys.modules.get("autohedge") or types.ModuleType("autohedge")
        autohedge_pkg.__path__ = [str(package_root)]  # type: ignore[attr-defined]
        sys.modules["autohedge"] = autohedge_pkg
        for package_name, relative in (
            ("autohedge.audit", "audit"),
            ("autohedge.brokers", "brokers"),
        ):
            package = sys.modules.get(package_name) or types.ModuleType(package_name)
            package.__path__ = [str(package_root / relative)]  # type: ignore[attr-defined]
            sys.modules[package_name] = package

        base_module = _load_local_module(
            "autohedge.brokers.base_agent",
            package_root / "brokers" / "base_agent.py",
        )
        alpaca_module = _load_local_module(
            "autohedge.brokers.alpaca_agent",
            package_root / "brokers" / "alpaca_agent.py",
        )
        audit_module = _load_local_module(
            "autohedge.audit.artifacts",
            package_root / "audit" / "artifacts.py",
        )
        return (
            audit_module.AuditArtifactWriter,
            alpaca_module.AlpacaBrokerAgent,
            base_module.BrokerOrderAgent,
        )


AuditArtifactWriter, AlpacaBrokerAgent, BrokerOrderAgent = _load_broker_dependencies()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip() == "1"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _positive_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_artifact_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    return cleaned or f"alpaca-attempt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


class AlpacaLiveBroker:
    """Thin live-capable wrapper around ``AlpacaBrokerAgent``."""

    def __init__(
        self,
        *,
        session_id: str = "alpaca-live",
        artifact_root: str | Path = "logs/audit",
        submitter: Submitter | None = None,
    ) -> None:
        self.session_id = _clean_text(session_id) or "alpaca-live"
        self.artifact_root = Path(artifact_root)
        self.api_key = _clean_text(os.getenv("ALPACA_API_KEY"))
        self.api_secret = _clean_text(
            os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_API_SECRET")
        )
        self.live_mode = _truthy_env("DAVEY_LIVE_MODE")
        self.live_trading = _truthy_env("ALPACA_LIVE_TRADING")
        self.base_url = ALPACA_LIVE_URL if self.live_trading else ALPACA_PAPER_URL
        self._submitter = submitter or self._post_order
        self._writer = AuditArtifactWriter(
            session_id=self.session_id,
            artifact_root=self.artifact_root,
            model="",
            provider="alpaca_live",
        )
        self._agent = AlpacaBrokerAgent(
            session_id=self.session_id,
            config={
                "ALPACA_DRY_RUN": "false",
                "ALPACA_API_KEY": self.api_key,
                "ALPACA_API_SECRET": self.api_secret,
                "ALPACA_BASE_URL": self.base_url,
                "ALPACA_LIVE_ENABLED": "true",
                "live_executor": self._execute_order,
            },
        )

    def _log_attempt(
        self,
        *,
        intent: ExecutionIntent,
        decision: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._writer.write_decision_artifact(
            decision_id=_safe_artifact_id(f"alpaca-order-attempt-{intent.intent_id}"),
            decision=decision,
            rationale=rationale,
            source="alpaca_live",
            context={
                "intent_id": intent.intent_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "order_type": intent.order_type,
                "paper_trading": not self.live_trading,
                "live_trading": self.live_trading,
                "base_url": self.base_url,
                **dict(context or {}),
            },
        )

    def _estimate_notional(self, intent: ExecutionIntent, order: dict[str, Any]) -> float | None:
        metadata = dict(intent.metadata or {})
        notional = _positive_float(metadata.get("notional"))
        if notional is not None:
            return notional
        quantity = _positive_float(order.get("quantity"))
        price = _positive_float(order.get("limit_price"))
        if price is None:
            for key in ("estimated_price", "price", "max_price"):
                price = _positive_float(metadata.get(key))
                if price is not None:
                    break
        if quantity is None or price is None:
            return None
        return quantity * price

    def _validate_for_submission(self, intent: ExecutionIntent) -> tuple[dict[str, Any], float]:
        if not self.live_mode:
            raise ValueError("DAVEY_LIVE_MODE=1 is required before Alpaca execution")
        if not self.api_key or not self.api_secret:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")

        validation = validate_execution_intent(intent)
        normalized = validation.require_allowed()
        if normalized.dry_run is True:
            raise ValueError("Alpaca execution requires a non-dry-run approved intent")
        if normalized.approved is not True:
            raise ValueError("approved=True is required before Alpaca execution")

        order = execution_intent_to_broker_order(normalized)
        notional = self._estimate_notional(normalized, order)
        if notional is None:
            raise ValueError(
                "unable to enforce $200 max notional without limit_price, "
                "metadata.notional, or metadata.estimated_price"
            )
        if notional > MAX_ORDER_NOTIONAL_USD:
            raise ValueError(
                f"order notional ${notional:.2f} exceeds hard ${MAX_ORDER_NOTIONAL_USD:.0f} cap"
            )
        return order, notional

    def submit_order(self, intent: ExecutionIntent) -> FillRecord:
        """Submit an approved non-dry-run intent to Alpaca paper/live trading."""
        order_payload: dict[str, Any] = {}
        try:
            order_payload, notional = self._validate_for_submission(intent)
            order = BrokerOrderAgent(
                symbol=str(order_payload["symbol"]),
                side=str(order_payload["side"]),
                quantity=float(order_payload["quantity"]),
                order_type=str(order_payload["order_type"]),
                limit_price=order_payload.get("limit_price"),
                time_in_force=str(order_payload.get("time_in_force", "day")),
                asset_class=str(order_payload.get("asset_class", "stock")),
                metadata={
                    **dict(order_payload.get("metadata", {})),
                    "estimated_notional": notional,
                },
            )
            response = self._agent.execute(order, live=True)
            fill = self._fill_from_response(intent, order, response)
            fill_result = self._writer.write_fill_artifact(fill, allow_live_fill=True)
            if not fill_result.ok:
                raise RuntimeError("fill artifact write failed: " + "; ".join(fill_result.reasons))
            self._log_attempt(
                intent=intent,
                decision="submitted",
                rationale=f"Alpaca order submitted with status {fill.status}",
                context={
                    "order_id": fill.order_id,
                    "status": fill.status,
                    "estimated_notional": notional,
                },
            )
            return fill
        except Exception as exc:
            self._log_attempt(
                intent=intent,
                decision="blocked_or_failed",
                rationale=str(exc),
                context={"order": order_payload},
            )
            raise

    def _execute_order(self, order: BrokerOrderAgent) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type,
            "time_in_force": order.time_in_force,
            "qty": str(order.quantity),
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(order.limit_price)
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return self._submitter(f"{self.base_url}/v2/orders", payload, headers)

    def _post_order(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Alpaca order rejected: HTTP {exc.code}: {body}") from exc
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("Alpaca order response was not a JSON object")
        return parsed

    def _fill_from_response(
        self,
        intent: ExecutionIntent,
        order: BrokerOrderAgent,
        response: dict[str, Any],
    ) -> FillRecord:
        order_id = _clean_text(response.get("id") or response.get("order_id"))
        if not order_id:
            raise RuntimeError("Alpaca order response missing order id")
        status = _clean_text(response.get("status")) or "submitted"
        filled_at = (
            _clean_text(response.get("filled_at"))
            or _clean_text(response.get("submitted_at"))
            or _utc_now_iso()
        )
        price = _positive_float(response.get("filled_avg_price"))
        if price is None:
            price = _positive_float(order.limit_price)
        return FillRecord(
            fill_id=_safe_artifact_id(f"alpaca-{order_id}"),
            intent_id=intent.intent_id,
            broker="alpaca",
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=float(order.quantity),
            filled_at=filled_at,
            price=price,
            fee=None,
            status=status,
            dry_run=False,
            metadata={
                "paper_trading": not self.live_trading,
                "live_trading": self.live_trading,
                "base_url": self.base_url,
                "raw_order": {
                    key: value
                    for key, value in response.items()
                    if key
                    in {
                        "id",
                        "client_order_id",
                        "created_at",
                        "submitted_at",
                        "filled_at",
                        "expired_at",
                        "canceled_at",
                        "failed_at",
                        "status",
                        "symbol",
                        "side",
                        "type",
                        "qty",
                        "filled_qty",
                        "limit_price",
                        "filled_avg_price",
                    }
                },
                "broker_order": asdict(order),
            },
        )


def submit_order(intent: ExecutionIntent) -> FillRecord:
    """Convenience wrapper for scripts and MCP call sites."""
    return AlpacaLiveBroker().submit_order(intent)
