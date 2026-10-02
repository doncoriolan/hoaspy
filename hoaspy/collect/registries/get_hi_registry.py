#!/usr/bin/env python
"""Hawaii registered condominium associations (AOUOs) from the DCCA Real
Estate Branch contact list PDF.

    ./venv/bin/python -m hoaspy.collect.registries.get_hi_registry

Merges into records/state_registries.jsonl (state HI rows replaced).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import requests

from hoaspy.collect.registries.get_states import REGS, write_merged, update_coverage

from hoaspy import ROOT
PDF = ROOT / ".cache" / "hi_aouo.pdf"
PAGE = "https://cca.hawaii.gov/reb/condo_ed/"
URL = "https://cca.hawaii.gov/wp-content/uploads/2026/04/AOUO-Contact-List-4.29.26.pdf"

log = logging.getLogger("hi")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    if not PDF.exists():
        resp = requests.get(URL, timeout=120, headers={
            "User-Agent": "macos:hoa-public-records:0.1 (academic research)"})
        resp.raise_for_status()
        PDF.write_bytes(resp.content)

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    with pdfplumber.open(PDF) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for r in table:
                # PDF cells wrap: collapse all internal whitespace/newlines.
                cells = [" ".join((c or "").split()) for c in r]
                if len(cells) < 9 or not cells[0].isdigit() or not cells[1]:
                    continue
                officer = " ".join(x for x in (cells[4], cells[3]) if x)
                rows.append({
                    "state": "HI",
                    "source": "hi-dcca-aouo-registry",
                    "source_url": PAGE,
                    "record_id": cells[0],
                    "name": cells[1],
                    "status": "registered AOUO",
                    "address": "",
                    "city": "",     # listed address is the officer's, not the property's
                    "county": "",
                    "zip": "",
                    "manager_name": cells[9] if len(cells) > 9 else "",
                    "registered_agent": (f"{cells[2].title()}: {officer.title()}"
                                         if officer else ""),
                    "retrieved_at": now,
                })

    total = write_merged(REGS, rows, {"HI"})
    update_coverage("HI", "hoa_registry", {
        "source": "DCCA Real Estate Branch AOUO contact list (PDF)",
        "records": len(rows), "url": PAGE})
    print(f"{len(rows)} Hawaii AOUOs parsed; state_registries.jsonl now {total} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
