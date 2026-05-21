from __future__ import annotations

import unittest

import pandas as pd

from backtest.risk_controls import apply_risk_gates


class RiskControlsTests(unittest.TestCase):
    def test_dividend_quality_gate_can_block_when_enforced(self) -> None:
        idx = pd.date_range("2024-01-01", periods=4, freq="QE")
        data_map = {
            "AAA": pd.DataFrame(
                {
                    "close": [10, 11, 12, 13],
                    "payout_ratio": [0.92, 0.95, 0.91, 0.93],
                    "dividend_per_share": [1.0, 1.1, 1.2, 1.3],
                    "free_cash_flow": [80, 80, 80, 80],
                    "dividends_paid": [100, 100, 100, 100],
                    "debt_to_equity": [3.5, 3.4, 3.6, 3.7],
                },
                index=idx,
            )
        }
        signal_map = {"AAA": pd.Series([1.0, 1.0, 1.0, 1.0], index=idx)}
        config = {
            "risk_gates": {
                "dividend_quality": {
                    "enabled": True,
                    "enforce": True,
                    "payout_ratio_ceiling": 0.8,
                    "min_checks_passed": 3,
                }
            }
        }

        updated, report = apply_risk_gates(config, data_map, signal_map)

        self.assertIn("AAA", report["dividend_quality"]["blocked_symbols"])
        self.assertTrue((updated["AAA"] == 0.0).all())

    def test_stress_test_monitoring_does_not_block_when_not_enforced(self) -> None:
        idx = pd.date_range("2024-01-01", periods=3, freq="QE")
        data_map = {
            "BBB": pd.DataFrame(
                {
                    "close": [50, 48, 47],
                    "dividend_yield": [0.08, 0.08, 0.08],
                    "debt_to_equity": [3.0, 3.0, 3.0],
                    "revenue_growth": [-0.15, -0.2, -0.25],
                },
                index=idx,
            )
        }
        signal_map = {"BBB": pd.Series([1.0, 1.0, 1.0], index=idx)}
        config = {
            "risk_gates": {
                "stress_test": {
                    "enabled": True,
                    "enforce": False,
                    "max_aggregate_stress_loss_pct": -0.1,
                }
            }
        }

        updated, report = apply_risk_gates(config, data_map, signal_map)

        self.assertIn("BBB", report["stress_test"]["blocked_symbols"])
        self.assertTrue((updated["BBB"] == 1.0).all())


if __name__ == "__main__":
    unittest.main()
