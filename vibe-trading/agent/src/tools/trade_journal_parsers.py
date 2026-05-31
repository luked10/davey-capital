"""Trade journal format adapters.

Each parser normalizes one broker export format into a list of TradeRecord.
Supported: Tonghuashun (同花顺), Eastmoney (东方财富), Futu (富途), generic CSV.

Encoding fallback order for CSV: utf-8 → utf-8-sig → gbk → gb2312.
Excel (.xlsx/.xls) always opens as utf-8 internally via openpyxl/xlrd.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

FormatName = str  # "tonghuashun" | "eastmoney" | "futu" | "generic" | "unknown"

_A_SHARE_EXCHANGE_MAP = {
    # prefix → suffix; Shanghai Main + STAR, Shenzhen Main + SME + ChiNext, BSE
    ("6",): ".SH",
    ("0", "3"): ".SZ",
    ("4", "8"): ".BJ",
}

_BUY_TOKENS = {"buy", "b", "买入", "证券买入", "融资买入", "做多", "long"}
_SELL_TOKENS = {"sell", "s", "卖出", "证券卖出", "融券卖出", "做空", "short"}


@dataclass(frozen=True)
class TradeRecord:
    """Standardized trade record (immutable).

    Attributes:
        datetime: ISO8601 timestamp, e.g. "2026-01-15 09:35:00".
        symbol: Exchange-qualified symbol, e.g. "600519.SH" / "AAPL" / "BTC-USDT".
        name: Human-readable instrument name.
        side: "buy" or "sell".
        quantity: Filled quantity.
        price: Filled price.
        amount: Gross amount (quantity * price, pre-fee).
        fee: Total fees (commission + stamp + transfer).
        market: "china_a" / "us" / "hk" / "crypto" / "other".
    """

    datetime: str
    symbol: str
    name: str
    side: str
    quantity: float
    price: float
    amount: float
    fee: float
    market: str


# ---------------- File loading ----------------

def load_dataframe(path: str | Path) -> pd.DataFrame:
    """Load a CSV/Excel file into a DataFrame with encoding fallback.

    Args:
        path: Path to the file (.csv/.xlsx/.xls).

    Returns:
        Parsed DataFrame with raw column names (no normalization).

    Raises:
        FileNotFoundError: File does not exist.
        ValueError: Unsupported extension or all encodings failed.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")

    ext = p.suffix.lower()
    if ext in {".xlsx", ".xls"}:
        return pd.read_excel(p, dtype=str)
    if ext != ".csv":
        raise ValueError(f"Unsupported extension: {ext}")

    last_err: Exception | None = None
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312"):
        try:
            return pd.read_csv(p, dtype=str, encoding=enc)
        except UnicodeDecodeError as exc:
            last_err = exc
    raise ValueError(f"Failed to decode CSV with utf-8/gbk/gb2312: {last_err}")


# ---------------- Format detection ----------------

def detect_format(df: pd.DataFrame) -> FormatName:
    """Detect broker format by column-name signature.

    Args:
        df: Raw DataFrame from load_dataframe.

    Returns:
        Format identifier; "unknown" when nothing matches (caller may still
        try GenericCSVParser).
    """
    cols = set(df.columns.astype(str))

    if {"成交时间", "证券代码", "操作"}.issubset(cols):
        return "tonghuashun"
    if {"买卖标志", "股票代码"}.issubset(cols) or {"买卖标志", "成交均价"}.issubset(cols):
        return "eastmoney"
    if {"Date", "Symbol", "Side"}.issubset(cols) or {"Date", "Symbol", "Direction"}.issubset(cols):
        return "futu"

    # Generic: any subset containing time/symbol/side hints
    lowered = {c.lower() for c in cols}
    if any(c in lowered for c in ("datetime", "time", "date")) and any(
        c in lowered for c in ("symbol", "ticker", "code")
    ):
        return "generic"
    return "unknown"


# ---------------- Parsers ----------------

def _normalize_side(raw: Any) -> str:
    """Return 'buy'/'sell', falling back to 'buy'."""
    s = str(raw).strip().lower()
    if s in _SELL_TOKENS or any(tok in s for tok in _SELL_TOKENS):
        return "sell"
    return "buy"


