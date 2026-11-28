"""采集限定历史时期的央行公开公告，缓存响应与散列；不把下载失败当作零操作。"""

import argparse
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.chinamoney.com.cn"
START, END = "2018-12-01", "2025-05-30"
_LOCK = threading.Lock()
_NEXT = 0.0


def dump(path: Path, value: object) -> None:
    """原子保存采集记录。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def fetch(path: Path, url: str, params: dict | None = None) -> bytes:
    """读取已校验缓存，或以有限重试采集公开页面并记录时间。"""
    global _NEXT
    if urlparse(url).hostname not in {
        "www.chinamoney.com.cn",
        "www.chinamoney.org.cn",
        "www.pbc.gov.cn",
    }:
        raise ValueError("unregistered public source host")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if path.exists() and meta_path.exists():
        body, meta = path.read_bytes(), json.loads(meta_path.read_text())
        if (
            meta["sha256"] != hashlib.sha256(body).hexdigest()
            or meta["url"] != url
            or meta["params"] != params
        ):
            raise ValueError("cached response identity differs")
        return body
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        with _LOCK:
            wait = max(0, _NEXT - time.monotonic())
            _NEXT = max(time.monotonic(), _NEXT) + 0.2
        time.sleep(wait)
        try:
            method = requests.post if params else requests.get
            response = method(url, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            body = response.content
            if not body or (params and "records" not in response.json()):
                raise ValueError("empty or invalid official response")
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
            dump(
                meta_path,
                dict(
                    url=url,
                    params=params,
                    retrieved_at=datetime.now(UTC).isoformat(),
                    sha256=hashlib.sha256(body).hexdigest(),
                    bytes=len(body),
                ),
            )
            return body
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("unreachable")


def collect(root: Path) -> None:
    """遍历来源目录，在业务解析前保留全部限定期间公告。"""
    rows, pages = [], []
    for page in range(1, 150):
        params = dict(pageNo=page, pageSize=50, channelId="2845")
        path = root / "index" / f"omo-{page}.json"
        raw = fetch(path, BASE + "/ags/ms/cm-s-notice-query/contents", params)
        data = json.loads(raw)
        records = data["records"]
        if not records:
            raise ValueError("unexpected empty source index page")
        dates = [r["releaseDate"] for r in records]
        pages.append(
            dict(
                page=page,
                sha256=hashlib.sha256(raw).hexdigest(),
                records=len(records),
                first=max(dates),
                last=min(dates),
                source_total=data["data"]["total"],
            )
        )
        rows.extend(r for r in records if START <= r["releaseDate"] <= END)
        if max(dates) < START:
            break
    else:
        raise ValueError("source index did not reach the frozen start date")
    ids = [r["contentId"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ID across source index pages; refresh index snapshot")
    dump(root / "index_manifest.json", dict(start=START, end=END, rows=rows, pages=pages))
    print(f"indexed {len(rows)} historical announcements across {len(pages)} pages", flush=True)

    def article(row):
        url = urljoin(BASE, row["url"] or row["draftPath"])
        try:
            fetch(root / "articles" / (row["contentId"] + ".html"), url)
            return dict(content_id=row["contentId"], status="downloaded")
        except (requests.RequestException, ValueError) as exc:
            return dict(content_id=row["contentId"], status="failed", error=type(exc).__name__)

    completed = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, record in enumerate(pool.map(article, rows), 1):
            completed.append(record)
            if i % 100 == 0 or i == len(rows):
                dump(
                    root / "download_status.json",
                    dict(expected=len(rows), complete=i, results=completed),
                )
                print(
                    f"downloaded {i}/{len(rows)}; failures={sum(r['status'] == 'failed' for r in completed)}",
                    flush=True,
                )
    if any(r["status"] == "failed" for r in completed):
        raise ValueError("historical announcement download incomplete")
    collect_primary(root, rows)
    calendar = json.loads(Path("config/baselines/funding_sessions.json").read_text())
    for rule in calendar["annual_notices"] + calendar["amendments"] + calendar["evidence"]:
        path = root / rule.get("cache", "calendar/" + rule["url"].split("/")[-1])
        raw = fetch(path, rule["url"])
        if hashlib.sha256(raw).hexdigest() != rule["sha256"]:
            raise ValueError(
                "calendar source changed; review a new snapshot, do not silently update"
            )


def notice_key(title: str) -> tuple[str, str]:
    """跨来源以公告年份、编号匹配，不依赖标点或转载标题。"""
    values = re.findall(r"\d+", title)
    if len(values) != 2:
        raise ValueError("unrecognized daily notice identity")
    return tuple(values)


def collect_primary(root: Path, cfets_rows: list[dict]) -> None:
    """独立遍历央行主目录并补齐转载目录未收录的公告。"""
    base = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/"
    rows, pages = [], []
    for page in range(1, 250):
        body = fetch(root / "pbc-index" / f"{page}.html", base + f"17081-{page}.html")
        page_rows = []
        for a in BeautifulSoup(body, "html.parser").select("a[istitle]"):
            day = a.parent.parent.get_text(" ", strip=True)[-10:]
            date_value = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
            page_rows.append(
                dict(title=a.get_text(strip=True), url=urljoin(base, a["href"]), date=date_value)
            )
        if not page_rows:
            raise ValueError("empty primary index page")
        pages.append(page)
        rows.extend(r for r in page_rows if START <= r["date"] <= END)
        if max(r["date"] for r in page_rows) < START:
            break
    else:
        raise ValueError("primary index does not reach frozen start")
    keys = [notice_key(r["title"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate primary notice; refresh source snapshot")
    dump(root / "pbc-index/manifest.json", dict(start=START, end=END, pages=pages, rows=rows))
    cfets_keys = {
        notice_key(r["title"]) for r in cfets_rows if "公开市场业务交易公告" in r["title"]
    }
    if not cfets_keys <= set(keys):
        raise ValueError("daily notice absent from primary index")
    supplements = []
    for row in rows:
        if notice_key(row["title"]) in cfets_keys:
            continue
        name = row["date"] + "-" + row["url"].split("/")[-2] + ".html"
        fetch(root / "pbc-supplement" / name, row["url"])
        supplements.append(dict(row, filename=name))
    dump(root / "pbc-supplement/index.json", supplements)
    print(f"primary index: {len(rows)} notices; {len(supplements)} supplements", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("datasets/pbc-omo-v1"))
    collect(p.parse_args().root)
