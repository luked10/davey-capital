#!/usr/bin/env python3
"""Deterministic parity smoke for parser-vectorization refactor."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
VIBE_AGENT_ROOT = REPO_ROOT / "vibe-trading" / "agent"
if str(VIBE_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(VIBE_AGENT_ROOT))

from src.tools.trade_journal_parsers import (  # noqa: E402
    TradeRecord,
    _futu_market,
    _infer_market_from_symbol,
    _normalize_side,
    _qualify_a_share,
    _to_float,
    parse_eastmoney,
    parse_futu,
    parse_generic,
    parse_tonghuashun,
)


def _legacy_parse_tonghuashun(df: pd.DataFrame) -> list[TradeRecord]:
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        qty = _to_float(row.get("成交数量"))
        price = _to_float(row.get("成交价格"))
        amount = _to_float(row.get("成交金额")) or qty * price
        fee = _to_float(row.get("手续费")) + _to_float(row.get("印花税")) + _to_float(row.get("过户费"))
        records.append(TradeRecord(
            datetime=str(row.get("成交时间", "")).strip(),
            symbol=_qualify_a_share(row.get("证券代码", "")),
            name=str(row.get("证券名称", "")).strip(),
            side=_normalize_side(row.get("操作")),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        ))
    return records


def _legacy_parse_eastmoney(df: pd.DataFrame) -> list[TradeRecord]:
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        raw_date = str(row.get("成交日期", "")).strip()
        raw_time = str(row.get("成交时间", "")).strip()
        if len(raw_date) == 8 and raw_date.isdigit():
            iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            iso_date = raw_date
        dt = f"{iso_date} {raw_time}".strip()
        qty = _to_float(row.get("成交数量"))
        price = _to_float(row.get("成交均价"))
        amount = _to_float(row.get("成交金额")) or qty * price
        fee = _to_float(row.get("佣金")) + _to_float(row.get("印花税"))
        records.append(TradeRecord(
            datetime=dt,
            symbol=_qualify_a_share(row.get("股票代码", "")),
            name=str(row.get("股票名称", "")).strip(),
            side=_normalize_side(row.get("买卖标志")),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        ))
    return records


def _legacy_parse_futu(df: pd.DataFrame) -> list[TradeRecord]:
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        date = str(row.get("Date", "")).strip()
        time = str(row.get("Time", "")).strip()
        dt = f"{date} {time}".strip()
        symbol = str(row.get("Symbol", "")).strip().upper()
        qty = _to_float(row.get("Quantity"))
        price = _to_float(row.get("Price"))
        amount = _to_float(row.get("Amount")) or qty * price
        fee = _to_float(row.get("Commission")) + _to_float(row.get("Platform Fee"))
        records.append(TradeRecord(
            datetime=dt,
            symbol=symbol,
            name=str(row.get("Name", "")).strip(),
            side=_normalize_side(row.get("Side") if "Side" in df.columns else row.get("Direction")),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market=_futu_market(symbol, str(row.get("Market", ""))),
        ))
    return records


def _legacy_parse_generic(df: pd.DataFrame) -> list[TradeRecord]:
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

    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        if dt_col:
            dt = str(row.get(dt_col, "")).strip()
        elif date_col:
            dt = str(row.get(date_col, "")).strip()
        else:
            dt = ""
        symbol = str(row.get(sym_col, "")).strip() if sym_col else ""
        qty = _to_float(row.get(qty_col)) if qty_col else 0.0
        price = _to_float(row.get(price_col)) if price_col else 0.0
        amount = _to_float(row.get(amount_col)) if amount_col else qty * price
        fee = _to_float(row.get(fee_col)) if fee_col else 0.0
        market = _infer_market_from_symbol(symbol)
        records.append(TradeRecord(
            datetime=dt,
            symbol=symbol.upper(),
            name=str(row.get(name_col, "")).strip() if name_col else "",
            side=_normalize_side(row.get(side_col) if side_col else "buy"),
            quantity=qty,
            price=price,
            amount=amount or qty * price,
            fee=fee,
            market=market,
        ))
    return records


def _assert_parity(
    parser_name: str,
    current: list[TradeRecord],
    legacy: list[TradeRecord],
) -> None:
    if len(current) != len(legacy):
        raise AssertionError(
            f"{parser_name} parity mismatch\n"
            f"current_len={len(current)} legacy_len={len(legacy)}"
        )

    for index, (current_record, legacy_record) in enumerate(zip(current, legacy)):
        current_dict = asdict(current_record)
        legacy_dict = asdict(legacy_record)
        for field_name in current_dict:
            current_value = current_dict[field_name]
            legacy_value = legacy_dict[field_name]
            if pd.isna(current_value) and pd.isna(legacy_value):
                continue
            if current_value != legacy_value:
                raise AssertionError(
                    f"{parser_name} parity mismatch at row={index}, field={field_name}\n"
                    f"current={current_dict}\n"
                    f"legacy={legacy_dict}"
                )


def _run_tonghuashun_case() -> None:
    fixture = pd.DataFrame(
        [
            {
                "成交时间": "2026-05-30 09:31:00",
                "证券代码": "600519",
                "证券名称": "贵州茅台",
                "操作": "证券买入",
                "成交数量": "100",
                "成交价格": "10.5",
                "成交金额": "",
                "手续费": "1.2",
                "印花税": "0.3",
                "过户费": "0.1",
            },
            {
                "成交时间": "2026-05-30 10:01:00",
                "证券代码": "000001",
                "证券名称": "平安银行",
                "操作": "卖出",
                "成交数量": "5",
                "成交价格": "2",
                "成交金额": "0",
                "手续费": "0",
                "印花税": "",
                "过户费": None,
            },
        ]
    )
    _assert_parity(
        "parse_tonghuashun",
        parse_tonghuashun(fixture),
        _legacy_parse_tonghuashun(fixture),
    )


def _run_eastmoney_case() -> None:
    fixture = pd.DataFrame(
        [
            {
                "成交日期": "20260530",
                "成交时间": "09:30:00",
                "股票代码": "300750",
                "股票名称": "宁德时代",
                "买卖标志": "B",
                "成交数量": "3,000",
                "成交均价": "11.2",
                "成交金额": "",
                "佣金": "1.5",
                "印花税": "0.5",
            },
            {
                "成交日期": "bad-date",
                "成交时间": "",
                "股票代码": "688001.SH",
                "股票名称": "华兴源创",
                "买卖标志": "融券卖出",
                "成交数量": None,
                "成交均价": "7",
                "成交金额": "0",
                "佣金": "0",
                "印花税": None,
            },
        ]
    )
    _assert_parity(
        "parse_eastmoney",
        parse_eastmoney(fixture),
        _legacy_parse_eastmoney(fixture),
    )


def _run_futu_case() -> None:
    fixture_side = pd.DataFrame(
        [
            {
                "Date": "2026-05-30",
                "Time": "09:30:00",
                "Symbol": "aapl",
                "Name": "Apple",
                "Side": "Buy",
                "Quantity": "10",
                "Price": "180.2",
                "Amount": "",
                "Commission": "1.0",
                "Platform Fee": "0.2",
                "Market": "US",
            },
            {
                "Date": "2026-05-30",
                "Time": "10:00:00",
                "Symbol": "00700.hk",
                "Name": "Tencent",
                "Side": "SELL",
                "Quantity": "2",
                "Price": "320",
                "Amount": "0",
                "Commission": "0",
                "Platform Fee": "",
                "Market": "",
            },
        ]
    )
    fixture_direction = pd.DataFrame(
        [
            {
                "Date": "2026-05-31",
                "Time": "11:30:00",
                "Symbol": "TSLA",
                "Name": "Tesla",
                "Direction": "short",
                "Quantity": "1",
                "Price": "250",
                "Amount": "",
                "Commission": "0.5",
                "Platform Fee": "0",
                "Market": "nan",
            }
        ]
    )
    _assert_parity(
        "parse_futu(side)",
        parse_futu(fixture_side),
        _legacy_parse_futu(fixture_side),
    )
    _assert_parity(
        "parse_futu(direction)",
        parse_futu(fixture_direction),
        _legacy_parse_futu(fixture_direction),
    )


def _run_generic_case() -> None:
    fixture = pd.DataFrame(
        [
            {
                "Time": "2026-05-30 09:30:00",
                "Ticker": "btc-usdt",
                "Instrument": "BTC",
                "Action": "sell",
                "Qty": "0.25",
                "Price": "68000",
                "Value": "",
                "Fees": "2.1",
            },
            {
                "Time": "",
                "Ticker": "000001.sz",
                "Instrument": "平安银行",
                "Action": None,
                "Qty": "",
                "Price": "",
                "Value": "0",
                "Fees": "",
            },
        ]
    )
    _assert_parity(
        "parse_generic",
        parse_generic(fixture),
        _legacy_parse_generic(fixture),
    )


def main() -> None:
    _run_tonghuashun_case()
    _run_eastmoney_case()
    _run_futu_case()
    _run_generic_case()
    print("trade journal parser vectorization smoke: ok")


if __name__ == "__main__":
    main()
