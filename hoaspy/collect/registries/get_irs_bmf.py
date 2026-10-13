"""Homeowners and condominium associations in the IRS Exempt Organizations
Business Master File (EO BMF), every state.

    python -m hoaspy irs-bmf                  # all states
    python -m hoaspy irs-bmf --state OK WY    # a few

The BMF is the IRS's public roster of organizations that hold a ruling of
tax exemption. Most HOAs file Form 1120-H and never appear in it, but the
ones that sought 501(c)(4) or (c)(7) status do — with EIN, address, ruling
date, NTEE code (L50 is "Homeowners Associations") and the latest reported
income and assets. It is the only free, nationwide, government-published
list of associations, so it is the floor for the states that publish no
registry or corporate file.

An organization is kept when its NTEE code is L50 or its name reads as an
association. The regional files eo1/eo2/eo3.csv are cached under
.cache/irs/ and downloaded when missing.

Output: records/irs_exempt_orgs.jsonl, one row per EIN in the registry
shape plus `ein`, `in_care_of`, `ntee`, `subsection`, `tax_period`,
`income_amt` and `asset_amt`; coverage.json collected["irs_exempt"] per
state.
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from collections import Counter
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, replace_rows
from hoaspy.lib.names import REGISTRY_NAME_RE
from hoaspy.lib.records import Association
from hoaspy.lib.states import STATE_CODES

CACHE_DIR = ROOT / ".cache" / "irs"
OUTPUT_PATH = ROOT / "records" / "irs_exempt_orgs.jsonl"
SOURCE_PAGE = "https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf"
EXTRACT_URLS = {name: f"https://www.irs.gov/pub/irs-soi/{name}.csv" for name in ("eo1", "eo2", "eo3")}
SOURCE = "irs-eo-bmf"
MIN_EXTRACT_BYTES = 1_000_000           # a smaller file is a failed download

SUBSECTION = {
    "03": "501(c)(3)", "04": "501(c)(4)", "05": "501(c)(5)", "06": "501(c)(6)",
    "07": "501(c)(7)", "08": "501(c)(8)", "10": "501(c)(10)", "12": "501(c)(12)",
    "13": "501(c)(13)", "19": "501(c)(19)",
}
STATUS = {"01": "exempt", "02": "conditional exemption", "12": "trust (Form 990-PF)",
          "25": "terminated (990-N filer)"}

log = logging.getLogger(__name__)


def ensure_extracts(session: requests.Session) -> list[Path]:
    """The three regional files, downloaded when missing or truncated."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, url in EXTRACT_URLS.items():
        path = CACHE_DIR / f"{name}.csv"
        if not path.exists() or path.stat().st_size < MIN_EXTRACT_BYTES:
            log.info("downloading %s", url)
            response = session.get(url, timeout=900)
            response.raise_for_status()
            path.write_bytes(response.content)
        paths.append(path)
    return paths


def ruling_month(value: str) -> str:
    """The RULING column (YYYYMM, sometimes longer) as YYYY-MM; '' otherwise."""
    value = (value or "").strip()
    return f"{value[:4]}-{value[4:6]}" if len(value) >= 6 and value.isdigit() else ""


def dollar_amount(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_association(row: dict) -> bool:
    if (row.get("NTEE_CD") or "").startswith("L50"):
        return True
    return bool(REGISTRY_NAME_RE.search(row.get("NAME") or "") or REGISTRY_NAME_RE.search(row.get("SORT_NAME") or ""))


def to_record(row: dict, retrieved_at: str = "") -> Association:
    name = " ".join((row.get("NAME") or "").split())
    sort_name = " ".join((row.get("SORT_NAME") or "").split())
    if sort_name and not REGISTRY_NAME_RE.search(name) and REGISTRY_NAME_RE.search(sort_name):
        name = sort_name            # NAME is the management agent, SORT_NAME the association
    code = row.get("SUBSECTION") or ""
    subsection = SUBSECTION.get(code, f"501(c)({int(code)})" if code.isdigit() else "")
    status = STATUS.get(row.get("STATUS") or "", row.get("STATUS") or "")
    ruling = ruling_month(row.get("RULING") or "")
    ein = (row.get("EIN") or "").strip()
    return Association(
        state=row["STATE"], source=SOURCE, source_url=SOURCE_PAGE, record_id=ein, name=name,
        status=f"{subsection} {status}".strip(), status_detail=f"IRS ruling {ruling}" if ruling else "",
        recorded_date=ruling, address=" ".join((row.get("STREET") or "").split()),
        city=" ".join((row.get("CITY") or "").split()).title(), zip=(row.get("ZIP") or "")[:5],
        retrieved_at=retrieved_at,
        extras={
            "ein": f"{ein[:2]}-{ein[2:]}" if len(ein) == 9 else ein,
            "in_care_of": " ".join((row.get("ICO") or "").replace("%", "").split()),
            "ntee": row.get("NTEE_CD") or "", "subsection": subsection,
            "tax_period": row.get("TAX_PERIOD") or "",
            "income_amt": dollar_amount(row.get("INCOME_AMT")), "asset_amt": dollar_amount(row.get("ASSET_AMT")),
        })


def collect(paths: list[Path], states: set[str] | None, retrieved_at: str = "") -> list[Association]:
    """The association rows of the extracts, one per EIN, for `states` (or every state)."""
    out: list[Association] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="latin-1") as fh:
            for row in csv.DictReader(fh):
                state = (row.get("STATE") or "").strip()
                if state not in STATE_CODES or (states and state not in states) or not is_association(row):
                    continue
                record = to_record(row, retrieved_at)
                if not record.name or record.record_id in seen:
                    continue
                seen.add(record.record_id)
                out.append(record)
        log.info("%s: %d association rows so far", path.name, len(out))
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", nargs="+", help="limit to these state codes")


def run(args: argparse.Namespace) -> int:
    started = time.time()
    paths = ensure_extracts(http.session())
    states = {code.upper() for code in args.state} if args.state else None
    records = collect(paths, states, retrieved_at=now_iso())

    by_state = Counter(record.state for record in records)
    total = replace_rows(OUTPUT_PATH, (record.as_row() for record in records), set(by_state) | (states or set()))
    for state, count in sorted(by_state.items()):
        update_coverage(state, "irs_exempt", {
            "source": "IRS Exempt Organizations Business Master File", "records": count, "url": SOURCE_PAGE,
            "note": "501(c) HOAs/condos only (NTEE L50 or association name); 1120-H filers absent"})
        print(f"  {state}: {count:,} tax-exempt associations")
    log.info("%s: %d rows total (%d fresh) in %.0fs", OUTPUT_PATH.name, total, len(records), time.time() - started)
    return 0
