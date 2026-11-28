"""Official daily schema, independent day coverage and information availability."""

import numpy as np
import pandas as pd
import pytest

from ficc.data.futures import asof_features, daily_features, parse_daily


def contracts():
    dates = pd.bdate_range("2021-01-01", periods=40)
    rows = [
        dict(
            date=d,
            symbol=s,
            variety="T",
            volume=100 + i,
            open_interest=300 + i,
            settle=price + 0.01 * i,
        )
        for i, d in enumerate(dates)
        for s, price in [("T2103", 100), ("T2106", 99)]
    ]
    return pd.DataFrame(rows), dates.strftime("%Y-%m-%d").tolist()


def test_official_schema_and_aggregate_rows():
    content = "合约代码,成交量,持仓量,今结算,今收盘,前结算,Delta,额外\nT2103 ,30,40,100,100,99,--,--\n小计,30,40,,,,,\n".encode(
        "gbk"
    )
    frame = parse_daily(content, "20210104")
    assert len(frame) == 1 and frame.iloc[0].symbol == "T2103"
    assert frame.iloc[0].volume == 30
    with pytest.raises(ValueError):
        parse_daily(content.replace(b",30,40,", b",-1,40,"), "20210104")


def test_missing_whole_session_and_future_perturbation():
    raw, sessions = contracts()
    original = daily_features(raw, sessions)
    changed = raw.copy()
    changed.loc[
        changed.date > pd.Timestamp(sessions[30]), ["volume", "open_interest", "settle"]
    ] *= 4
    other = daily_features(changed, sessions)
    a, used = asof_features(original, [sessions[30]])
    b, _ = asof_features(other, [sessions[30]])
    np.testing.assert_array_equal(a, b)
    assert used == [sessions[30]]
    delayed, used_delayed = asof_features(original, [sessions[30]], lag_sessions=1)
    previous, _ = asof_features(original, [sessions[29]])
    np.testing.assert_array_equal(delayed, previous)
    assert used_delayed == [sessions[29]]
    with pytest.raises(ValueError, match="negative"):
        asof_features(original, [sessions[30]], lag_sessions=-1)
    late = original.copy()
    late.loc[sessions[30], "available_at"] += pd.Timedelta(days=1)
    with pytest.raises(ValueError, match="late"):
        asof_features(late, [sessions[30]])
    assert original.iloc[30].available_at == pd.Timestamp(
        sessions[30], tz="Asia/Shanghai"
    ) + pd.Timedelta(days=1)
    with pytest.raises(ValueError, match="calendar"):
        daily_features(raw.loc[raw.date != pd.Timestamp(sessions[12])], sessions)
    with pytest.raises(ValueError, match="warmup"):
        asof_features(original, [sessions[0]])
