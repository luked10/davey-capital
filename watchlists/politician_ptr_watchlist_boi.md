# Politician PTR clustered-trade watchlist boi

Date: Wednesday, May 20, 2026
Purpose: House/Senate PTR filings turned into a clustered-trade watchlist with alertable conviction scores

## Source boi
- House PTR disclosures
- Senate PTR disclosures
- any mirrored disclosure page or scraper endpoint the run is pointed at

## Extraction boi
The PTR monitor should normalize each filing into:
- politician name
- chamber / office
- party / state
- ticker
- asset name
- transaction type
- direction
- amount text and estimated range
- filing date
- source URL
- confidence

## Cluster boi
Flag a name when all of the following are true:
- at least 2 filings for the same ticker within the clustering window
- filings are recent enough to belong to one conviction cluster
- the score clears the alert threshold
- buys should outrank sells for the strongest signals

## Score boi
Score should reward:
- multiple lawmakers or offices touching the same ticker
- higher disclosed dollar ranges
- fresh filing dates
- buy-heavy directionality
- higher extraction confidence

Suggested alert posture:
- score 65+ = alert candidate
- score 80+ = high-conviction clustered trade
- score below threshold = keep in watchlist only

## Operational note
This is designed to feed the existing session / alert trail rather than place orders.
The tool should emit an alert packet that can be written into sessions/politician_ptr_alert_boi.md.
