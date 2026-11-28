"""采集资金工具的历史月报/结算公告；范围固定，不请求前瞻期数据。"""

import argparse
import json
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from fetch_funding_announcements import dump, fetch

ROOT = Path("datasets/funding-contract-v1")
BASE = "https://www.pbc.gov.cn"
CHANNELS = {
    "mlf_table": ("/zhengcehuobisi/125207/125213/125437/125446/125876/", "17100", "2018-11-01"),
    "mlf_news": ("/zhengcehuobisi/125207/125213/125437/125446/125873/", "17099", "2018-11-01"),
    "treasury": ("/zhengcehuobisi/125207/125213/125431/125481/", "34af3845", "2018-01-01"),
    "outright_tender": ("/zhengcehuobisi/125207/125213/125431/5492845/", "b0da893b", "2018-12-01"),
}
END = "2025-06-30"


def collect(root: Path) -> None:
    """保留目录与全量响应；限定公告日期、独立记录业务参考期间。"""
    for channel, (folder, module, start) in CHANNELS.items():
        rows, pages = [], []
        for page in range(1, 40):
            path = root / channel / "index" / f"{page}.html"
            body = fetch(path, BASE + folder + f"{module}-{page}.html")
            soup = BeautifulSoup(body, "html.parser")
            page_rows = []
            for a in soup.select("a[istitle]"):
                day = a.parent.parent.get_text(" ", strip=True)[-10:]
                page_rows.append(
                    dict(title=a.get_text(strip=True), date=day, url=urljoin(BASE, a["href"]))
                )
            if not page_rows:
                raise ValueError("empty official index page")
            pages.append(page)
            rows.extend(r for r in page_rows if start <= r["date"] <= END)
            page_meta = soup.select_one("input[name=article_paging_list_hidden]")
            if max(r["date"] for r in page_rows) < start or page >= int(page_meta["totalpage"]):
                break
        else:
            raise ValueError("source pagination incomplete")
        if len({r["url"] for r in rows}) != len(rows):
            raise ValueError("overlapping source index")
        dump(
            root / channel / "index.json",
            dict(channel=channel, start=start, end=END, pages=pages, rows=rows),
        )
        for row in rows:
            fetch(root / channel / "articles" / (row["url"].split("/")[-2] + ".html"), row["url"])
        print(f"{channel}: {len(rows)} articles; {len(pages)} index pages", flush=True)
    plan = Path("config/baselines/funding_contract_audit.json")
    if plan.exists():
        for rule in json.loads(plan.read_text())["rules"]:
            fetch(root / rule["cache"], rule["url"])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    collect(p.parse_args().root)
