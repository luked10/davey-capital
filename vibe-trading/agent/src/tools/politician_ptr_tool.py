"""PTR ingest, LLM extraction, clustered-trade scoring, and alert packaging.

This tool is intentionally configurable:
- it scrapes PTR disclosure pages from caller-provided source URLs
- it uses the repo's LLM abstraction to normalize filings into structured JSON
- it scores clustered trades by ticker / recency / conviction
- it emits an alert packet that can be written into the existing session trail
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional

import requests
from langchain_core.messages import HumanMessage, SystemMessage

from src.agent.tools import BaseTool
from src.providers.llm import build_llm

DEFAULT_WINDOW_DAYS = 30
DEFAULT_ALERT_THRESHOLD = 65.0
DEFAULT_SOURCE_ENV = "PTR_DISCLOSURE_URLS"
DEFAULT_TIMEOUT_SECONDS = 30


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VisibleTextExtractor(HTMLParser):
    """Extract visible text and links from HTML without extra dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if lowered == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self._current_href = value
                    break

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if lowered == "a" and self._current_href:
            self._current_href = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = normalize_whitespace(data)
        if not text:
            return
        self._chunks.append(text)

    def text(self) -> str:
        return normalize_whitespace(" ".join(self._chunks))


@dataclass
class PTRDocument:
    source_url: str
    fetched_at: str
    title: str | None
    text: str


@dataclass
class PTRFiling:
    source_url: str
    filing_date: str | None
    politician_name: str | None
    chamber: str | None
    office: str | None
    party: str | None
    state: str | None
    ticker: str | None
    asset_name: str | None
    transaction_type: str | None
    direction: str | None
    amount_text: str | None
    amount_low: float | None
    amount_high: float | None
    confidence: float | None
    notes: str | None


@dataclass
class PTRClusterAlert:
    ticker: str
    score: float
    filing_count: int
    total_estimated_amount: float
    buy_count: int
    sell_count: int
    unique_people: list[str]
    filing_dates: list[str]
    reasons: list[str]
    filings: list[dict[str, Any]]


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_html(html: str) -> tuple[str, list[dict[str, str]]]:
    parser = VisibleTextExtractor()
    parser.feed(html)
    return parser.text(), parser.links


def _parse_amount_range(amount_text: str | None) -> tuple[float | None, float | None]:
    if not amount_text:
        return None, None

    raw = amount_text.strip().replace(",", "")
    if not raw:
        return None, None

    # Common range formats like "$15,001-$50,000" or "$50k - $100k".
    match = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)(?:\s*[kK])?\s*[-–]\s*\$?([0-9]+(?:\.[0-9]+)?)(?:\s*[kK])?", raw)
    if match:
        left = float(match.group(1))
        right = float(match.group(2))
        if "k" in raw.lower():
            left *= 1000.0
            right *= 1000.0
        return left, right

    single = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)(?:\s*[kK])?", raw)
    if single:
        value = float(single.group(1))
        if "k" in raw.lower():
            value *= 1000.0
        return value, value

    return None, None


def _median_amount_low(filings: list[PTRFiling]) -> float:
    values = [f.amount_low for f in filings if f.amount_low is not None]
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return float((values[mid - 1] + values[mid]) / 2.0)


