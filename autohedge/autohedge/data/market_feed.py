"""Conservative yfinance market feed for dry-run watcher candidates."""

from __future__ import annotations

from typing import Any

def _load_candlestick_patterns():
    """Lazily load candlestick_patterns from vibe-trading/agent/src/tools/pattern_tool.py.

    Tries multiple candidate agent dirs in order (local cwd first, then /app on Fly).
    The agent dir must be on sys.path so pattern_tool.py's own 'from src.X import Y'
    imports resolve correctly.
    Returns None if unavailable — callers must handle None gracefully.
    """
    import importlib.util
    import os
    import sys

    # Candidate vibe-trading/agent directories; first one whose pattern_tool.py exists wins.
    agent_dirs = [
        os.path.join(os.getcwd(), "vibe-trading/agent"),
        os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "../../../vibe-trading/agent",
        )),
        "/app/vibe-trading/agent",
    ]
    agent_dir = next(
        (d for d in agent_dirs if os.path.isfile(os.path.join(d, "src/tools/pattern_tool.py"))),
        None,
    )
    if agent_dir is None:
        print(
            "PatternTool unavailable: pattern_tool.py not found in any candidate path",
            flush=True,
        )
        return None

    # Add the agent dir to sys.path so pattern_tool.py's internal
    # 'from src.tools.X import Y' imports resolve correctly.
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)

    path = os.path.join(agent_dir, "src/tools/pattern_tool.py")
    try:
        spec = importlib.util.spec_from_file_location("pattern_tool", path)
        if spec is None or spec.loader is None:
            print(f"PatternTool unavailable: no loader for {path}", flush=True)
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "candlestick_patterns", None)
        print(f"PatternTool loaded from {path}", flush=True)
        return fn
    except Exception as exc:
        print(f"PatternTool unavailable ({path}): {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Ticker universe (priority tiers)
#
# Signal generation here is deliberately model-free: yfinance price/volume +
# the deterministic PatternTool only. There is NO AutoHedge LLM agent
# (Director/Quant/Risk) wiring in this signal flow — Poke does free web
# research at Tier 1 instead. This module must never import or call those
# agents; broker/audit/circuit-breaker imports live elsewhere.
# ---------------------------------------------------------------------------

# Auto-pinged on a >2% move OR >1.5x 20-day average volume.
HIGH_PRIORITY = ("NVDA", "MU", "AMD", "TSLA")

# Auto-pinged on a >3% move OR >2x 20-day average volume.
MEDIUM_PRIORITY = ("META", "GOOGL", "AMZN", "AAPL", "MSFT", "PLTR", "ARM", "SMCI")

# yfinance + PatternTool only — never auto-pinged (cost saving). Poke can still
# call inject_researched_candidate manually for any of these.
WATCH_ONLY = (
    "NFLX", "AVGO", "QCOM", "ASML", "MCHP", "MRVL", "CRM", "ADBE",
    "SNOW", "CRWD", "OKTA", "DDOG", "CRDO", "RBLX", "SOUN", "IONQ",
    "MSTR", "COIN", "HOOD", "SOFI", "UPST", "AFRM", "NU", "SHOP",
    "SPOT", "UBER", "LYFT", "NET", "BILL", "PATH",
)

# Actively auto-pinged universe (HIGH + MEDIUM). Kept named WATCH_SYMBOLS for
# backwards compatibility with the market overview / research tools.
WATCH_SYMBOLS = HIGH_PRIORITY + MEDIUM_PRIORITY

# Full universe valid for Poke manual injection (HIGH + MEDIUM + WATCH_ONLY).
ALL_SYMBOLS = HIGH_PRIORITY + MEDIUM_PRIORITY + WATCH_ONLY

# Per-tier auto-ping thresholds. WATCH_ONLY is intentionally absent: it is
# scanned for data only and never produces an auto-ping candidate.
TIER_THRESHOLDS: dict[str, dict[str, float]] = {
    "high": {"price_move": 0.02, "volume": 1.5},
    "medium": {"price_move": 0.03, "volume": 2.0},
}

# Composite confidence weights (scheduler-generated candidates only).
COMPOSITE_BASE = 0.40
COMPOSITE_PRICE_MOVE_WEIGHT = 0.15
COMPOSITE_VOLUME_WEIGHT = 0.10
COMPOSITE_PATTERN_WEIGHT = 0.10
COMPOSITE_POKE_RESEARCH_WEIGHT = 0.25
COMPOSITE_SURFACE_THRESHOLD = 0.80

# Legacy aliases retained so any external reference keeps resolving.
PRICE_MOVE_THRESHOLD = TIER_THRESHOLDS["high"]["price_move"]
VOLUME_MULTIPLIER_THRESHOLD = TIER_THRESHOLDS["high"]["volume"]


def priority_tier(symbol: str) -> str | None:
    """Return the priority tier for a symbol: high/medium/watch_only/None."""
    sym = str(symbol or "").strip().upper()
    if sym in HIGH_PRIORITY:
        return "high"
    if sym in MEDIUM_PRIORITY:
        return "medium"
    if sym in WATCH_ONLY:
        return "watch_only"
    return None


def composite_confidence(
    *,
    price_move_ok: bool,
    volume_ok: bool,
    pattern_ok: bool,
    source: str = "",
) -> float:
    """Composite confidence score for SCHEDULER-GENERATED candidates only.

    Base 0.40, plus weighted boosts for the price-move, volume, and PatternTool
    confirmations. Capped at 1.0; only candidates >= COMPOSITE_SURFACE_THRESHOLD
    (0.80) reach Sonnet downstream.

    Poke-injected candidates (source == "poke_research") BYPASS this function
    entirely — their confidence is set by Poke after free web research and must
    never be recomputed here. The +0.25 poke_research branch below is therefore
    never reached via the scheduler path; it is left as explicit documentation
    that injected candidates skip composite scoring.
    """
    score = COMPOSITE_BASE
    if price_move_ok:
        score += COMPOSITE_PRICE_MOVE_WEIGHT
    if volume_ok:
        score += COMPOSITE_VOLUME_WEIGHT
    if pattern_ok:
        score += COMPOSITE_PATTERN_WEIGHT
    if source == "poke_research":
        # Unreachable via the scheduler: injected candidates never call this.
        score += COMPOSITE_POKE_RESEARCH_WEIGHT
    return round(min(score, 1.0), 4)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _series_values(frame: Any, column: str) -> list[float]:
    try:
        if frame is None or getattr(frame, "empty", True):
            return []
        values = frame[column].dropna().tolist()
    except Exception:
        return []
    clean_values = []
    for value in values:
        number = _safe_float(value)
        if number is not None:
            clean_values.append(number)
    return clean_values


def _average_positive(values: list[float]) -> float | None:
    positives = [value for value in values if value > 0]
    if not positives:
        return None
    return sum(positives) / len(positives)


def _build_candidate(
    *,
    symbol: str,
    side: str,
    confidence: float,
    latest_close: float | None,
    previous_close: float | None,
    latest_volume: float | None,
    average_volume: float | None,
    price_move: float | None,
    volume_ratio: float | None,
    trigger_reasons: list[str],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side,
        "confidence": round(max(0.0, min(confidence, 0.95)), 4),
        "strategy": "market_feed_momentum",
        "source": "yfinance_market_feed",
        "dry_run": True,
        "metadata": {
            "latest_close": latest_close,
            "previous_close": previous_close,
            "latest_volume": latest_volume,
            "average_20d_volume": average_volume,
            "price_move_1h": price_move,
            "volume_ratio_20d": volume_ratio,
            "trigger_reasons": trigger_reasons,
        },
    }


def _candidate_for_symbol(yf: Any, symbol: str) -> dict[str, Any] | None:
    # Tier gates the auto-ping thresholds. WATCH_ONLY (and anything off-universe)
    # never produces an auto-ping candidate — those are manual-injection only.
    tier = priority_tier(symbol)
    thresholds = TIER_THRESHOLDS.get(tier or "")
    if thresholds is None:
        return None
    price_move_threshold = thresholds["price_move"]
    volume_threshold = thresholds["volume"]

    ticker = yf.Ticker(symbol)
    hourly = ticker.history(period="2d", interval="60m", auto_adjust=False)
    closes = _series_values(hourly, "Close")
    if len(closes) < 2:
        return None

    latest_close = closes[-1]
    previous_close = closes[-2]
    if previous_close <= 0:
        return None

    price_move = (latest_close - previous_close) / previous_close
    side = "buy" if price_move >= 0 else "sell"
    trigger_reasons: list[str] = []
    price_move_ok = abs(price_move) > price_move_threshold
    if price_move_ok:
        trigger_reasons.append("price_move_1h")

    # Technical pattern detection (optional — skipped if PatternTool unavailable)
    pattern_ok = False
    try:
        candlestick_fn = _load_candlestick_patterns()
        if candlestick_fn is not None and len(closes) >= 5:
            df = hourly.tail(5).copy()
            patterns = candlestick_fn(df["Open"], df["High"], df["Low"], df["Close"])
            latest_pattern = patterns.iloc[-1]
            if latest_pattern == 1 and side == "buy":
                trigger_reasons.append("bullish_candlestick")
                pattern_ok = True
            elif latest_pattern == -1 and side == "sell":
                trigger_reasons.append("bearish_candlestick")
                pattern_ok = True
    except Exception:
        pass

    latest_volume = None
    average_volume = None
    volume_ratio = None
    volume_ok = False
    try:
        daily = ticker.history(period="30d", interval="1d", auto_adjust=False)
        daily_volumes = _series_values(daily, "Volume")
        if daily_volumes:
            latest_volume = daily_volumes[-1]
            average_volume = _average_positive(daily_volumes[-21:-1] or daily_volumes[:-1])
            if average_volume and average_volume > 0 and latest_volume is not None:
                volume_ratio = latest_volume / average_volume
                if volume_ratio > volume_threshold:
                    volume_ok = True
                    trigger_reasons.append("volume_spike_20d")
    except Exception:
        latest_volume = None
        average_volume = None
        volume_ratio = None

    if not trigger_reasons:
        return None

    # Composite confidence (scheduler path). Poke-injected candidates skip this
    # entirely and carry Poke's own confidence — see composite_confidence().
    confidence = composite_confidence(
        price_move_ok=price_move_ok,
        volume_ok=volume_ok,
        pattern_ok=pattern_ok,
        source="yfinance_market_feed",
    )

    return _build_candidate(
        symbol=symbol,
        side=side,
        confidence=confidence,
        latest_close=latest_close,
        previous_close=previous_close,
        latest_volume=latest_volume,
        average_volume=average_volume,
        price_move=price_move,
        volume_ratio=volume_ratio,
        trigger_reasons=trigger_reasons,
    )


def fetch_candidates() -> list[dict[str, Any]]:
    """Fetch dry-run watcher candidates from yfinance.

    Scans only the auto-ping universe (HIGH + MEDIUM). WATCH_ONLY tickers are
    never auto-scanned here (cost saving); they reach the pipeline only through
    Poke's manual inject_researched_candidate.

    This function is intentionally fail-closed: any yfinance/import/data error
    returns an empty candidate list and never reaches broker APIs.
    """
    try:
        import yfinance as yf

        candidates: list[dict[str, Any]] = []
        for symbol in WATCH_SYMBOLS:
            try:
                candidate = _candidate_for_symbol(yf, symbol)
            except Exception:
                candidate = None
            if candidate is not None:
                candidates.append(candidate)
        return candidates
    except Exception:
        return []
