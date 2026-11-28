"""Official contract-level daily quotes and causal, end-of-day futures features."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd

FIELDS = {
    "合约代码": "symbol",
    "成交量": "volume",
    "持仓量": "open_interest",
    "今结算": "settle",
    "今收盘": "close",
    "前结算": "pre_settle",
}


def parse_daily(content: bytes, day: str) -> pd.DataFrame:
    frame = pd.read_csv(io.BytesIO(content), encoding="gbk")
    frame.columns = frame.columns.str.strip()
    if not set(FIELDS).issubset(frame.columns):
        raise ValueError("official daily CSV schema missing required fields")
    frame = frame.rename(columns=FIELDS)
    frame["symbol"] = frame.symbol.astype(str).str.strip()
    frame = frame.loc[
        frame.symbol.str.fullmatch(r"(?:T|TF|TS|TL)\d{4}"), list(FIELDS.values())
    ].copy()
    if frame.empty or frame.symbol.duplicated().any():
        raise ValueError("no unique government futures contracts")
    for field in list(FIELDS.values())[1:]:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    if (
        not np.isfinite(frame[["volume", "open_interest", "settle"]]).all().all()
        or (frame[["volume", "open_interest"]] < 0).any().any()
        or (frame.settle <= 0).any()
    ):
        raise ValueError("invalid volume, inventory or settlement")
    frame["date"] = pd.Timestamp(day)
    frame["variety"] = frame.symbol.str.extract(r"^([A-Z]+)")[0]
    return frame


def daily_features(contracts: pd.DataFrame, sessions: list[str]) -> pd.DataFrame:
    """Require independently frozen sessions; deleting a complete day must fail."""
    expected = pd.DatetimeIndex(sessions)
    if expected.has_duplicates or not expected.is_monotonic_increasing:
        raise ValueError("invalid frozen futures sessions")
    d = contracts.loc[contracts.variety.eq("T")].copy()
    d["date"] = pd.to_datetime(d.date)
    actual = pd.DatetimeIndex(sorted(d.date.unique()))
    if not expected.equals(actual) or d.duplicated(["date", "symbol"]).any():
        raise ValueError("futures calendar or contract coverage mismatch")
    rows = []
    for day, frame in d.groupby("date", sort=True):
        active = frame.loc[(frame.open_interest > 0) & (frame.settle > 0)].sort_values("symbol")
        if len(active) < 2:
            raise ValueError("fewer than two active T maturities")
        near, far = active.iloc[0], active.iloc[1]
        rows.append(
            dict(
                date=day,
                volume=float(frame.volume.sum()),
                oi=float(frame.open_interest.sum()),
                spread=float(near.settle - far.settle),
                near=near.symbol,
                far=far.symbol,
            )
        )
    out = pd.DataFrame(rows).set_index("date")
    if (out[["volume", "oi"]] <= 0).any().any():
        raise ValueError("aggregate futures activity must be positive")
    # Every term uses at most the current official end-of-day report.
    out["log_volume_ratio20"] = np.log(out.volume / out.volume.rolling(20, min_periods=20).median())
    out["oi_change5"] = out.oi / out.oi.shift(5) - 1
    out["log_volume_oi"] = np.log(out.volume / out.oi)
    out["near_next_settle_spread"] = out.spread
    out["roll_changed"] = out.near.ne(out.near.shift())
    out["available_at"] = out.index.tz_localize("Asia/Shanghai") + pd.Timedelta(days=1)
    return out


FEATURE_NAMES = ["log_volume_ratio20", "oi_change5", "log_volume_oi", "near_next_settle_spread"]


def asof_features(
    frame: pd.DataFrame, cutoffs, lag_sessions: int = 0
) -> tuple[np.ndarray, list[str]]:
    """Use reports dated no later than the previous-session cutoff, never same-decision-day EOD."""
    if lag_sessions < 0:
        raise ValueError("negative information lag")
    result = []
    used = []
    for cutoff in pd.DatetimeIndex(cutoffs):
        eligible = frame.loc[frame.index <= cutoff]
        if len(eligible) <= lag_sessions:
            raise ValueError("no report before information cutoff")
        if lag_sessions:
            eligible = eligible.iloc[:-lag_sessions]
        row = eligible.iloc[-1]
        deadline = (cutoff + pd.Timedelta(days=1)).tz_localize("Asia/Shanghai")
        if row.available_at > deadline or (cutoff - eligible.index[-1]).days > 15:
            raise ValueError("late or stale futures report")
        values = row[FEATURE_NAMES].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("incomplete futures indicator warmup")
        result.append(values)
        used.append(str(eligible.index[-1].date()))
    return np.array(result), used