def _coerce_date(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned[:10], fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        ch = text[index]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def _estimate_confidence(record: dict[str, Any]) -> float:
    value = record.get("confidence")
    try:
        if value is not None:
            return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        pass

    score = 0.45
    if record.get("ticker"):
        score += 0.15
    if record.get("amount_text"):
        score += 0.15
    if record.get("filing_date"):
        score += 0.1
    if record.get("politician_name"):
        score += 0.15
    return max(0.0, min(1.0, score))


def _build_llm_payload(documents: list[PTRDocument]) -> str:
    sections: list[str] = []
    for index, doc in enumerate(documents, start=1):
        sections.append(
            f"SOURCE {index}\nURL: {doc.source_url}\nFETCHED_AT: {doc.fetched_at}\nTITLE: {doc.title or 'unknown'}\nTEXT:\n{doc.text}"
        )
    return "\n\n---\n\n".join(sections)


def extract_ptr_filings_with_llm(documents: list[PTRDocument], *, model_name: str | None = None) -> list[PTRFiling]:
    """Use the repo's LLM abstraction to convert scraped text into structured PTR filings."""
    if not documents:
        return []

    try:
        llm = build_llm(model_name=model_name)
        prompt = SystemMessage(
            content=(
                "You extract House and Senate PTR disclosures into strict JSON. "
                "Return only a JSON object with a top-level 'filings' array. "
                "Each filing should contain: source_url, filing_date, politician_name, chamber, office, party, state, "
                "ticker, asset_name, transaction_type, direction, amount_text, amount_low, amount_high, confidence, notes. "
                "Normalize tickers to uppercase. Use null when unknown. "
                "Treat transactions as buys or sells when the disclosure supports that. "
                "If a filing lists multiple tickers, emit one record per ticker."
            )
        )
        payload = HumanMessage(content=_build_llm_payload(documents))
        response = llm.invoke([prompt, payload])
        raw_text = getattr(response, "content", str(response))
        parsed = _extract_json_object(raw_text)
        if not parsed:
            return _fallback_extract_ptr_filings(documents)
    except Exception:
        return _fallback_extract_ptr_filings(documents)

    filings: list[PTRFiling] = []
    for record in parsed.get("filings", []):
        if not isinstance(record, dict):
            continue
        amount_low = record.get("amount_low")
        amount_high = record.get("amount_high")
        try:
            amount_low_f = float(amount_low) if amount_low is not None else None
        except (TypeError, ValueError):
            amount_low_f = None
        try:
            amount_high_f = float(amount_high) if amount_high is not None else None
        except (TypeError, ValueError):
            amount_high_f = None
        filings.append(
            PTRFiling(
                source_url=str(record.get("source_url") or ""),
                filing_date=record.get("filing_date"),
                politician_name=record.get("politician_name"),
                chamber=record.get("chamber"),
                office=record.get("office"),
                party=record.get("party"),
                state=record.get("state"),
                ticker=(str(record.get("ticker") or "").upper() or None),
                asset_name=record.get("asset_name"),
                transaction_type=record.get("transaction_type"),
                direction=(str(record.get("direction") or "").lower() or None),
                amount_text=record.get("amount_text"),
                amount_low=amount_low_f,
                amount_high=amount_high_f,
                confidence=_estimate_confidence(record),
                notes=record.get("notes"),
            )
        )
    return filings


def fetch_ptr_documents(
    source_urls: list[str],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_chars_per_source: int = 12000,
) -> list[PTRDocument]:
    """Fetch and lightly normalize source pages.

    This is a scraper, not a JavaScript-heavy browser automation layer.
    Callers can point it at any House/Senate PTR disclosure pages or mirrors.
    """
    session = requests.Session()
    documents: list[PTRDocument] = []
    for url in source_urls:
        response = session.get(url, timeout=timeout_seconds, headers={"User-Agent": "quant-hub-bridge/ptr-monitor"})
        response.raise_for_status()
        text, links = strip_html(response.text)
        title = None
        title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.I | re.S)
        if title_match:
            title = normalize_whitespace(title_match.group(1))
        merged = text
        if links:
            link_text = " | ".join(sorted({f"{item.get('text', '')} -> {item.get('href', '')}".strip() for item in links if item.get('href')}))
            if link_text:
                merged = f"{merged}\n\nLINKS:\n{link_text}"
        documents.append(
            PTRDocument(
                source_url=url,
                fetched_at=_now_utc_iso(),
                title=title,
                text=merged[:max_chars_per_source],
            )
        )
    return documents


def _cluster_key(filing: PTRFiling) -> str:
    if filing.ticker:
        return filing.ticker.upper()
    if filing.asset_name:
        return normalize_whitespace(filing.asset_name).upper()
    return "UNKNOWN"


