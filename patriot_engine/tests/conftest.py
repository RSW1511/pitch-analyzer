import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest


@pytest.fixture
def rcc_bid():
    """Path to a local bid workspace holding real company/government files (never committed).
    Set PATRIOT_RCC_BID to run tests/test_rcc_regression.py, which is also git-ignored."""
    p = os.environ.get("PATRIOT_RCC_BID")
    if not p or not Path(p, "bid.yaml").exists():
        pytest.skip("PATRIOT_RCC_BID not set: real-bid regression skipped")
    return Path(p)
