"""South Carolina HOA complaints from the Department of Consumer Affairs.

    python -m hoaspy sc-complaints

S.C. Code 37-6-117 has SCDCA collect HOA complaints and report them to the
legislature every year. The reports name the association, its county, its
management company, the complaint category and the outcome. The XLSX links
are read off the HOA-reports page, so a new year's report is picked up on
its own. A complaint that appears in two adjacent reports is kept once.

Output: records/sc_complaints.jsonl, one row per complaint.
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import time
from collections import Counter

import openpyxl

from hoaspy import ROOT
from hoaspy.lib import http
from hoaspy.lib.jsonl import now_iso, write_rows
from hoaspy.lib.records import Complaint

OUTPUT_PATH = ROOT / "records" / "sc_complaints.jsonl"
SOURCE_PAGE = "https://consumer.sc.gov/HOA-reports"
SITE_URL = "https://consumer.sc.gov"
SOURCE = "sc-dca-hoa-complaints"

# The header wording drifts a little between years, so each column is
# found by a pattern over the lower-cased header.
COLUMN_PATTERNS = {
    "number": r"complaint number",
    "date": r"complaint date",
    "name": r"hoa name",
    "city": r"hoa city",
    "county": r"hoa county",
    "manager": r"management company name",
    "category": r"^complaint description$",
    "status": r"^status$",
}
NO_NAME = ("none", "n/a", "unknown")

log = logging.getLogger(__name__)


def cell_text(value) -> str:
    text = str(value) if value is not None else ""
    return " ".join(text.replace("_x000D_", " ").split())


def cell(row: tuple, columns: dict[str, int | None], key: str) -> str:
    """The text of the mapped column `key` in one sheet row; '' when the sheet lacks it."""
    index = columns[key]
    return cell_text(row[index]) if index is not None and index < len(row) else ""


def parse_workbook(content: bytes, url: str, retrieved_at: str = "") -> list[Complaint]:
    """Every complaint on every sheet that has an "HOA name" column."""
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    out: list[Complaint] = []
    for sheet in workbook.worksheets:
        rows = sheet.iter_rows(values_only=True)
        try:
            header = [cell_text(cell).lower() for cell in next(rows)]
        except StopIteration:
            continue
        columns = {}
        for key, pattern in COLUMN_PATTERNS.items():
            columns[key] = next((i for i, heading in enumerate(header) if re.search(pattern, heading)), None)
        if columns["name"] is None:
            continue
        for row in rows:
            name = cell(row, columns, "name")
            if not name or name.lower() in NO_NAME:
                continue
            date = cell(row, columns, "date")[:10]
            number = cell(row, columns, "number")
            year = None
            if match := re.search(r"\b(20[12]\d)\b", date):
                year = int(match.group(1))
            elif match := re.match(r"C(\d{2})-", number):
                year = 2000 + int(match.group(1))
            out.append(Complaint(
                state="SC", source=SOURCE, source_url=url, complaint_number=number, date=date, year=year,
                name=name, city=cell(row, columns, "city").title(), county=cell(row, columns, "county").title(),
                manager_name=cell(row, columns, "manager"), category=cell(row, columns, "category"),
                status=cell(row, columns, "status"),
                retrieved_at=retrieved_at))
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No options."""


def run(args: argparse.Namespace) -> int:
    session = http.session()
    started = time.time()
    page = session.get(SOURCE_PAGE, timeout=60)
    page.raise_for_status()
    links = sorted(set(re.findall(r'href="([^"]*\.xlsx)"', page.text, re.I)))
    log.info("found %d report workbooks", len(links))

    retrieved_at = now_iso()
    complaints: list[Complaint] = []
    for href in links:
        url = href if href.startswith("http") else SITE_URL + href
        try:
            response = session.get(url, timeout=120)
            response.raise_for_status()
            found = parse_workbook(response.content, url, retrieved_at)
        except Exception as exc:
            log.error("%s failed: %s", url, exc)
            continue
        complaints.extend(found)
        log.info("%s -> %d complaints", url.rsplit("/", 1)[-1], len(found))
        time.sleep(1.0)

    seen: set[tuple[str, str]] = set()
    rows = []
    for complaint in complaints:
        key = (complaint.complaint_number, complaint.name.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(complaint.as_row())
    write_rows(OUTPUT_PATH, rows)

    by_year = Counter(row["year"] for row in rows if row["year"])
    print(f"\n{len(rows):,} complaints ({len(complaints) - len(rows)} duplicates dropped)")
    for year in sorted(by_year):
        print(f"  {year}: {by_year[year]}")
    print(f"Wrote {OUTPUT_PATH} in {time.time() - started:.0f}s")
    return 0
