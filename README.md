# quant-hub-bridge

Bridge repo for Vibe-Trading signal generation and AutoHedge execution.

## Structure

- `vibe-trading/` — signal generation, strategy research, and pre-trade analytics
- `autohedge/` — execution, routing, sizing, and risk controls
- `logs/` — execution artifacts, run logs, and handoff records

## Signal -> Execution flow

1. Vibe-Trading produces a signal with the strategy, asset, direction, and context.
2. AutoHedge consumes that signal, validates it against risk rules, and routes execution.
3. Execution status and outcome are written back into Notion.

## Logging and communication

Notion is the live status dashboard for the GitHub-based execution workflow.

Use the `Trading & Performance Log` database to track:

- Date
- Strategy
- Asset
- Signal Type
- Result
- Notes
- System Status
- Communication Log

The `System Status` / `Communication Log` fields are the internal handoff layer between Vibe-Trading and AutoHedge, while this repo holds the execution modules and supporting logs.
