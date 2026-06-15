#!/usr/bin/env python3
"""Dedicated smoke test for symbol normalizer logic."""

from __future__ import annotations
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.bridge_contract import symbol_normalize

# Capture warnings to verify they are logged
class WarningCounter(logging.Handler):
    def __init__(self):
        super().__init__()
        self.warnings = []

    def emit(self, record):
        if record.levelno == logging.WARNING:
            self.warnings.append(record.getMessage())

def main() -> None:
    # Set up warning handler
    logger = logging.getLogger()
    handler = WarningCounter()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    # Test cases
    cases = [
        # (input_symbol, expected_output, expect_warning)
        ("SOL-USD", "SOL/USD", False),
        ("BTC-USD", "BTC/USD", False),
        ("ETH-USD", "ETH/USD", False),
        ("DOGE-USD", "DOGE/USD", False),
        ("AVAX-USD", "AVAX/USD", False),
        ("MATIC-USD", "MATIC/USD", False),
        ("NVDA", "NVDA", False),
        ("MU", "MU", False),
        ("SOL/USD", "SOL/USD", False),
        ("BTC/USD", "BTC/USD", False),
        ("AAPL", "AAPL", True),
        ("UNKNOWN-USD", "UNKNOWN-USD", True),
    ]

    for inp, expected, warn in cases:
        handler.warnings.clear()
        res = symbol_normalize(inp)
        assert res == expected, f"Failed for {inp}: got {res}, expected {expected}"
        if warn:
            assert len(handler.warnings) > 0, f"Expected warning for {inp} but got none"
        else:
            assert len(handler.warnings) == 0, f"Expected no warning for {inp} but got one: {handler.warnings}"

    print("symbol normalization smoke: ok")

if __name__ == "__main__":
    main()
