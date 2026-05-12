# Proposals for quant-hub-bridge

## Executive summary
The repo is already shaped like a bridge layer between signal generation and execution, but the seam is still mostly implicit:
- vibe-trading produces signals, reports, and run artifacts.
- autohedge runs an agentic execution pipeline plus interactive shells.
- nova-alpha is documentation/reporting only today, but it clearly wants to be a scheduled consumer of portfolio snapshots.

The highest-value work is therefore not a broad rewrite. It is to standardize the contract between modules, remove the hottest Python loops in the data paths, and replace the remaining polling loops with explicit runners/schedulers.

## What I reviewed
High-signal files and the issues they surface:
- vibe-trading/agent/src/tools/trade_journal_parsers.py: repeated row-by-row DataFrame parsing via iterrows.
- vibe-trading/agent/src/tools/factor_analysis_tool.py: daily IC and quantile group backtests are computed in per-date Python loops.
- vibe-trading/agent/src/shadow_account/backtester.py: equity curve loading uses iterrows to rebuild a list of tuples.
- vibe-trading/backtest/validation.py: trade reconstruction uses iterrows over filtered exit rows.
- vibe-trading/agent/cli.py: long-running interactive and dashboard loops use fixed sleep polling.
- autohedge/autohedge/cli.py: perpetual REPL creates a new AutoHedge instance for each task.
- autohedge/autohedge/main.py and workers.py: execution is agent-driven, but there is no shared job/schedule abstraction yet.
- nova-alpha/README.md and nova-alpha/daily_report.md: reporting is currently static content, not a scheduled output from live artifacts.

## Prioritized roadmap

### P0: Define the bridge contract between signal generation and execution
This should happen first because it reduces integration risk for every later optimization.

1. Create a shared artifact/schema layer for:
   - signal payloads
   - trade records
   - risk summaries
   - execution intents
   - run metadata

2. Make vibe-trading export a single canonical payload for downstream consumers instead of ad hoc CSV/JSON shapes.
3. Make autohedge ingest that payload directly instead of re-parsing files or reconstructing state from CLI prompts.
4. Standardize output locations and naming for run artifacts so nova-alpha can consume them as a reporting source.

