#!/usr/bin/env python
"""Collect NYC condo-board liens (ACRIS LOCC/TOLCC) into liens/liens_nyc.jsonl.

    ./venv/bin/python -m hoaspy.collect.liens.get_nyc_liens
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy.collect.liens import records_acris
from hoaspy import ROOT
OUT = ROOT / "liens" / "liens_nyc.jsonl"

USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    started = time.time()

    records = records_acris.fetch(session)
    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        r["retrieved_at"] = now

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT)

    (ROOT / "liens" / "sources_nyc.json").write_text(json.dumps({
        "retrieved_at": now,
        "source": "NYC ACRIS (City Register) via NYC Open Data",
        "page": records_acris.SOURCE_PAGE,
        "records": len(records),
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("Covers the five boroughs. LOCC = condo lien of common "
                   "charges; TOLCC = its termination. Unlike Broward, most "
                   "records carry a parcel id and street address."),
    }, indent=2))

    by_year = Counter(r["year"] for r in records)
    by_boro = Counter(r["county"] for r in records)
    print(f"\n{len(records):,} NYC lien-family records -> {OUT}")
    print("By borough:", dict(by_boro.most_common()))
    years = sorted(by_year)
    if years:
        print(f"Years {years[0]}–{years[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
