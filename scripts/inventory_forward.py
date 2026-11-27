"""Pre-registered confirmation dates and artifact identity for the inventory experiment."""

import hashlib
from datetime import date

import numpy as np
import pandas as pd
from evaluate_rl import sha

from ficc.data.schema import CANONICAL_TENORS
from ficc.research.protocol import ExperimentProtocol
from ficc.utils.calendar import TradingCalendar


def digest_lines(lines):
    return hashlib.sha256("".join(f"{line}\n" for line in lines).encode()).hexdigest()


def runtime_digest(root):
    """Cover replay, policy, configuration and confirmation code, independent of checkout path."""
    files = [
        *root.glob("src/ficc/**/*.py"),
        *root.glob("scripts/**/*.py"),
        root / "config/config.yaml",
        root / "config/universe.yaml",
        root / "config/experiments/ppo_evaluation.json",
        root / "pyproject.toml",
    ]
    return digest_lines(f"{p.relative_to(root)} {sha(p)}" for p in sorted(files))


def scheduled_window(plan, cfg, anchor):
    # The pre-cutoff count fixes the phase of the original grid from its historical anchor.
    if plan["anchor"] != str(anchor):
        raise ValueError("calendar anchor differs")
    suffix = [date.fromisoformat(s) for s in plan["sessions"]]
    cutoff = date.fromisoformat(plan["history_end"])
    first = date.fromisoformat(cfg["confirmation_start"])
    if (
        not suffix
        or suffix != sorted(set(suffix))
        or suffix[0] <= cutoff
        or suffix[-1] > date.fromisoformat(plan["verified_through"])
        or first <= cutoff
    ):
        raise ValueError("invalid independent calendar")
    step = ExperimentProtocol().decision_step_days
    grid = [
        d
        for i, d in enumerate(suffix, start=plan["history_sessions"])
        if i % step == 0 and d >= first
    ]
    n = cfg["confirmation_intervals"]
    # Settlement can use the session after the last mark without using its price.
    if len(grid) <= n or grid[n] >= suffix[-1]:
        raise ValueError(
            "independently verified calendar does not cover the confirmation and settlement"
        )
    return grid[0], grid[n], suffix


def checked_calendar(market, anchor, end, plan, suffix):
    cutoff = date.fromisoformat(plan["history_end"])
    historical = [d for d in market.panel.index.date if anchor <= d <= cutoff]
    if (
        len(historical) != plan["history_sessions"]
        or digest_lines(historical) != plan["history_sha256"]
    ):
        raise ValueError("historical calendar differs from frozen grid")
    expected = [d for d in suffix if d <= end]
    actual = [d for d in market.panel.index.date if cutoff < d <= end]
    if actual != expected:
        delta = sorted(set(actual).symmetric_difference(expected))
        raise ValueError(f"forward curve session mismatch: {list(map(str, delta[:3]))}")
    dates = pd.DatetimeIndex(expected)
    curves = market.panel.reindex(index=dates, columns=CANONICAL_TENORS)
    funding = market.funding.reindex(dates)
    if not np.isfinite(curves.to_numpy()).all() or not np.isfinite(funding.to_numpy()).all():
        raise ValueError("forward curve or funding observations incomplete")
    history = [d for d in market.panel.index.date if d <= cutoff]
    return TradingCalendar.from_dates([*history, *suffix])