def score_ptr_clusters(
    filings: list[PTRFiling],
    *,
    cluster_window_days: int = DEFAULT_WINDOW_DAYS,
    alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
) -> list[PTRClusterAlert]:
    """Score clustered PTR filings and flag the highest-conviction names."""
    if not filings:
        return []

    grouped: dict[str, list[PTRFiling]] = defaultdict(list)
    for filing in filings:
        grouped[_cluster_key(filing)].append(filing)

    alerts: list[PTRClusterAlert] = []
    now = datetime.now(timezone.utc)

    for ticker, rows in grouped.items():
        if ticker == "UNKNOWN":
            continue

        dated_rows = [r for r in rows if _coerce_date(r.filing_date)]
        if len(dated_rows) < 2:
            continue

        ordered = sorted(dated_rows, key=lambda item: _coerce_date(item.filing_date) or now, reverse=True)
        newest = _coerce_date(ordered[0].filing_date) or now
        cluster_rows = [row for row in ordered if (newest - (_coerce_date(row.filing_date) or newest)).days <= cluster_window_days]
        if len(cluster_rows) < 2:
            continue

        buy_count = sum(1 for row in cluster_rows if (row.direction or "").lower() in {"buy", "purchase", "bought", "long"})
        sell_count = sum(1 for row in cluster_rows if (row.direction or "").lower() in {"sell", "sale", "sold", "short"})
        unique_people = sorted({normalize_whitespace(row.politician_name or row.office or "") for row in cluster_rows if (row.politician_name or row.office)})
        filing_dates = sorted({row.filing_date or "" for row in cluster_rows if row.filing_date})
        total_estimated_amount = 0.0
        for row in cluster_rows:
            if row.amount_low is not None and row.amount_high is not None:
                total_estimated_amount += (row.amount_low + row.amount_high) / 2.0
            elif row.amount_low is not None:
                total_estimated_amount += row.amount_low

        recency_days = max(0, (now - newest.replace(tzinfo=timezone.utc) if newest.tzinfo is None else now - newest).days)
        recency_score = max(0.0, 18.0 - recency_days * 0.75)
        cluster_score = min(24.0, 6.0 * (len(cluster_rows) - 1))
        amount_score = min(18.0, math.log10(max(total_estimated_amount, 1.0)) * 4.5)
        bias_score = 10.0 if buy_count > sell_count else 2.0 if buy_count == sell_count else -4.0
        confidence_score = min(20.0, sum((row.confidence or 0.0) for row in cluster_rows) / len(cluster_rows) * 20.0)
        diversity_score = 5.0 if len(unique_people) >= 2 else 0.0
        score = round(recency_score + cluster_score + amount_score + bias_score + confidence_score + diversity_score, 2)

        reasons = [
            f"{len(cluster_rows)} filings within {cluster_window_days} days",
            f"{buy_count} buys / {sell_count} sells",
        ]
        if len(unique_people) >= 2:
            reasons.append("cluster spans multiple lawmakers or offices")
        if total_estimated_amount:
            reasons.append(f"estimated disclosed size about ${total_estimated_amount:,.0f}")
        if score < alert_threshold:
            continue

        alerts.append(
            PTRClusterAlert(
                ticker=ticker,
                score=score,
                filing_count=len(cluster_rows),
                total_estimated_amount=round(total_estimated_amount, 2),
                buy_count=buy_count,
                sell_count=sell_count,
                unique_people=unique_people,
                filing_dates=filing_dates,
                reasons=reasons,
                filings=[asdict(row) for row in cluster_rows],
            )
        )

    return sorted(alerts, key=lambda alert: alert.score, reverse=True)