def _qualify_a_share(code: str) -> str:
    """Append .SH/.SZ/.BJ suffix to a bare A-share ticker."""
    code = str(code).strip().zfill(6)
    if "." in code:
        return code.upper()
    first = code[0]
    for prefixes, suffix in _A_SHARE_EXCHANGE_MAP.items():
        if first in prefixes:
            return code + suffix
    return code


def _to_float(val: Any, default: float = 0.0) -> float:
    """Safely cast to float; return default on failure."""
    if val is None:
        return default
    try:
        s = str(val).replace(",", "").strip()
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def _column_or_default(
    df: pd.DataFrame,
    column: str | None,
    *,
    default: Any,
    row_count: int,
) -> list[Any]:
    """Return column values or a row-aligned default list."""
    if column and column in df.columns:
        return df[column].tolist()
    return [default] * row_count


def parse_tonghuashun(df: pd.DataFrame) -> list[TradeRecord]:
    """Parse 同花顺 exports.

    Expected columns: 成交时间, 证券代码, 证券名称, 操作, 成交数量, 成交价格,
    成交金额, 手续费, 印花税, 过户费.
    """
    row_count = len(df)
    datetimes = [str(value).strip() for value in _column_or_default(df, "成交时间", default="", row_count=row_count)]
    symbols = [_qualify_a_share(value) for value in _column_or_default(df, "证券代码", default="", row_count=row_count)]
    names = [str(value).strip() for value in _column_or_default(df, "证券名称", default="", row_count=row_count)]
    sides = [_normalize_side(value) for value in _column_or_default(df, "操作", default=None, row_count=row_count)]

    qty_values = [_to_float(value) for value in _column_or_default(df, "成交数量", default=None, row_count=row_count)]
    price_values = [_to_float(value) for value in _column_or_default(df, "成交价格", default=None, row_count=row_count)]
    amount_raw_values = [_to_float(value) for value in _column_or_default(df, "成交金额", default=None, row_count=row_count)]
    fee_values = [
        _to_float(shouxu) + _to_float(yinhua) + _to_float(guohu)
        for shouxu, yinhua, guohu in zip(
            _column_or_default(df, "手续费", default=None, row_count=row_count),
            _column_or_default(df, "印花税", default=None, row_count=row_count),
            _column_or_default(df, "过户费", default=None, row_count=row_count),
        )
    ]
    amount_values = [
        amount_raw or (qty * price)
        for amount_raw, qty, price in zip(amount_raw_values, qty_values, price_values)
    ]

    return [
        TradeRecord(
            datetime=dt,
            symbol=symbol,
            name=name,
            side=side,
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        )
        for dt, symbol, name, side, qty, price, amount, fee in zip(
            datetimes,
            symbols,
            names,
            sides,
            qty_values,
            price_values,
            amount_values,
            fee_values,
        )
    ]


def parse_eastmoney(df: pd.DataFrame) -> list[TradeRecord]:
    """Parse 东方财富 exports.

    Expected columns: 成交日期 (YYYYMMDD), 成交时间 (HH:MM:SS), 股票代码,
    股票名称, 买卖标志 (B/S), 成交数量, 成交均价, 成交金额, 佣金, 印花税.
    """
    row_count = len(df)
    raw_dates = [str(value).strip() for value in _column_or_default(df, "成交日期", default="", row_count=row_count)]
    raw_times = [str(value).strip() for value in _column_or_default(df, "成交时间", default="", row_count=row_count)]
    iso_dates = [
        f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        if len(raw_date) == 8 and raw_date.isdigit()
        else raw_date
        for raw_date in raw_dates
    ]
    datetimes = [f"{iso_date} {raw_time}".strip() for iso_date, raw_time in zip(iso_dates, raw_times)]
    symbols = [_qualify_a_share(value) for value in _column_or_default(df, "股票代码", default="", row_count=row_count)]
    names = [str(value).strip() for value in _column_or_default(df, "股票名称", default="", row_count=row_count)]
    sides = [_normalize_side(value) for value in _column_or_default(df, "买卖标志", default=None, row_count=row_count)]
    qty_values = [_to_float(value) for value in _column_or_default(df, "成交数量", default=None, row_count=row_count)]
    price_values = [_to_float(value) for value in _column_or_default(df, "成交均价", default=None, row_count=row_count)]
    amount_raw_values = [_to_float(value) for value in _column_or_default(df, "成交金额", default=None, row_count=row_count)]
    fee_values = [
        _to_float(yongjin) + _to_float(yinhua)
        for yongjin, yinhua in zip(
            _column_or_default(df, "佣金", default=None, row_count=row_count),
            _column_or_default(df, "印花税", default=None, row_count=row_count),
        )
    ]
    amount_values = [
        amount_raw or (qty * price)
        for amount_raw, qty, price in zip(amount_raw_values, qty_values, price_values)
    ]

    return [
        TradeRecord(
            datetime=dt,
            symbol=symbol,
            name=name,
            side=side,
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        )
        for dt, symbol, name, side, qty, price, amount, fee in zip(
            datetimes,
            symbols,
            names,
            sides,
            qty_values,
            price_values,
            amount_values,
            fee_values,
        )
    ]


