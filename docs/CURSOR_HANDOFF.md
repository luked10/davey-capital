# Repo Memory Handoff (main@6060fea)

## Repo Purpose
- `vibe-trading` produces research/backtest artifacts and parser outputs.
- `contracts` defines the canonical bridge payloads between research and execution.
- `autohedge` contains broker adapters and local execution scaffolding.
- `nova-alpha` is downstream reporting context (non-execution in current flow).

## Current Merged Main State
- `main` includes merge commit `6060fea` and the overnight slices merged before/at that point.
- Completed merge stack on `main`:
  - Step 1 bridge contract (`contracts/bridge_contract.py` + smoke).
  - Step 1.5 validation hardening (`ExecutionValidationResult`, strict approval/dry-run checks).
  - Step 2 broker hardening (canonical `*Agent` naming; aliases preserved).
  - Step 3 broker consistency/test hardening.
  - Overnight #3-#5 scaffolds (watcher artifacts, runtime/scheduler scaffold).
  - Overnight #6 parser vectorization (`trade_journal_parsers.py` parity smoke).
  - Overnight #7 validation loader vectorization (`validation.py` parity smoke).
- `validation.py` conflict was resolved before merge; current `main` smokes are passing.

## Safety Invariants (Do Not Violate)
- No live trading integration in scaffold work.
- No Alpaca live calls and no real orders.
- `dry_run` defaults remain `True` across contract/scaffold payloads.
- `ExecutionIntent` must be validated and blocked unless explicitly approved for non-dry-run paths.
- Scheduler defaults stay disabled (`enabled=False`) unless a human explicitly enables it.
- No credentials, tokens, or secrets committed to repo files.
- No hidden network side effects in local scaffold/watcher/vectorization tasks.

## No-Live-Order Policy
- Treat all execution scaffolds as local simulation unless a human explicitly approves a reviewed live-integration branch.
- `AlpacaBrokerAgent` is fail-closed for non-dry-run mode and must remain that way in scaffold branches.
- Poke/Watcher paths are local artifact queues only; no broker API dispatch from automation branches.

## Branch and Worktree Workflow
1. Start from clean `main` only:
   - `pwd`
   - `git branch --show-current`
   - `git status --short`
   - `git log --oneline -8`
   - `git worktree list`
2. Continue only when:
   - current branch is `main`
   - working tree is clean
   - local `refs/remotes/origin/main` includes `6060fea` or newer (no fetch required).
3. Create focused feature branch/worktree for each slice; keep changes small and smoke-backed.
4. Remove generated `__pycache__/` before finalizing.
5. Do not push from automated stabilization passes.

## Smoke Command Matrix
- `python3 scripts/smoke_bridge_contract.py`
- `python3 scripts/smoke_brokers_step2.py`
- `python3 scripts/smoke_brokers_step3.py`
- `python3 scripts/smoke_watcher_scaffold.py`
- `python3 scripts/smoke_vectorization_step4.py`
- `python3 scripts/smoke_trade_journal_parser_vectorization.py`
- `python3 scripts/smoke_validation_trade_loader_vectorization.py`
- `python3 scripts/smoke_engine_scheduler_step5.py`
- Optional: `python3 scripts/smoke_local_pipeline_scaffold.py` (if present)

## Completed Steps (Checkpoint)
- Bridge contract and strict intent validation established.
- Broker naming consistency and safety-gate smokes established.
- Local watcher + poke queue scaffold implemented (artifact-only flow).
- Parser and validation loader vectorizations shipped with deterministic parity smokes.
- Runtime/scheduler scaffold added with default-disabled scheduler and explicit lifecycle smoke.

## Next Recommended Implementation Slices
1. `feature/intent-structured-output-harness`
   - Deterministic structured-output reliability harness for `ExecutionIntent` parsing/normalization.
2. `feature/watcher-fixtures-schema-hardening`
   - Expand watcher fixtures and lock JSONL artifact schema invariants.
3. `feature/risk-circuit-breaker-scaffold`
   - Add config scaffold for circuit-breakers with deterministic smokes (no behavior change defaults).
4. `feature/prompt-cached-sonnet-proposal-scaffold`
   - Local-only Sonnet proposal scaffolding (default no external API calls).
5. `feature/flyio-hosting-plan-only`
   - Deployment plan docs only; no deployment actions.

## Model and Tool Routing
- Codex/Cursor agents:
  - Local code edits, deterministic smoke runs, scaffold hardening, docs updates.
- Claude/Poke agents:
  - Structured proposal drafting from local artifacts, queue triage, schema validation suggestions.
- Use shell/python scripts for deterministic local verification only; no credentialed services.
- Treat networked model providers as opt-in and disabled by default in scaffold branches.

## What Poke Can Handle Safely
- Read local watcher outputs (`candidate_events.jsonl`, `needs_human_events.jsonl`, `poke_bridge_queue.jsonl`).
- Produce structured proposal records and handoff artifacts for human review.
- Run/assist deterministic local validation harnesses and fixture checks.
- Keep outputs as `approved=False` execution drafts unless a human promotes them.

## What Must Stay Human Reviewed
- Any change that could affect order placement semantics.
- Any change to risk parameters/circuit-breaker thresholds that alters runtime behavior.
- Any scheduler enablement that can auto-start jobs outside explicit CLI invocation.
- Any credential wiring, broker API enablement, or deployment configuration.
- Any merge/push decision to shared branches.

## Stabilization Snapshot (2026-05-31)
- Main preflight gates passed.
- Full post-merge smoke suite passed for all present scripts.
- Optional `smoke_local_pipeline_scaffold.py` not present.
- Generated `__pycache__/` directories may appear during smokes and should be cleaned before commit.
