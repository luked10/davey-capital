# quant-hub-bridge

Stateless trading bridge. The repository is the source of truth for workflow checkpoints, decisions, fills, and broker wiring.

## Operating model

- No hidden memory or off-repo state is assumed.
- Each trading day is recorded through session files under sessions/.
- Decisions and fills are written back into the repo so the audit trail stays versioned.

## Daily checkpoints

- sessions/pre_market_boi.md
- sessions/execution_boi.md
- sessions/midday_boi.md
- sessions/close_boi.md

## Audit trail templates

- sessions/decision_log_boi.md
- sessions/fill_report_boi.md

## Broker abstraction layer

- autohedge/autohedge/brokers/base_boi.py
- autohedge/autohedge/brokers/robinhood_boi.py
- autohedge/autohedge/brokers/robinhood_state_boi.py
- autohedge/autohedge/brokers/solana_boi.py
- autohedge/autohedge/brokers/factory_boi.py

The adapter layer keeps broker-specific behavior behind a shared contract so execution can switch between Robinhood and Solana without changing the rest of the workflow.

## Repository layout

- vibe-trading/ — signal generation and research modules
- autohedge/ — execution and routing modules
- nova-alpha/ — supplemental signal logic and notes
- logs/ — bridge-level execution logs
- sessions/ — daily stateless checkpoints and audit templates

## Workflow

1. Research and signals can be generated anywhere in the repo.
2. The session files capture the plan before execution.
3. Broker adapters resolve the active execution venue.
4. Fills and decisions are recorded back into sessions/ and logs/.