class PoliticianPTRMonitorTool(BaseTool):
    name = "politician_ptr_monitor"
    description = (
        "Scrape House/Senate PTR disclosures, extract structured trades with the repo LLM, "
        "and score clustered high-conviction trades for alerting."
    )
    parameters = {
        "type": "object",
        "properties": {
            "source_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "PTR disclosure URLs or mirrors to scrape.",
            },
            "cluster_window_days": {
                "type": "integer",
                "default": DEFAULT_WINDOW_DAYS,
                "description": "Lookback window for clustering filings by ticker.",
            },
            "alert_threshold": {
                "type": "number",
                "default": DEFAULT_ALERT_THRESHOLD,
                "description": "Minimum score required to emit a clustered-trade alert.",
            },
            "max_sources": {
                "type": "integer",
                "default": 8,
                "description": "Maximum number of source URLs to ingest in one pass.",
            },
            "max_chars_per_source": {
                "type": "integer",
                "default": 12000,
                "description": "Character cap per source page before LLM extraction.",
            },
            "use_llm": {
                "type": "boolean",
                "default": True,
                "description": "Whether to run the structured extraction through the repo LLM.",
            },
            "model_name": {
                "type": "string",
                "description": "Optional explicit model override for the repo LLM factory.",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        source_urls = kwargs.get("source_urls") or _urls_from_env()
        if not source_urls:
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        f"No source URLs supplied. Pass source_urls or set {DEFAULT_SOURCE_ENV} "
                        "to a comma-separated list of PTR disclosure pages."
                    ),
                },
                ensure_ascii=False,
            )

        max_sources = int(kwargs.get("max_sources") or 8)
        source_urls = list(dict.fromkeys(source_urls))[:max_sources]
        cluster_window_days = int(kwargs.get("cluster_window_days") or DEFAULT_WINDOW_DAYS)
        alert_threshold = float(kwargs.get("alert_threshold") or DEFAULT_ALERT_THRESHOLD)
        max_chars_per_source = int(kwargs.get("max_chars_per_source") or 12000)
        use_llm = bool(kwargs.get("use_llm", True))
        model_name = kwargs.get("model_name")

        documents = fetch_ptr_documents(source_urls, max_chars_per_source=max_chars_per_source)
        if use_llm:
            filings = extract_ptr_filings_with_llm(documents, model_name=model_name)
        else:
            filings = _fallback_extract_ptr_filings(documents)

        alerts = score_ptr_clusters(
            filings,
            cluster_window_days=cluster_window_days,
            alert_threshold=alert_threshold,
        )
        alert_packet = {
            "session_template": "sessions/politician_ptr_alert_boi.md",
            "summary": {
                "source_count": len(source_urls),
                "document_count": len(documents),
                "filing_count": len(filings),
                "cluster_alert_count": len(alerts),
            },
            "top_alerts": [asdict(alert) for alert in alerts[:10]],
            "generated_at": _now_utc_iso(),
        }

        return json.dumps(
            {
                "status": "ok",
                "documents": [asdict(doc) for doc in documents],
                "filings": [asdict(filing) for filing in filings],
                "cluster_alerts": [asdict(alert) for alert in alerts],
                "alert_packet": alert_packet,
            },
            ensure_ascii=False,
        )


def _fallback_extract_ptr_filings(documents: list[PTRDocument]) -> list[PTRFiling]:
    """Regex fallback when the LLM is unavailable or unconfigured."""
    filings: list[PTRFiling] = []
    ticker_pattern = re.compile(r"\b[A-Z]{1,5}\b")
    amount_pattern = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:\s*[kK])?(?:\s*[-–]\s*\$?\d[\d,]*(?:\.\d+)?(?:\s*[kK])?)?")
    for doc in documents:
        tickers = {match.group(0) for match in ticker_pattern.finditer(doc.text)}
        amounts = amount_pattern.findall(doc.text)
        for ticker in sorted(tickers):
            if ticker in {"HOUSE", "SENATE", "PTR", "DATE", "URL", "TEXT", "LINKS"}:
                continue
            amount_text = amounts[0] if amounts else None
            amount_low, amount_high = _parse_amount_range(amount_text)
            filings.append(
                PTRFiling(
                    source_url=doc.source_url,
                    filing_date=None,
                    politician_name=None,
                    chamber=None,
                    office=None,
                    party=None,
                    state=None,
                    ticker=ticker,
                    asset_name=None,
                    transaction_type=None,
                    direction=None,
                    amount_text=amount_text,
                    amount_low=amount_low,
                    amount_high=amount_high,
                    confidence=0.2,
                    notes="regex fallback; configure LLM extraction for richer structured output",
                )
            )
    return filings


def _urls_from_env() -> list[str]:
    raw = os.getenv(DEFAULT_SOURCE_ENV, "").strip()
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[;,\n]", raw) if item.strip()]
