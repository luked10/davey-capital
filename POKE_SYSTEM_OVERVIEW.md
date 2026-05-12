# Poke system overview

Poke orchestrates the Davey Capital strategy as a stateless control plane. Research, execution, and reporting are separated into explicit steps so each run can be reconstructed from repo state instead of hidden memory.

## Strategy orchestration

- Poke collects the current strategy context and routes work to the appropriate execution boi.
- Trading decisions are written into repo-backed session files so the next step can resume from disk.
- The broker layer stays behind the shared boi contract so execution can switch venues without changing the strategy layer.

## Stateless repo memory

- Session checkpoints live in the repo under sessions/.
- Decision logs, fills, and account snapshots are saved as plain text markdown artifacts.
- The repo is treated as the durable memory layer for the dashboard and follow-up actions.

## Notion integration boi

- Notion is used as the external knowledge surface for summaries, status, and task tracking.
- Poke can mirror strategy notes or checkpoint summaries into Notion so the workspace stays in sync with repo state.
- The Notion layer should stay write-only at the summary level unless a task explicitly requires deeper updates.

## Broker factory

The exact broker factory function name is get_broker_boi in autohedge/autohedge/brokers/factory_boi.py.

## Verification one-liner

From inside the autohedge folder, run:

poetry run python -c "from autohedge.brokers.factory_boi import get_broker_boi; b=get_broker_boi('robinhood'); s=b.get_account_snapshot(); print({'cash_balance': s.cash_balance, 'equity_value': s.equity_value, 'buying_power': s.buying_power, 'positions': len(s.positions)})"