def _futu_market(symbol: str, market_hint: str) -> str:
    """Infer market from symbol/market column."""
    hint = market_hint.strip().lower()
    if hint in {"hk", "us", "cn"}:
        return {"hk": "hk", "us": "us", "cn": "china_a"}[hint]
    if symbol.endswith(".HK"):
        return "hk"
    if symbol.isalpha() or "." not in symbol:
        return "us"
    return "other"


def parse_futu(df: pd.DataFrame) -> list[TradeRecord]:
    """Parse 富途 exports (English headers, HK+US mix).

    Expected columns: Date, Time, Symbol, Name, Side, Quantity, Price,
    Amount, Commission, Platform Fee, Market (optional).
    """
    row_count = len(df)
    dates = [str(value).strip() for value in _column_or_default(df, "Date", default="", row_count=row_count)]
    times = [str(value).strip() for value in _column_or_default(df, "Time", default="", row_count=row_count)]
    datetimes = [f"{date} {time}".strip() for date, time in zip(dates, times)]

    symbols = [
        str(value).strip().upper()
        for value in _column_or_default(df, "Symbol", default="", row_count=row_count)
    ]
    names = [str(value).strip() for value in _column_or_default(df, "Name", default="", row_count=row_count)]
    side_source_col = "Side" if "Side" in df.columns else "Direction"
    sides = [
        _normalize_side(value)
        for value in _column_or_default(df, side_source_col, default=None, row_count=row_count)
    ]
    qty_values = [_to_float(value) for value in _column_or_default(df, "Quantity", default=None, row_count=row_count)]
    price_values = [_to_float(value) for value in _column_or_default(df, "Price", default=None, row_count=row_count)]
    amount_raw_values = [_to_float(value) for value in _column_or_default(df, "Amount", default=None, row_count=row_count)]
    fee_values = [
        _to_float(commission) + _to_float(platform_fee)
        for commission, platform_fee in zip(
            _column_or_default(df, "Commission", default=None, row_count=row_count),
            _column_or_default(df, "Platform Fee", default=None, row_count=row_count),
        )
    ]
    amount_values = [
        amount_raw or (qty * price)
        for amount_raw, qty, price in zip(amount_raw_values, qty_values, price_values)
    ]
    market_hints = [
        str(value)
        for value in _column_or_default(df, "Market", default="", row_count=row_count)
    ]
    markets = [_futu_market(symbol, market_hint) for symbol, market_hint in zip(symbols, market_hints)]

    return [
        TradeRecord(
            datetime=dt,
            symbol=symbol,
            name=name,
            side=side,
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market=market,
        )
        for dt, symbol, name, side, qty, price, amount, fee, market in zip(
            datetimes,
            symbols,
            names,
            sides,
            qty_values,
            price_values,
            amount_values,
            fee_values,
            markets,
        )
    ]


