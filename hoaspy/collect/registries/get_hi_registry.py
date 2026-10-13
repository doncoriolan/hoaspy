"""Hawaii condominium associations (AOUOs) from the DCCA Real Estate
Branch contact-list PDF.

    python -m hoaspy hi-registry

The PDF is downloaded once to .cache/hi_aouo.pdf and read with pdfplumber:
one table row per association with its registration number, name, an
officer with a title and the managing agent. The address the list carries
is the officer's, not the property's, so none is kept.

Output: Hawaii's rows in records/state_registries.jsonl (replaced on each
run) and coverage.json collected["hoa_registry"] for HI.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pdfplumber
import requests

from hoaspy import ROOT
from hoaspy.collect.registries import REGISTRIES_PATH
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, replace_rows
from hoaspy.lib.records import Association, Officer

SOURCE_PAGE = "https://cca.hawaii.gov/reb/condo_ed/"
PDF_URL = "https://cca.hawaii.gov/wp-content/uploads/2026/04/AOUO-Contact-List-4.29.26.pdf"
PDF_PATH = ROOT / ".cache" / "hi_aouo.pdf"
SOURCE = "hi-dcca-aouo-registry"
MIN_COLUMNS = 9

log = logging.getLogger(__name__)


def fetch_pdf(path: Path = PDF_PATH) -> Path:
    if not path.exists():
        response = requests.get(PDF_URL, timeout=120, headers={"User-Agent": http.USER_AGENT})
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    return path


def parse_pdf(path: Path, retrieved_at: str = "") -> list[Association]:
    """One record per table row that carries a registration number and a name."""
    rows: list[Association] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for raw in table:
                cells = [" ".join((cell or "").split()) for cell in raw]     # PDF cells wrap
                if len(cells) < MIN_COLUMNS or not cells[0].isdigit() or not cells[1]:
                    continue
                officer_name = " ".join(part for part in (cells[4], cells[3]) if part)
                rows.append(Association(
                    state="HI", source=SOURCE, source_url=SOURCE_PAGE, record_id=cells[0], name=cells[1],
                    status="registered AOUO", manager_name=cells[9] if len(cells) > 9 else "",
                    officers=[Officer(name=officer_name.title(), title=cells[2].title())] if officer_name else [],
                    retrieved_at=retrieved_at))
    return rows


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No options."""


def run(args: argparse.Namespace) -> int:
    rows = parse_pdf(fetch_pdf(), retrieved_at=now_iso())
    total = replace_rows(REGISTRIES_PATH, (row.as_row() for row in rows), {"HI"})
    update_coverage("HI", "hoa_registry", {
        "source": "DCCA Real Estate Branch AOUO contact list (PDF)", "records": len(rows), "url": SOURCE_PAGE})
    print(f"{len(rows)} Hawaii AOUOs parsed; {REGISTRIES_PATH.name} now {total} rows")
    return 0
