#!/usr/bin/env python3
"""Deterministic offline smoke for Tier A/B/C vibe-trading research routing.

Verifies:
  - _select_research_preset maps confidence bands to the right swarm preset
    (A: none, B: technical_analysis_panel, C: investment_committee).
  - _build_research_package only calls the swarm for Tier B/C and threads the
    Poke thesis through.
  - call_vibe_trading_swarm skips silently when VIBE_TRADING_URL is unset and
    constructs the correct HTTP calls when it is set (urllib is mocked — no
    real network).
  - Any HTTP error is swallowed (returns None), never blocking the pipeline.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import urllib.error
import urllib.request

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


class _FakeResp:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self._data


def _smoke_preset_routing(server) -> None:
    sel = server._select_research_preset
    # Tier A: 0.80–0.84 → no swarm.
    assert sel(0.79) is None
    assert sel(0.80) is None
    assert sel(0.84) is None
    # Tier B: 0.85–0.89 → technical_analysis_panel.
    assert sel(0.85) == "technical_analysis_panel"
    assert sel(0.89) == "technical_analysis_panel"
    # Tier C: >= 0.90 → investment_committee.
    assert sel(0.90) == "investment_committee"
    assert sel(0.97) == "investment_committee"
    # Bad input fails safe.
    assert sel("nope") is None  # type: ignore[arg-type]

    label = server._research_tier_label
    assert label(0.82) == "A"
    assert label(0.87) == "B"
    assert label(0.93) == "C"
    print("preset routing smoke: ok")


def _smoke_user_vars(server) -> None:
    ta = server._swarm_user_vars("technical_analysis_panel", "NVDA")
    assert ta == {"target": "NVDA", "timeframe": "daily"}, ta
    ic = server._swarm_user_vars("investment_committee", "NVDA")
    assert ic == {"target": "NVDA", "market": "US"}, ic
    print("swarm user_vars smoke: ok")


def _smoke_build_package_routing(server) -> None:
    """_build_research_package must call the swarm only for Tier B/C."""
    with tempfile.TemporaryDirectory(prefix="vibe-tier-smoke-") as tmp:
        service = server.PokeBridgeService(repo_root=Path(tmp))

        calls: list[tuple[str, dict]] = []

        def _fake_swarm(preset_name, user_vars, *, timeout=60.0):
            calls.append((preset_name, dict(user_vars)))
            return {"preset_name": preset_name, "status": "completed", "report": "ok"}

        original = server.call_vibe_trading_swarm
        server.call_vibe_trading_swarm = _fake_swarm
        try:
            meta = {"thesis": "poke thesis here", "trigger_reason": "news"}

            # Tier A: no swarm call.
            pkg_a = service._build_research_package(
                symbol="NVDA", confidence=0.82, candidate_metadata=meta
            )
            assert pkg_a["research_tier"] == "A"
            assert pkg_a["vibe_trading_preset"] is None
            assert pkg_a["vibe_trading"] is None
            assert pkg_a["poke_thesis"] == "poke thesis here"
            assert calls == [], "Tier A must not call the swarm"

            # Tier B: technical_analysis_panel.
            pkg_b = service._build_research_package(
                symbol="NVDA", confidence=0.87, candidate_metadata=meta
            )
            assert pkg_b["research_tier"] == "B"
            assert pkg_b["vibe_trading_preset"] == "technical_analysis_panel"
            assert pkg_b["vibe_trading"]["status"] == "completed"
            assert calls[-1][0] == "technical_analysis_panel"
            assert calls[-1][1]["target"] == "NVDA"

            # Tier C: investment_committee.
            pkg_c = service._build_research_package(
                symbol="NVDA", confidence=0.93, candidate_metadata=meta
            )
            assert pkg_c["research_tier"] == "C"
            assert pkg_c["vibe_trading_preset"] == "investment_committee"
            assert calls[-1][0] == "investment_committee"

            assert len(calls) == 2, f"only Tier B + C call the swarm, got {calls}"
        finally:
            server.call_vibe_trading_swarm = original
    print("build-package routing smoke: ok")


def _smoke_swarm_url_unset(server) -> None:
    prev = os.environ.pop("VIBE_TRADING_URL", None)
    try:
        result = server.call_vibe_trading_swarm(
            "technical_analysis_panel", {"target": "NVDA", "timeframe": "daily"}
        )
        assert result is None, "no VIBE_TRADING_URL must skip silently"
    finally:
        if prev is not None:
            os.environ["VIBE_TRADING_URL"] = prev
    print("swarm url-unset smoke: ok")


def _smoke_swarm_http_success(server) -> None:
    requests_made: list[tuple[str, str, bytes | None]] = []

    def fake_urlopen(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        requests_made.append((method, url, req.data))
        if method == "POST" and url.endswith("/swarm/runs"):
            body = json.loads(req.data.decode("utf-8"))
            assert body["preset_name"] == "investment_committee", body
            assert body["user_vars"]["target"] == "NVDA", body
            return _FakeResp({"id": "run-xyz", "status": "running", "preset_name": body["preset_name"]})
        if method == "GET" and url.endswith("/swarm/runs/run-xyz"):
            return _FakeResp(
                {"id": "run-xyz", "status": "completed", "final_report": "CONSENSUS: bullish"}
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    prev_url = os.environ.get("VIBE_TRADING_URL")
    prev_urlopen = urllib.request.urlopen
    os.environ["VIBE_TRADING_URL"] = "http://vibe.test/"  # trailing slash trimmed
    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        result = server.call_vibe_trading_swarm(
            "investment_committee", {"target": "NVDA", "market": "US"}, timeout=5.0
        )
    finally:
        urllib.request.urlopen = prev_urlopen  # type: ignore[assignment]
        if prev_url is None:
            os.environ.pop("VIBE_TRADING_URL", None)
        else:
            os.environ["VIBE_TRADING_URL"] = prev_url

    assert result is not None, "successful swarm call must return a dict"
    assert result["status"] == "completed", result
    assert result["report"] == "CONSENSUS: bullish", result
    assert result["run_id"] == "run-xyz", result
    # POST to create, then at least one GET to poll detail.
    methods = [m for m, _, _ in requests_made]
    assert methods[0] == "POST"
    assert "GET" in methods[1:]
    print("swarm http-success smoke: ok")


def _smoke_swarm_http_error(server) -> None:
    def boom_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    prev_url = os.environ.get("VIBE_TRADING_URL")
    prev_urlopen = urllib.request.urlopen
    os.environ["VIBE_TRADING_URL"] = "http://vibe.test"
    urllib.request.urlopen = boom_urlopen  # type: ignore[assignment]
    try:
        result = server.call_vibe_trading_swarm(
            "technical_analysis_panel", {"target": "NVDA", "timeframe": "daily"}, timeout=5.0
        )
    finally:
        urllib.request.urlopen = prev_urlopen  # type: ignore[assignment]
        if prev_url is None:
            os.environ.pop("VIBE_TRADING_URL", None)
        else:
            os.environ["VIBE_TRADING_URL"] = prev_url
    assert result is None, "HTTP error must be swallowed (skip silently)"
    print("swarm http-error smoke: ok")


def main() -> None:
    _stub_mcp()
    server = _load_module("davey_vibe_tier_smoke_server", REPO_ROOT / "mcp_server" / "server.py")

    _smoke_preset_routing(server)
    _smoke_user_vars(server)
    _smoke_build_package_routing(server)
    _smoke_swarm_url_unset(server)
    _smoke_swarm_http_success(server)
    _smoke_swarm_http_error(server)

    print("vibe trading tier smoke: ok")


if __name__ == "__main__":
    main()
