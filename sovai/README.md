# Sovai Open Investment Datasets

Source: https://github.com/sovai-research/open-investment-datasets

This folder mirrors the dataset reference material used by the agents for research context and signal filtering workflows.

## Dataset reference highlights

- news_sentiment
- price_breakout
- insider_flow_prediction
- institutional_trading
- lobbying_data
- short_selling
- wikipedia_views
- pharma_clinical_trials
- factor_signals
- financial_ratios
- government_contracts
- corp_risks
- risks
- cfpb_complaints
- risk_indicators
- traffic_agencies
- earnings_surprise
- bankruptcy

## Practical use in the bridge

- Use these references as candidate filters before routing signals into execution.
- Prefer datasets that match the asset class, event type, and timeliness of the signal.
- Treat the live/realtime datasets as higher-priority gating inputs when screening trades.
- Keep the evaluation logic separate from execution so the signal handoff remains auditable.

## Loading pattern

The upstream project documents Hugging Face dataset loading with the `datasets` library, for example:

```python
from datasets import load_dataset
df = load_dataset("sovai/news_sentiment", split="train").to_pandas()
```

## Notes

The upstream project states that free access is available with a brief delay and that subscribers receive realtime data.
