from __future__ import annotations

import json

WATCHLIST_FUNDS = [
    "ABERDEEN_GROUP_PLC",
    "ALECTA_TJANSTEPENSION_OMSESIDIGT",
    "AEGON_ASSET_MANAGEMENT_UK_PLC",
    "ABN_AMRO_BANK_NV",
    "ADAMS_DIVERSIFIED_EQUITY_FUND_INC",
    "ALBION_FINANCIAL_GROUP",
    "ADDENDA_CAPITAL_INC",
    "AFFINITY_CAPITAL_ADVISORS_LLC",
    "AARON_WEALTH_ADVISORS_LLC",
    "ACCURATE_WEALTH_MANAGEMENT_LLC",
]

SOURCE_CHECKS = {
    "institutional_ownership": "poke/finance/get_institutional_ownership.ts",
    "sec_filings": "poke/finance/get_sec_filings.ts",
    "filings_tickers": "poke/finance/get_filings_tickers.ts",
}

ALERT_RULES = {
    "ownership_change_focus": [
        "new large holder",
        "material position increase",
        "material position decrease",
        "fresh filing around the May 15 drop",
    ],
    "watch_threshold_market_value_usd": 20000000,
}


def build_monitor_plan(reference_date: str = "2026-05-15") -> dict:
    return {
        "boi": "13F Drop Monitor Boi",
        "reference_date": reference_date,
        "watchlist_funds": WATCHLIST_FUNDS,
        "source_checks": SOURCE_CHECKS,
        "alert_rules": ALERT_RULES,
        "next_actions": [
            "refresh institutional ownership snapshots",
            "compare deltas against the previous reporting period",
            "flag any new or expanded positions for manual 13F review",
            "write results back to the repo log trail",
        ],
    }


def main() -> None:
    print(json.dumps(build_monitor_plan(), indent=2))


if __name__ == "__main__":
    main()
