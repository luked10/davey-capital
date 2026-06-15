#!/usr/bin/env python3
"""Smoke test for expanded ticker universe and Poke research agent tools.

All tests are deterministic and offline (yfinance calls are replaced by a
lightweight mock injected into sys.modules before each function call).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Minimal yfinance mock — injected into sys.modules for offline tests
# ---------------------------------------------------------------------------

class _MockDF:
    """Minimal DataFrame-like for yfinance history() mocks."""

    def __init__(self, data: dict):
        self._data = data
        self.empty = not any(v for v in data.values())

    def __getitem__(self, col: str):
        return _MockSeries(self._data.get(col, []))


class _MockSeries:
    def __init__(self, vals):
        self._vals = list(vals)

    def dropna(self):
        return self

    def tolist(self):
        return self._vals


class _MockFastInfo:
    def __init__(self, d: dict):
        self._d = d

    def __getitem__(self, k: str):
        if k not in self._d:
            raise KeyError(k)
        return self._d[k]

    def get(self, k: str, default=None):
        return self._d.get(k, default)


_NVDA_NEWS = [
    {"title": "NVIDIA surge hits record high on AI growth"},
    {"title": "NVIDIA beats earnings with strong data center gain"},
    {"title": "NVIDIA rally continues on positive outlook"},
    {"title": "Analysts upgrade NVIDIA on bullish momentum"},
    {"title": "NVIDIA soars on record AI chip demand"},
]
_NVDA_CLOSES_HOURLY = [100.0, 102.0, 100.0, 103.5]   # price_move ~3.5%
_NVDA_VOLS_DAILY = [1_000_000] * 20 + [1_800_000]     # volume_ratio = 1.8


def _make_mock_yf():
    mock = types.ModuleType("yfinance")

    class MockTicker:
        def __init__(self, sym: str):
            self._sym = sym.upper()

        @property
        def news(self):
            return _NVDA_NEWS if self._sym == "NVDA" else []

        @property
        def fast_info(self):
            if self._sym == "NVDA":
                return _MockFastInfo({
                    "last_price": 850.0,
                    "previous_close": 820.0,
                    "last_volume": 1_800_000,
                    "three_month_average_volume": 1_000_000,
                })
            return _MockFastInfo({
                "last_price": 50.0,
                "previous_close": 49.0,
                "last_volume": 500_000,
                "three_month_average_volume": 500_000,
            })

        def history(self, period="1d", interval="1d", auto_adjust=True):
            if self._sym == "NVDA":
                if interval == "60m":
                    return _MockDF({"Close": _NVDA_CLOSES_HOURLY, "Volume": []})
                return _MockDF({"Close": [850.0] * 22, "Volume": _NVDA_VOLS_DAILY})
            return _MockDF({"Close": [], "Volume": []})

    mock.Ticker = MockTicker
    return mock


def _inject_yf(mock):
    old = sys.modules.get("yfinance")
    sys.modules["yfinance"] = mock
    return old


def _restore_yf(old):
    if old is not None:
        sys.modules["yfinance"] = old
    else:
        sys.modules.pop("yfinance", None)


# ---------------------------------------------------------------------------
# Test: WATCH_SYMBOLS expansion
# ---------------------------------------------------------------------------

def _smoke_watch_symbols():
    market_feed = _load_module(
        "smoke_market_feed",
        REPO_ROOT / "autohedge" / "autohedge" / "data" / "market_feed.py",
    )
    expected = {
        "NVDA", "MU", "AMD", "TSLA", "META", "GOOGL",
        "AMZN", "AAPL", "MSFT", "PLTR", "ARM", "SMCI",
    }
    actual = set(market_feed.WATCH_SYMBOLS)
    missing = expected - actual
    assert not missing, f"WATCH_SYMBOLS missing tickers: {missing}"
    assert len(market_feed.WATCH_SYMBOLS) == 12, (
        f"Expected 12 symbols, got {len(market_feed.WATCH_SYMBOLS)}: {market_feed.WATCH_SYMBOLS}"
    )
    # Crypto must be absent
    assert "BTC/USD" not in actual and "SOL/USD" not in actual, (
        "Crypto symbols must remain commented out"
    )
    print("watch symbols smoke: ok")


# ---------------------------------------------------------------------------
# Test: ticker extraction helper
# ---------------------------------------------------------------------------

def _smoke_ticker_extraction(server_module):
    extract = server_module._extract_ticker_from_query
    known = frozenset({"NVDA", "MU", "AMD", "TSLA"})

    assert extract("research NVDA catalysts today", known) == "NVDA"
    assert extract("research nvda", known) == "NVDA"           # case-insensitive known lookup
    assert extract("what does TSLA do next week", known) == "TSLA"
    assert extract("AAPL earnings report", known) == "AAPL"    # uppercase fallback
    assert extract("what is the market doing today", known) is None
    assert extract("", known) is None
    print("ticker extraction smoke: ok")


# ---------------------------------------------------------------------------
# Test: headline sentiment scoring
# ---------------------------------------------------------------------------

def _smoke_headline_sentiment(server_module):
    score = server_module._headline_sentiment

    bull, bear = score("NVIDIA surges to record high on strong growth")
    assert bull > 0 and bear == 0, f"Expected bullish, got bull={bull} bear={bear}"

    bull, bear = score("AMD drops on weak earnings miss and decline")
    assert bear > 0 and bull == 0, f"Expected bearish, got bull={bull} bear={bear}"

    bull, bear = score("Market closed for holiday")
    assert bull == 0 and bear == 0, "Neutral headline should score 0/0"
    print("headline sentiment smoke: ok")


# ---------------------------------------------------------------------------
# Test: research_and_surface_candidates — no ticker in query
# ---------------------------------------------------------------------------

def _smoke_no_ticker_query(service):
    result = service.research_and_surface_candidates("what is the market doing today")
    assert result.get("queued") is False
    assert "error" in result
    assert "No ticker" in result["error"]
    print("no-ticker query smoke: ok")


# ---------------------------------------------------------------------------
# Test: research_and_surface_candidates — full pipeline with mock yfinance
# ---------------------------------------------------------------------------

def _smoke_research_pipeline(service, mock_yf):
    old = _inject_yf(mock_yf)
    try:
        result = service.research_and_surface_candidates("research NVDA catalysts today")
        assert result["symbol"] == "NVDA", f"symbol={result['symbol']}"
        assert result["side"] == "buy", f"Expected buy, got {result['side']}"
        # With 5 bullish headlines and a 3.5% price move, confidence > 0.65
        assert result["confidence"] >= 0.65, f"confidence={result['confidence']}"
        assert result["queued"] is True, f"Expected queued, got {result}"
        assert result["handoff_id"], "Expected a handoff_id"
        assert result["bullish_count"] > 0
        assert result["bearish_count"] == 0
        assert result["price_move_pct"] is not None and result["price_move_pct"] > 0
        assert result["volume_ratio"] is not None and result["volume_ratio"] > 1.0
        print(
            f"research pipeline smoke: NVDA confidence={result['confidence']} "
            f"handoff={result['handoff_id']}: ok"
        )
    finally:
        _restore_yf(old)


# ---------------------------------------------------------------------------
# Test: research — low-confidence symbol produces no queue entry
# ---------------------------------------------------------------------------

def _smoke_research_low_confidence(service, mock_yf):
    """MU has no news or price history in the mock — confidence stays at 0.55."""
    old = _inject_yf(mock_yf)
    try:
        result = service.research_and_surface_candidates("research MU")
        assert result["symbol"] == "MU"
        assert result["queued"] is False
        assert result["confidence"] < 0.65, f"Expected low confidence, got {result['confidence']}"
        print(f"low-confidence smoke: MU confidence={result['confidence']}: ok")
    finally:
        _restore_yf(old)


# ---------------------------------------------------------------------------
# Test: get_market_overview — full snapshot with mock yfinance
# ---------------------------------------------------------------------------

def _smoke_market_overview(service, mock_yf):
    old = _inject_yf(mock_yf)
    try:
        result = service.get_market_overview()
        assert "overview" in result
        assert "snapshot_at" in result
        assert "symbols" in result

        expected = {
            "NVDA", "MU", "AMD", "TSLA", "META", "GOOGL",
            "AMZN", "AAPL", "MSFT", "PLTR", "ARM", "SMCI",
        }
        assert expected.issubset(set(result["symbols"])), (
            f"Missing symbols: {expected - set(result['symbols'])}"
        )

        nvda = result["overview"].get("NVDA", {})
        assert nvda.get("status") == "ok", f"NVDA status: {nvda}"
        assert nvda.get("price") == 850.0, f"NVDA price: {nvda.get('price')}"
        assert nvda.get("pct_change_today") is not None
        assert nvda.get("side") == "buy"   # price rose from 820 → 850

        # All 12 symbols should be present in the overview
        assert len(result["overview"]) == 12, (
            f"Expected 12 entries, got {len(result['overview'])}"
        )
        print(f"market overview smoke: {len(result['overview'])} symbols: ok")
    finally:
        _restore_yf(old)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. Market feed standalone check (no server.py needed)
    _smoke_watch_symbols()

    # 2. Load server module for the service-level tests.
    #    Use a temp dir for all file I/O so nothing lands in the real repo.
    with tempfile.TemporaryDirectory(prefix="research-agent-smoke-") as tmp:
        tmp_path = Path(tmp)

        # Pre-inject a bare mcp stub so server.py's build_mcp_app doesn't
        # fail when mcp is unavailable in CI.
        if "mcp" not in sys.modules:
            mcp_stub = types.ModuleType("mcp")
            mcp_stub.server = types.ModuleType("mcp.server")
            mcp_stub.server.fastmcp = types.ModuleType("mcp.server.fastmcp")
            sys.modules["mcp"] = mcp_stub
            sys.modules["mcp.server"] = mcp_stub.server
            sys.modules["mcp.server.fastmcp"] = mcp_stub.server.fastmcp

        server_module = _load_module(
            "davey_research_agent_smoke_server",
            REPO_ROOT / "mcp_server" / "server.py",
        )

        service = server_module.PokeBridgeService(repo_root=tmp_path)
        mock_yf = _make_mock_yf()

        _smoke_ticker_extraction(server_module)
        _smoke_headline_sentiment(server_module)
        _smoke_no_ticker_query(service)
        _smoke_research_pipeline(service, mock_yf)
        _smoke_research_low_confidence(service, mock_yf)
        _smoke_market_overview(service, mock_yf)

    print("research agent smoke: ok")


if __name__ == "__main__":
    main()
