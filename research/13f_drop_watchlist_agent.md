# 13F Drop Watchlist Agent

Source note: this watchlist was built from the latest institutional ownership slices I could sort in the current bridge data. I used AAPL and NVDA as high-liquidity reference names and ranked the latest holders by market value.

## Top funds to watch Agent

These are the recurring / highest-value holders surfaced in the current data feed:
- ABERDEEN_GROUP_PLC
- ALECTA_TJANSTEPENSION_OMSESIDIGT
- AEGON_ASSET_MANAGEMENT_UK_PLC
- ABN_AMRO_BANK_NV
- ADAMS_DIVERSIFIED_EQUITY_FUND_INC
- ALBION_FINANCIAL_GROUP
- ADDENDA_CAPITAL_INC
- AFFINITY_CAPITAL_ADVISORS_LLC
- AARON_WEALTH_ADVISORS_LLC
- ACCURATE_WEALTH_MANAGEMENT_LLC

## Why these funds Agent

- They showed up as large holders in the latest AAPL and NVDA institutional ownership snapshots.
- The current bridge data is better for holder tracing than for direct 13F manager feed parsing, so this list is the cleanest immediate prep set.
- The most useful thing to watch into the May 15 drop is not just who filed, but who meaningfully increased or decreased exposure across liquid mega-cap names.

## Data source check Agent

Verified bridge-side data sources available for this workflow:
- poke/finance/get_institutional_ownership.ts
- poke/finance/get_sec_filings.ts
- poke/finance/get_filings_tickers.ts

Gap Agent:
- there is no dedicated 13F manager-filings endpoint in the current bridge toolset
- the monitoring script should therefore watch SEC filing arrivals and institutional ownership deltas, then flag names for manual 13F review

## Handoff Agent

- Planning Agent: keep the watchlist tight and rank by recurrence plus market value
- Research Agent: confirm which names are still present in the next ownership refresh
- Execution Agent: wire the monitor to produce repo logs and session updates
- Quality Check Agent: verify that the source set still covers SEC filing arrivals plus ownership changes
