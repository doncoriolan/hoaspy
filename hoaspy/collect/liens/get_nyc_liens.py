"""NYC condominium-board liens (ACRIS LOCC and TOLCC documents).

    python -m hoaspy nyc-liens

Output: liens/liens_nyc.jsonl, one record per document, and
liens/sources_nyc.json. records_acris.py says how the three datasets are
read and joined.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter

from hoaspy.collect.liens import LIENS_DIR, records_acris
from hoaspy.lib import http
from hoaspy.lib.jsonl import now_iso, write_json, write_rows

OUTPUT_PATH = LIENS_DIR / "liens_nyc.jsonl"
SOURCES_PATH = LIENS_DIR / "sources_nyc.json"

log = logging.getLogger(__name__)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No options."""


def run(args: argparse.Namespace) -> int:
    started = time.time()
    retrieved_at = now_iso()
    records = records_acris.fetch(http.session(), retrieved_at=retrieved_at)
    write_rows(OUTPUT_PATH, (record.as_row() for record in records))
    write_json(SOURCES_PATH, {
        "retrieved_at": retrieved_at,
        "source": "NYC ACRIS (City Register) via NYC Open Data",
        "page": records_acris.SOURCE_PAGE,
        "records": len(records),
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("Covers the five boroughs. LOCC = condo lien of common charges; TOLCC = its termination. "
                   "Unlike Broward, most records carry a parcel id and street address."),
    })
    by_year = Counter(record.year for record in records)
    by_borough = Counter(record.county for record in records)
    print(f"\n{len(records):,} NYC lien-family records -> {OUTPUT_PATH}")
    print("By borough:", dict(by_borough.most_common()))
    if by_year:
        print(f"Years {min(by_year)}–{max(by_year)}")
    return 0
