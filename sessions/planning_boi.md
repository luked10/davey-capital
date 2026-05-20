# planning boi

Date: Tuesday, May 12, 2026
Session: paper-trading planning for Davey Capital
Objective: coordinate Signal Boi and Execution Boi on entry criteria and expected first paper fills for the current high-alpha watchlist

## Current watchlist
Biotech:
- CMPS
- GHRS
- MNMD
- DFTX
- VRDN
- ATRA
- DNL

Quantum:
- IONQ
- RGTI
- QBTS
- QUBT
- INFQ

Photonics:
- LITE
- COHR
- AAOI
- FN
- MTSI
- CIEN

## Entry criteria by bucket
Biotech gate:
- requires a clear catalyst or regulatory headline from the repo research stack
- requires liquidity confirmation and a clean risk pass
- avoid forcing entries into dead tape
- only then move to Signal Boi for paper routing

Quantum gate:
- requires partner validation, roadmap news, or a clear trend reclaim
- avoid forcing entries into dead tape
- only send through Execution Boi after Risk Boi approval

Photonics gate:
- requires fresh AI-infrastructure / optical-interconnect catalyst confirmation
- requires trend/breadth confirmation rather than one-day spikes
- avoid forcing entries into dead tape
- prefer the names that remain aligned with the optical-interconnect / AI-backbone thesis
- only route once the setup remains strong after a sanity check

## Coordination result
- Signal Boi should only emit orders after the above gates are met
- Execution Boi should route approved signals directly to the paper broker
- no live paper order packet is currently queued in this environment

## Expected first fills
- not immediate
- highest-priority paper-fill candidates to monitor: AAOI, CMPS/MNMD, IONQ/INFQ
- first fills should hit on the next qualifying signal cycle after a catalyst or trend-confirmation setup passes Risk Boi
- practical expectation: earliest next market session if a vetted signal appears before then; otherwise the first qualifying intraday or next-day setup after the next catalyst

## Blocker
- there is currently no executed signal packet waiting in the paper broker queue, so nothing can fill until Signal Boi produces an approved entry
- no signal is approved for execution yet; keep this as monitoring/watchlist mode until sizing approval is added