def parse_generic(df: pd.DataFrame) -> list[TradeRecord]:
    """Parse a generic CSV with lowercase English headers.

    Matches columns case-insensitively. Expected (any alias in parens):
        datetime (time/date+time), symbol (ticker/code), name, side (direction),
        quantity (qty/size), price, amount (value/notional), fee (commission).
    """
    colmap: dict[str, str] = {}
    for col in df.columns:
        key = str(col).strip().lower()
        colmap[key] = col

    def pick(*names: str) -> str | None:
        for n in names:
            if n in colmap:
                return colmap[n]
        return None

    dt_col = pick("datetime", "time")
    date_col = pick("date")
    sym_col = pick("symbol", "ticker", "code")
    name_col = pick("name", "instrument")
    side_col = pick("side", "direction", "action")
    qty_col = pick("quantity", "qty", "size", "volume")
    price_col = pick("price")
    amount_col = pick("amount", "value", "notional")
    fee_col = pick("fee", "commission", "fees")

    row_count = len(df)
    if dt_col:
        datetimes = [
            str(value).strip()
            for value in _column_or_default(df, dt_col, default="", row_count=row_count)
        ]
    elif date_col:
        datetimes = [
            str(value).strip()
            for value in _column_or_default(df, date_col, default="", row_count=row_count)
        ]
    else:
        datetimes = [""] * row_count

    symbols = [
        str(value).strip()
        for value in _column_or_default(df, sym_col, default="", row_count=row_count)
    ]
    names = [
        str(value).strip()
        for value in _column_or_default(df, name_col, default="", row_count=row_count)
    ]
    sides = [
        _normalize_side(value)
        for value in _column_or_default(df, side_col, default="buy", row_count=row_count)
    ]
    qty_values = [_to_float(value) for value in _column_or_default(df, qty_col, default=0.0, row_count=row_count)]
    price_values = [_to_float(value) for value in _column_or_default(df, price_col, default=0.0, row_count=row_count)]
    if amount_col:
        amount_raw_values = [
            _to_float(value) for value in _column_or_default(df, amount_col, default=None, row_count=row_count)
        ]
    else:
        amount_raw_values = [qty * price for qty, price in zip(qty_values, price_values)]
    fee_values = [_to_float(value) for value in _column_or_default(df, fee_col, default=0.0, row_count=row_count)]
    markets = [_infer_market_from_symbol(symbol) for symbol in symbols]
    amount_values = [
        amount_raw or (qty * price)
        for amount_raw, qty, price in zip(amount_raw_values, qty_values, price_values)
    ]

    return [
        TradeRecord(
            datetime=dt,
            symbol=symbol.upper(),
            name=name,
            side=side,
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market=market,
        )
        for dt, symbol, name, side, qty, price, amount, fee, market in zip(
            datetimes,
            symbols,
            names,
            sides,
            qty_values,
            price_values,
            amount_values,
            fee_values,
            markets,
        )
    ]


def _infer_market_from_symbol(symbol: str) -> str:
    """Best-effort market inference from a symbol string."""
    s = symbol.upper()
    if s.endswith(".HK"):
        return "hk"
    if s.endswith(".SH") or s.endswith(".SZ") or s.endswith(".BJ"):
        return "china_a"
    if "-" in s and any(quote in s for quote in ("USDT", "USDC", "BTC", "USD")):
        return "crypto"
    if s.isalpha():
        return "us"
    return "other"


_PARSERS = {
    "tonghuashun": parse_tonghuashun,
    "eastmoney": parse_eastmoney,
    "futu": parse_futu,
    "generic": parse_generic,
}


def parse_file(path: str | Path) -> tuple[FormatName, list[TradeRecord]]:
    """End-to-end: load file, detect format, parse.

    Args:
        path: File path.

    Returns:
        (format_name, records). Falls back to generic if detection is unknown
        but columns look parsable; otherwise raises ValueError.

    Raises:
        ValueError: Unknown format with no usable columns.
    """
    df = load_dataframe(path)
    fmt = detect_format(df)
    if fmt == "unknown":
        try:
            records = parse_generic(df)
            if records and records[0].symbol:
                return "generic", records
        except Exception:
            pass
        raise ValueError(f"Unrecognized trade journal format. Columns: {list(df.columns)}")
    return fmt, _PARSERS[fmt](df)


def records_to_dataframe(records: list[TradeRecord]) -> pd.DataFrame:
    """Convert records to a standardized DataFrame (datetime column parsed)."""
    if not records:
        return pd.DataFrame(columns=[f.name for f in TradeRecord.__dataclass_fields__.values()])
    df = pd.DataFrame([asdict(r) for r in records])
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df.sort_values("datetime").reset_index(drop=True)
