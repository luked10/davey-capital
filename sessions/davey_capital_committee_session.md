# davey capital committee

Date: Thursday, May 14, 2026
Session: Davey Capital Committee preset scaffold
Objective: document the new multi-agent debate structure that powers the vibe-trading preset

## Committee structure

- Bull Advocate: builds the strongest bullish case for the target and highlights upside drivers.
- Bear Advocate: builds the strongest bearish case and surfaces invalidation signals and downside risk.
- Risk Officer: reviews both sides independently and adds sizing, stop, hedge, and blind-spot guidance.
- Portfolio Manager: makes the final decision and converts the debate into an executable plan.

## Workflow

1. Bull Advocate and Bear Advocate run in parallel.
2. Risk Officer reviews both memos and produces a risk recommendation.
3. Portfolio Manager synthesizes the full debate and makes the final call.

## Variables

- target
- market

## Notes

- This session file is the source-of-truth note for the new preset structure.
- The YAML preset lives at vibe-trading/agent/src/swarm/presets/davey_capital_committee.yaml.
- The structure is intentionally committee-shaped so future runs can reuse the same debate order and role boundaries.