Best integration points:
- vibe-trading/agent/src/session/events.py for progress and status events.
- vibe-trading/agent/src/tools/* for data preparation.
- autohedge/autohedge/main.py for the top-level execution entrypoint.

Suggested outcome: one contract that can be used by CLI, API, and future scheduled jobs.

### P1: Vectorize the highest-traffic pandas loops
These are the best candidates because they are repeated across many rows or dates and are easy to benchmark.

1. vibe-trading/agent/src/tools/trade_journal_parsers.py
   - parse_tonghuashun, parse_eastmoney, parse_futu, and parse_generic all loop with iterrows.
   - Replace row-by-row extraction with itertuples or column-wise vectorized transforms where possible.
   - Normalize date/time, quantity, price, amount, and fee columns in bulk before building TradeRecord objects.
   - For export formats with stable schemas, precompute the column map once and operate on numpy arrays.

2. vibe-trading/agent/src/tools/factor_analysis_tool.py
   - _compute_ic_series loops once per date and then does per-date alignment/correlation.
   - _compute_group_equity loops once per date and once per quantile bucket.
   - This is the best place for a larger numpy refactor:
     - use aligned matrices and boolean masks
     - use argsort / rank-based vectorization where possible
     - use np.nan-aware aggregations for group returns
   - The current implementation is readable, but it is the most obvious CPU hotspot in the repo.

3. vibe-trading/agent/src/shadow_account/backtester.py
   - _load_equity_curve reconstructs a list of tuples with iterrows.
   - Convert the selected columns to a vectorized list/array path or a direct records export.
   - This is a small win, but it is on a code path that runs every time the shadow backtester loads artifacts.

4. vibe-trading/backtest/validation.py
   - _load_trades builds TradeRecord objects from filtered exit rows one row at a time.
   - If the trade CSV schema is stable, move the extraction to vectorized column handling and only instantiate objects at the very end.
   - The main goal here is less pandas overhead and less repeated scalar conversion.

5. Secondary candidates
   - vibe-trading/backtest/metrics.py is already mostly aggregation-based and does not look like a major pandas-loop hotspot.
   - autohedge/autohedge/tools/yahoo_api.py and polygon_api.py are network-bound; optimize request batching and caching before worrying about local vectorization.

Expected result: lower latency on daily backtests and factor workflows, plus cleaner benchmarkable performance regressions.

### P1: Replace fixed polling with explicit schedulers and event-driven refresh
There are several loops that should be made more deliberate.

1. vibe-trading/agent/cli.py
   - The interactive run loop and the swarm dashboard loop both rely on fixed sleep polling.
   - Replace the dashboard refresh loop with an event-driven update path, or at least a configurable refresh interval that can be tuned per mode.
   - The session timer thread should be collapsed into a shared timer utility so prompt UI, CLI, and API all use the same cadence logic.
   - The `time.sleep(0.25)` loop in the swarm dashboard is a good target for a shared runner abstraction.

2. autohedge/autohedge/cli.py
   - The REPL creates a new AutoHedge object per task.
   - That is convenient, but it makes warm-up, caching, and per-session state impossible.
   - Introduce a long-lived engine instance or a job runner so repeated tasks can reuse loaded state, cached tools, and connection setup.
   - Separate prompt handling from execution orchestration.

3. vibe-trading/agent/src/session/events.py
   - The SSE event bus already has replay and heartbeat semantics.
   - It should be the transport for status, progress, and cancellation events instead of ad hoc console polling.
   - Make the heartbeat interval configurable and keep the queue-drain/replay logic as the canonical live-update layer.

4. Add a small scheduler layer
   - One abstraction for recurring report generation.
   - One abstraction for one-shot runs.
   - One abstraction for live execution / cancellation.
   - This would let nova-alpha reports, backtests, and execution tasks all share the same lifecycle semantics.

### P2: Turn nova-alpha into a scheduled consumer of artifacts
nova-alpha currently reads like a reporting surface, not an executable module. That is fine, but it is the perfect place to plug in after P0/P1.

1. Generate daily_report.md from structured portfolio snapshots instead of hand-maintained text.
2. Feed it from the same canonical run artifact schema used by vibe-trading and autohedge.
3. If the repo wants a public-facing status page, keep the markdown as a template and add a generator that fills in positions, performance, and strategy notes.
4. Schedule that generation off the new scheduler layer rather than ad hoc manual updates.

### P2: Add tests and benchmarks around the new seams
The refactor should be measured, not just cleaner.

1. Add regression tests for:
   - parser correctness across trade export formats
   - factor-analysis equivalence before/after vectorization
   - trade validation loading
   - CLI cancellation and completion behavior
   - event bus replay / heartbeat behavior

2. Add microbenchmarks for:
   - factor_analysis_tool daily IC and quantile grouping
   - trade_journal_parsers parsing throughput
   - validation trade loading

3. Add schema tests so bridge payloads do not drift.

## Recommended implementation order
1. Introduce the shared bridge schema and artifact contract.
2. Vectorize factor_analysis_tool and trade_journal_parsers.
3. Replace polling loops in the CLI paths with a reusable runner/scheduler.
4. Reuse a persistent AutoHedge engine instance in the REPL.
5. Wire nova-alpha daily reporting to structured outputs.
6. Add benchmarks and regression tests after each step.

## Short version
If this were being executed in phases, I would start with:
- shared contract first
- factor-analysis vectorization second
- trade-parser/vector-loader cleanup third
- scheduler cleanup fourth
- nova-alpha reporting automation last

That sequence gives the highest performance payoff without breaking the bridge between signal generation and execution.
