"""Public CI contract suite: no licensed snapshots, label caches or GPU required."""

import sys
from pathlib import Path

# Optional terminal completion is not used; some local Anaconda readline builds crash.
sys.modules["readline"] = None

import pytest  # noqa: E402
import torch  # noqa: E402, F401 - required: do not silently skip PPO tests in public CI

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    entries = (ROOT / "config/acceptance/public_tests.txt").read_text().splitlines()
    raise SystemExit(pytest.main([s for s in entries if s and not s.startswith("#")]))
