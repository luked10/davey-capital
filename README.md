# davey-capital

Stateless trading bridge. The repository is the source of truth for workflow checkpoints, decisions, fills, and broker wiring.

## Operating model

- No hidden memory or off-repo state is assumed.
- Each trading day is recorded through session files under sessions/.
- Decisions and fills are written back into the repo so the audit trail stays versioned.

## Daily checkpoints

- sessions/pre_market_agent.md
- sessions/execution_agent.md
- sessions/midday_agent.md
- sessions/close_agent.md

## Audit trail templates

- sessions/decision_log_agent.md
- sessions/fill_report_agent.md

## Broker abstraction layer

- autohedge/autohedge/brokers/base_agent.py
- autohedge/autohedge/brokers/robinhood_agent.py
- autohedge/autohedge/brokers/solana_agent.py
- autohedge/autohedge/brokers/factory_agent.py

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
