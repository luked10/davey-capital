# Davey Capital / Quant-Hub-Bridge — Agent Memory

## Architecture Summary

- `vibe-trading/` — research, factor analysis, backtesting, signal tooling.
- `contracts/` — canonical bridge payloads (`bridge_contract.py`: `ExecutionIntent`,
  `validate_execution_intent`, `execution_intent_to_broker_order`) plus watcher
  payload schemas (`overnight_scaffold.py`).
- `autohedge/` — broker abstraction (`brokers/`), dry-run execution scaffolds, and
  local-only runtime scaffolds: `risk/circuit_breaker.py`, `proposal/cached_proposer.py`,
  `runtime/runtime_state.py`, `audit/artifacts.py`, watcher (`overnight_scaffold.py`),
  engine/scheduler (`runtime_scaffold.py`).
- `nova-alpha/` — reporting surface; `report_scaffold.py` renders local daily markdown
  reports from repo-backed artifacts only.
- `scripts/` — deterministic offline smoke tests (the verification gate for every change).
- `sessions/`, `logs/` — repo-backed audit trail. `runtime_state.example.json` is the
  shared machine-readable memory template.
- Doctrine: stateless bridge; the repo is the source of truth; no hidden off-repo state.
  Tier 0 watcher is deterministic ($0), Tier 1 Poke triage is $0 marginal, Tier 2
  Sonnet/API structured proposals are rare and prompt-cached, Tier 3 human/Poke approval
  is mandatory before any live execution.

## Safety Invariants (No Exceptions)

- No real orders. No live Alpaca/Robinhood calls. No broker account reads in scaffolds.
- No network calls in smoke tests. No hardcoded secrets or credentials. No required env vars.
- `dry_run` defaults to `True` everywhere; unapproved `ExecutionIntent` must never execute.
- `validate_execution_intent(...)` is the gate; `execution_intent_to_broker_order(...)`
  raises on invalid intents and must never be bypassed.
- `risk.needs_human=True` and `run.needs_human=True` block execution.
- Scheduler never auto-starts on import (`LocalSchedulerScaffold(enabled=False)` default).
- Poke delivery is local queue/log only (`poke_bridge_local_queue`); the schema validator
  rejects any other destination.
- Circuit breaker (`CircuitBreakerConfig`) is disabled/no-op by default; enabling it for
  runtime is a human-reviewed change.
- `CachedProposerScaffold` accepts only clients with `is_local=True`; networked provider
  clients require explicit human-reviewed promotion.
- **Do not live-promote anything without human review.**

## Smoke Matrix (run from repo root)

```
python3 scripts/smoke_bridge_contract.py
python3 scripts/smoke_brokers_step2.py
python3 scripts/smoke_brokers_step3.py
python3 scripts/smoke_watcher_scaffold.py
python3 scripts/smoke_vectorization_step4.py
python3 scripts/smoke_trade_journal_parser_vectorization.py
python3 scripts/smoke_validation_trade_loader_vectorization.py
python3 scripts/smoke_engine_scheduler_step5.py
python3 scripts/smoke_execution_intent_structured_output.py
python3 scripts/smoke_watcher_fixtures_schema.py
python3 scripts/smoke_risk_circuit_breaker_scaffold.py
python3 scripts/smoke_cached_proposer_scaffold.py
python3 scripts/smoke_runtime_state_scaffold.py
python3 scripts/smoke_audit_artifacts_scaffold.py
python3 scripts/smoke_nova_alpha_report_scaffold.py
```

All smokes are deterministic and offline. A failing smoke blocks commit.

## Branch / Workflow Rules

1. Start only from clean `main`; verify with `git branch --show-current`,
   `git status --short`, `git log --oneline -10`.
2. One focused `feature/...` branch per slice; small commits; smokes alongside code.
3. Remove `__pycache__/` before committing. Do not commit `.DS_Store`.
4. Do not push from automated passes; humans review and push.
5. Parser/vectorization files stabilized in Builds 6–7 and `validation.py` timestamp
   semantics are frozen — do not touch without an explicit reason.

## Model / Tool Routing

- Cursor/Codex/Claude Code (build-time, subscription-funded): code edits, scaffold
  hardening, deterministic smoke runs, docs.
- Poke (Tier 1): local queue triage, reading watcher artifacts, drafting handoffs.
- Sonnet/Fable API (Tier 2, scarce): structured `ExecutionIntent` proposals only via the
  cached-proposer pattern (static cached prefix + small candidate suffix); currently
  scaffold-only with a fake client.
- Human (Tier 3): mandatory approval gate for anything live.
