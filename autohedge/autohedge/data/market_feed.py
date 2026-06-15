"""Conservative yfinance market feed for dry-run watcher candidates."""

from __future__ import annotations

from typing import Any

def _load_candlestick_patterns():
    """Lazily load candlestick_patterns from vibe-trading/agent/src/tools/pattern_tool.py.

    Returns None if the file is absent or fails to load — callers must handle None.
    """
    import importlib.util
    import os

    path = os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "../../../vibe-trading/agent/src/tools/pattern_tool.py",
        )
    )
    try:
        spec = importlib.util.spec_from_file_location("pattern_tool", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "candlestick_patterns", None)
    except Exception as exc:
        print(f"PatternTool unavailable: {exc}", flush=True)
        return None


WATCH_SYMBOLS = (
    "NVDA", "MU", "AMD", "TSLA", "META", "GOOGL",
    "AMZN", "AAPL", "MSFT", "PLTR", "ARM", "SMCI",
    # Crypto disabled pending Alpaca account enablement
    # "BTC/USD", "SOL/USD",
)
PRICE_MOVE_THRESHOLD = 0.02
VOLUME_MULTIPLIER_THRESHOLD = 1.5


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
    if abs(price_move) > PRICE_MOVE_THRESHOLD:
        trigger_reasons.append("price_move_1h")

    # Technical pattern detection (optional — skipped if PatternTool unavailable)
    try:
        candlestick_fn = _load_candlestick_patterns()
        if candlestick_fn is not None and len(closes) >= 5:
            df = hourly.tail(5).copy()
            patterns = candlestick_fn(df["Open"], df["High"], df["Low"], df["Close"])
            latest_pattern = patterns.iloc[-1]
            if latest_pattern == 1 and side == "buy":
                trigger_reasons.append("bullish_candlestick")
            elif latest_pattern == -1 and side == "sell":
                trigger_reasons.append("bearish_candlestick")
    except Exception:
        pass

    latest_volume = None
    average_volume = None
    volume_ratio = None
    try:
        daily = ticker.history(period="30d", interval="1d", auto_adjust=False)
        daily_volumes = _series_values(daily, "Volume")
        if daily_volumes:
            latest_volume = daily_volumes[-1]
            average_volume = _average_positive(daily_volumes[-21:-1] or daily_volumes[:-1])
            if average_volume and average_volume > 0 and latest_volume is not None:
                volume_ratio = latest_volume / average_volume
                if volume_ratio > VOLUME_MULTIPLIER_THRESHOLD:
                    trigger_reasons.append("volume_spike_20d")
    except Exception:
        latest_volume = None
        average_volume = None
        volume_ratio = None

    if not trigger_reasons:
        return None

    confidence = 0.55 + min(abs(price_move) * 4.0, 0.25)

    # Adjust confidence for technical patterns
    if "bullish_candlestick" in trigger_reasons or "bearish_candlestick" in trigger_reasons:
        confidence += 0.10

    if volume_ratio is not None and volume_ratio > VOLUME_MULTIPLIER_THRESHOLD:
        confidence += min((volume_ratio - VOLUME_MULTIPLIER_THRESHOLD) * 0.08, 0.15)

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
