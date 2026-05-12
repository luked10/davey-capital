# quant-hub-bridge

Consolidated bridge for signal generation, execution, and logging.

## Layout

- `vibe-trading/` — signal generation and research modules from Vibe-Trading
- `autohedge/` — execution and routing modules from AutoHedge
- `nova-alpha/` — supplemental signal logic and notes from nova-alpha-signals
- `logs/` — bridge-level execution logs

## Flow

1. Vibe-Trading emits a signal.
2. AutoHedge validates and executes it.
3. Nova Alpha can contribute supplemental signals or filters.
4. Notion remains the live status dashboard for handoffs, execution status, and outcomes.

## Status logging

Use the Notion database `Trading & Performance Log` to track:
- Date
- Strategy
- Asset
- Signal Type
- Result
- Notes
- System Status
- Communication Log

The `System Status` and `Communication Log` fields are the internal handoff layer between signal generation and execution.
