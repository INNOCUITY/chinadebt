"""Download official monthly ZIPs, preserve hashes, and validate contract/day coverage."""

import argparse
import hashlib
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import requests

from ficc.data.futures import daily_features, parse_daily


def fetch_month(item):
    month, dest = item
    url = f"http://www.cffex.com.cn/sj/historysj/{month}/zip/{month}.zip"
    file = dest / f"{month}.zip"
    if file.exists():
        body = file.read_bytes()
    else:
        for attempt in range(3):
            try:
                response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
                response.raise_for_status()
                body = response.content
                with ZipFile(io.BytesIO(body)) as archive:
                    if not archive.namelist():
                        raise ValueError("empty monthly ZIP")
                file.write_bytes(body)
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
    frames = []
    with ZipFile(io.BytesIO(body)) as archive:
        for name in sorted(archive.namelist()):
            match = re.fullmatch(r"(\d{8})_1.csv", name)
            if not match:
                continue
            day = match[1]
            if not day.startswith(month):
                raise ValueError("ZIP date outside requested month")
            frames.append(parse_daily(archive.read(name), day))
    if not frames:
        raise ValueError("no official daily CSVs")
    frame = pd.concat(frames, ignore_index=True)
    print(f"downloaded {month}: {frame.date.nunique()} dates", flush=True)
    return frame, dict(
        month=month,
        url=url,
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
        dates=int(frame.date.nunique()),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("datasets/cffex-eod-v1"))
    p.add_argument("--calendar", type=Path, default=Path("config/baselines/futures_sessions.json"))
    a = p.parse_args()
    a.root.mkdir(parents=True, exist_ok=True)
    plan = json.loads(a.calendar.read_text())
    sessions = plan["sessions"]
    months = pd.period_range(sessions[0], sessions[-1], freq="M").strftime("%Y%m")
    with ThreadPoolExecutor(max_workers=3) as pool:
        pieces = list(pool.map(fetch_month, [(m, a.root) for m in months]))
    contracts = pd.concat([f for f, _ in pieces], ignore_index=True)
    contracts = contracts.loc[
        contracts.date.between(pd.Timestamp(sessions[0]), pd.Timestamp(sessions[-1]))
    ].sort_values(["date", "symbol"])
    features = daily_features(contracts, sessions)
    contracts.to_parquet(a.root / "contracts.parquet", index=False)
    features.to_parquet(a.root / "features.parquet")
    manifest = dict(
        retrieved_at=datetime.now(UTC).isoformat(),
        calendar_sha256=hashlib.sha256(a.calendar.read_bytes()).hexdigest(),
        months=[m for _, m in pieces],
        rows=len(contracts),
        dates=len(features),
        first=sessions[0],
        last=sessions[-1],
        contracts_sha256=hashlib.sha256((a.root / "contracts.parquet").read_bytes()).hexdigest(),
        features_sha256=hashlib.sha256((a.root / "features.parquet").read_bytes()).hexdigest(),
        variety_dates=contracts.groupby("variety").date.nunique().to_dict(),
        publication="Daily information is released after trading. Next calendar day 00:00 Asia/Shanghai is a conservative modeling assumption, not an observed historical timestamp.",
        calendar_basis=plan["basis"],
    )
    (a.root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print({k: manifest[k] for k in ["rows", "dates", "first", "last", "variety_dates"]})


if __name__ == "__main__":
    main()
