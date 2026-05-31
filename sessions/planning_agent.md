# planning agent

Date: Tuesday, May 12, 2026
Session: paper-trading planning for Davey Capital
Objective: coordinate Signal Agent and Execution Agent on entry criteria and expected first paper fills for the current high-alpha watchlist

## Current watchlist
Biotech:
- CMPS
- GHRS
- ATAI
- DFTX

Quantum:
- IONQ
- RGTI
- QBTS
- QUBT

Photonics:
- LITE
- COHR
- FN
- MTSI
- AAOI

## Entry criteria by bucket
Biotech gate:
- requires a clear catalyst or regulatory headline from the repo research stack
- requires liquidity confirmation and a clean risk pass
- only then move to Signal Agent for paper routing

Quantum gate:
- requires fresh momentum or a sector catalyst
- prioritize names that can reclaim trend and remain bid after the initial headline move
- only send through Execution Agent after Risk Agent approval

Photonics gate:
- requires continued AI-infrastructure follow-through and sector breadth
- prefer the names that remain aligned with the optical-interconnect / AI-backbone thesis
- only route once the setup remains strong after a sanity check

## Coordination result
- Signal Agent should only emit orders after the above gates are met
- Execution Agent should route approved signals directly to the paper broker
- no live paper order packet is currently queued in this environment

## Expected first fills
- not immediate
- first fills should hit on the next qualifying signal cycle after a catalyst or trend-confirmation setup passes Risk Agent
- practical expectation: earliest next market session if a vetted signal appears before then; otherwise the first qualifying intraday or next-day setup after the next catalyst

## Blocker
- there is currently no executed signal packet waiting in the paper broker queue, so nothing can fill until Signal Agent produces an approved entry
