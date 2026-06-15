#!/usr/bin/env python3
"""Deterministic offline smoke for inject_researched_candidate.

Verifies:
  - A Poke-researched candidate is queued with the confidence Poke provides
    (composite scoring is NOT run on injected candidates).
  - Composite scoring is bypassed (monkeypatched to raise; injection still ok).
  - Input validation (universe, direction, confidence) fails closed.
  - WATCH_ONLY tickers can be injected manually.
  - A tripped circuit breaker blocks injection (nothing written to the queue).

No network calls, no broker calls, no Sonnet calls.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stub_mcp() -> None:
    if "mcp" in sys.modules:
        return
    mcp_stub = types.ModuleType("mcp")
    mcp_stub.server = types.ModuleType("mcp.server")
    mcp_stub.server.fastmcp = types.ModuleType("mcp.server.fastmcp")
    sys.modules["mcp"] = mcp_stub
    sys.modules["mcp.server"] = mcp_stub.server
    sys.modules["mcp.server.fastmcp"] = mcp_stub.server.fastmcp


def _all_handoffs(overnight_root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in overnight_root.rglob("poke_bridge_queue.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _find_handoff(overnight_root: Path, handoff_id: str) -> dict | None:
    for row in _all_handoffs(overnight_root):
        if row.get("handoff_id") == handoff_id:
            return row
    return None


def main() -> None:
    _stub_mcp()
    server = _load_module("davey_poke_inject_smoke_server", REPO_ROOT / "mcp_server" / "server.py")

    with tempfile.TemporaryDirectory(prefix="poke-inject-smoke-") as tmp:
        root = Path(tmp)
        service = server.PokeBridgeService(repo_root=root)
        overnight_root = service.overnight_root

        # 1) Valid injection — confidence preserved verbatim (composite never
        #    produces 0.73, so this also proves no re-scoring).
        thesis = "Datacenter demand inflection; channel checks show Q3 pull-ins."
        res = service.inject_researched_candidate(
            symbol="nvda",
            thesis=thesis,
            confidence=0.73,
            trigger_reason="poke_web_research",
            direction="buy",
        )
        assert res["queued"] is True, res
        assert res["candidate_id"], res
        assert res["confidence"] == 0.73, res
        assert res["source"] == "poke_research", res

        handoff = _find_handoff(overnight_root, res["handoff_id"])
        assert handoff is not None, "injected handoff must be on the queue"
        md = handoff["metadata"]
        assert md["confidence"] == 0.73, md
        assert md["thesis"] == thesis, md
        assert md["source"] == "poke_research", md
        assert md["direction"] == "buy", md
        assert md["side"] == "buy", md
        assert handoff["destination"] == "poke_bridge_local_queue", handoff
        print("inject valid smoke: ok (confidence preserved, thesis attached)")

        # 2) Candidate is visible to the triage queue with the same confidence.
        pending = {c.handoff_id: c for c in service.get_pending_candidates()}
        assert res["handoff_id"] in pending, "injected candidate must be pending"
        assert pending[res["handoff_id"]].confidence == 0.73
        assert pending[res["handoff_id"]].symbol == "NVDA"
        print("inject pending smoke: ok")

        # 3) Composite scoring is bypassed — monkeypatch it to raise; injection
        #    must still succeed because injected candidates never call it.
        original_composite = server.market_feed_module.composite_confidence

        def _boom(*args, **kwargs):
            raise AssertionError("composite_confidence must NOT run on injected candidates")

        server.market_feed_module.composite_confidence = _boom
        try:
            res_bypass = service.inject_researched_candidate(
                symbol="AMD",
                thesis="GPU share gains",
                confidence=0.71,
                trigger_reason="poke_web_research",
                direction="buy",
            )
        finally:
            server.market_feed_module.composite_confidence = original_composite
        assert res_bypass["queued"] is True, res_bypass
        assert res_bypass["confidence"] == 0.71, res_bypass
        print("inject composite-bypass smoke: ok")

        # 4) WATCH_ONLY tickers can be injected manually (not auto-pinged, but
        #    Poke may surface them on demand).
        res_watch = service.inject_researched_candidate(
            symbol="COIN",
            thesis="Crypto beta + record volumes",
            confidence=0.66,
            trigger_reason="poke_web_research",
            direction="buy",
        )
        assert res_watch["queued"] is True, res_watch
        assert "COIN" in set(server.market_feed_module.WATCH_ONLY)
        print("inject watch-only smoke: ok")

        # 5) Validation fails closed.
        bad_symbol = service.inject_researched_candidate(
            symbol="ZZZZ", thesis="x", confidence=0.8, trigger_reason="t", direction="buy"
        )
        assert bad_symbol["queued"] is False and "not in ticker universe" in bad_symbol["error"]

        bad_dir = service.inject_researched_candidate(
            symbol="NVDA", thesis="x", confidence=0.8, trigger_reason="t", direction="hold"
        )
        assert bad_dir["queued"] is False and "buy/sell" in bad_dir["error"]

        bad_conf = service.inject_researched_candidate(
            symbol="NVDA", thesis="x", confidence=1.5, trigger_reason="t", direction="buy"
        )
        assert bad_conf["queued"] is False and "[0, 1]" in bad_conf["error"]

        bad_conf_type = service.inject_researched_candidate(
            symbol="NVDA", thesis="x", confidence="high", trigger_reason="t", direction="buy"  # type: ignore[arg-type]
        )
        assert bad_conf_type["queued"] is False and "number" in bad_conf_type["error"]
        print("inject validation smoke: ok")

        # 6) A tripped circuit breaker blocks injection and writes nothing.
        (root / "circuit_breaker_config.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "max_consecutive_losses": 0,
                    "max_daily_loss_pct": 0.02,
                    "max_open_trades": 5,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        before = len(_all_handoffs(overnight_root))
        blocked = service.inject_researched_candidate(
            symbol="TSLA",
            thesis="should be blocked",
            confidence=0.9,
            trigger_reason="poke_web_research",
            direction="buy",
        )
        assert blocked["queued"] is False, blocked
        assert "circuit breaker" in blocked["error"].lower(), blocked
        after = len(_all_handoffs(overnight_root))
        assert before == after, "blocked injection must not write to the queue"
        print("inject circuit-breaker smoke: ok")

    # Tool must be wired into the MCP app surface.
    source = (REPO_ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8")
    assert "app.tool()(inject_researched_candidate)" in source
    assert "TODO(pydantic-schemas)" in source, "pending Pydantic merge marker required"

    print("poke inject smoke: ok")


if __name__ == "__main__":
    main()
