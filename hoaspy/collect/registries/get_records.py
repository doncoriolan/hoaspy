"""Florida condominium projects (DBPR) and Texas management certificates
(TREC): the two registries that give every association a real name and a
real location, joined into one file.

    python -m hoaspy records                  # both states
    python -m hoaspy records --state FL       # one state
    python -m hoaspy records --county Harris --name-contains oaks

Output (default records/):
    associations.jsonl   one row per association: the registry columns plus
                         `id` (a hash of state, source, record id and ZIP,
                         so re-runs keep their ids), `type`, `source_region`,
                         `file_number`, `address_raw` and the managing
                         entity's `manager_id`, `manager_address`,
                         `manager_city`, `manager_state`, `manager_zip`
    associations.csv     the same rows, the CSV_COLUMNS only
    sources.json         what was fetched, row counts, timestamp
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.registries import records_fl, records_tx
from hoaspy.lib import http
from hoaspy.lib.jsonl import now_iso, write_json, write_rows
from hoaspy.lib.records import Association

DEFAULT_OUT_DIR = ROOT / "records"
STATES = ("FL", "TX")

CSV_COLUMNS = [
    "id", "state", "source", "name", "type", "address", "city", "county", "zip",
    "units", "status", "status_detail", "recorded_date",
    "manager_name", "manager_city", "manager_state", "manager_zip",
    "document_url", "source_url", "retrieved_at",
]

log = logging.getLogger(__name__)


def derived_id(record: Association) -> str:
    """A stable id from what identifies a community. The registries share
    no key, so one is derived — which also makes re-runs idempotent."""
    basis = "|".join([record.state, record.source, record.record_id or record.name.upper(), record.zip])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def collect(session: requests.Session, states: list[str], timeout: float) -> tuple[list[dict], dict]:
    """(rows, sources) for the requested states, de-duplicated on `id` (an
    association can appear twice where extracts overlap a region boundary)."""
    retrieved_at = now_iso()
    records: list[Association] = []
    sources: dict = {"retrieved_at": retrieved_at, "states": {}}

    if "FL" in states:
        log.info("Florida: fetching DBPR condominium extracts ...")
        found = records_fl.fetch(session, timeout=timeout, retrieved_at=retrieved_at)
        records.extend(found)
        sources["states"]["FL"] = {
            "source": "DBPR Division of Florida Condominiums, Timeshares and Mobile Homes",
            "page": records_fl.SOURCE_PAGE, "files": list(records_fl.EXTRACTS), "rows": len(found)}
    if "TX" in states:
        log.info("Texas: fetching TREC management certificates ...")
        found = records_tx.fetch(session, timeout=timeout, retrieved_at=retrieved_at)
        records.extend(found)
        sources["states"]["TX"] = {
            "source": "Texas Real Estate Commission HOA management certificates",
            "page": records_tx.SOURCE_PAGE, "endpoint": records_tx.DATASET_URL, "rows": len(found)}

    rows: list[dict] = []
    seen: set[str] = set()
    for record in records:
        record.extras["id"] = derived_id(record)
        if record.extras["id"] in seen:
            continue
        seen.add(record.extras["id"])
        rows.append(record.as_row())
    if len(rows) != len(records):
        log.info("dropped %d duplicate rows", len(records) - len(rows))
    sources["total_records"] = len(rows)
    return rows, sources


def apply_filters(rows: list[dict], args: argparse.Namespace) -> list[dict]:
    out = rows
    if args.county:
        wanted = {county.lower() for county in args.county}
        out = [row for row in out if row.get("county", "").lower() in wanted]
    if args.name_contains:
        needle = args.name_contains.lower()
        out = [row for row in out if needle in row.get("name", "").lower()]
    if args.status:
        wanted = {status.lower() for status in args.status}
        out = [row for row in out
               if row.get("status_detail", "").lower() in wanted or row.get("status", "").lower() in wanted]
    if args.with_address:
        out = [row for row in out if row.get("address")]
    return out


def write_outputs(rows: list[dict], sources: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_rows(out_dir / "associations.jsonl", rows)
    with (out_dir / "associations.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    write_json(out_dir / "sources.json", sources)


def print_summary(rows: list[dict]) -> None:
    if not rows:
        print("\nNo records matched.")
        return
    by_state = Counter(row["state"] for row in rows)
    with_address = sum(1 for row in rows if row.get("address"))
    with_manager = sum(1 for row in rows if row.get("manager_name"))
    print(f"\n{len(rows)} associations  ({', '.join(f'{state} {n}' for state, n in sorted(by_state.items()))})")
    print(f"  {with_address} with a street address, {with_manager} with a named manager")

    def show(title: str, counter: Counter, top: int = 8) -> None:
        if not counter:
            return
        print(f"\n{title}")
        width = max(len(str(key)) for key, _ in counter.most_common(top))
        for key, count in counter.most_common(top):
            print(f"  {key:<{width}}  {count}")

    show("Counties", Counter(row["county"] for row in rows if row.get("county")))
    show("Registration flags", Counter(row["status_detail"] for row in rows if row.get("status_detail")))
    show("Managing entities", Counter(row["manager_name"] for row in rows if row.get("manager_name")))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", action="append", choices=STATES,
                        help="limit to one state (repeatable); default both")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--county", action="append", help="filter by county (repeatable)")
    parser.add_argument("--name-contains", help="filter by a substring of the association name")
    parser.add_argument("--status", action="append", help="filter by registration status, e.g. Delinquent")
    parser.add_argument("--with-address", action="store_true", help="keep only records that have a street address")
    parser.add_argument("--timeout", type=float, default=120.0)


def run(args: argparse.Namespace) -> int:
    states = args.state or list(STATES)
    started = time.time()
    try:
        rows, sources = collect(http.session(), states, args.timeout)
    except requests.RequestException as exc:
        print(f"\nFetch failed: {exc}\n", file=sys.stderr)
        return 1
    if not rows:
        print("\nNothing collected — both sources returned no rows.\n", file=sys.stderr)
        return 1

    filtered = apply_filters(rows, args)
    sources["filters_applied"] = {key: value for key, value in {
        "county": args.county, "name_contains": args.name_contains,
        "status": args.status, "with_address": args.with_address or None}.items() if value}
    sources["records_written"] = len(filtered)
    sources["duration_seconds"] = round(time.time() - started, 1)
    write_outputs(filtered, sources, args.out)
    print_summary(filtered)
    print(f"\nWrote {len(filtered)} records to {args.out}/")
    return 0
