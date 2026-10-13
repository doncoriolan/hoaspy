"""Broward County, FL association liens from the county's official-records index.

    python -m hoaspy liens --years 2025
    python -m hoaspy liens --years 2015-2025              # a decade
    python -m hoaspy liens --years 2025 --all-parties     # every lien, not just associations'

Output (default liens/):
    liens.jsonl          one record per lien-family document
    liens.csv            the same rows, the CSV_COLUMNS only
    by_association.csv   filings per association, with escalation counts
    sources.json         what was fetched, from where, with the coverage caveat

Document types collected: claim of lien, amended lien, partial lien and
its satisfaction, notice of contest of lien, lis pendens. A lien followed
by a lis pendens on the same association is the escalation-to-foreclosure
signal.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from hoaspy import ROOT
from hoaspy.collect.liens import LIENS_DIR, records_broward
from hoaspy.lib.jsonl import now_iso, write_json, write_rows

CACHE_DIR = ROOT / ".cache" / "broward"
CSV_COLUMNS = [
    "doc_id", "year", "recorded_date", "doc_type", "doc_type_label", "state",
    "county", "association", "respondent", "amount", "case_number",
    "parcel_id", "legal_description", "n_parties",
]
LIEN_CODES = ("LIE", "LIEX")
LIS_PENDENS_CODE = "LP"
CONTEST_CODE = "NCL"

log = logging.getLogger(__name__)


def parse_years(spec: str) -> list[int]:
    """'2025', '2015-2025' or '2019,2021,2024' as a sorted list of years."""
    years: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if match := re.fullmatch(r"(\d{4})-(\d{4})", part):
            first, last = int(match.group(1)), int(match.group(2))
            if first > last:
                raise ValueError(f"{part}: start year is after end year")
            years.extend(range(first, last + 1))
        elif re.fullmatch(r"\d{4}", part):
            years.append(int(part))
        else:
            raise ValueError(f"cannot parse year spec {part!r}")
    return sorted(set(years))


def by_association(rows: list[dict]) -> dict[str, dict]:
    """Filings per association: liens, lis pendens, contested, other, first and last year."""
    totals: dict[str, dict] = defaultdict(
        lambda: {"liens": 0, "lis_pendens": 0, "contested": 0, "other": 0, "first_year": None, "last_year": None})
    for row in rows:
        entry = totals[row.get("association") or "(unnamed)"]
        code = row["doc_type"]
        if code in LIEN_CODES:
            entry["liens"] += 1
        elif code == LIS_PENDENS_CODE:
            entry["lis_pendens"] += 1
        elif code == CONTEST_CODE:
            entry["contested"] += 1
        else:
            entry["other"] += 1
        year = row.get("year")
        entry["first_year"] = year if entry["first_year"] is None else min(entry["first_year"], year)
        entry["last_year"] = year if entry["last_year"] is None else max(entry["last_year"], year)
    return totals


def write_outputs(rows: list[dict], sources: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_rows(out_dir / "liens.jsonl", rows)
    with (out_dir / "liens.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "respondent": "; ".join(row.get("respondents") or [])[:200]})
    with (out_dir / "by_association.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["association", "liens", "lis_pendens", "contested", "other", "first_year", "last_year",
                         "total"])
        totals = sorted(by_association(rows).items(), key=lambda item: -(item[1]["liens"] + item[1]["lis_pendens"]))
        for name, entry in totals:
            writer.writerow([name, entry["liens"], entry["lis_pendens"], entry["contested"], entry["other"],
                             entry["first_year"], entry["last_year"],
                             entry["liens"] + entry["lis_pendens"] + entry["contested"] + entry["other"]])
    write_json(out_dir / "sources.json", sources)


def print_summary(rows: list[dict]) -> None:
    if not rows:
        print("\nNo records matched.")
        return
    by_type = Counter(row["doc_type_label"] for row in rows)
    by_year = Counter(row["year"] for row in rows)
    filers = Counter(row["association"] for row in rows if row["doc_type"] in LIEN_CODES and row.get("association"))
    with_parcel = sum(1 for row in rows if row.get("parcel_id"))

    print(f"\n{len(rows):,} lien-family documents")
    print(f"  {with_parcel:,} carry a parcel id "
          f"({with_parcel / len(rows) * 100:.1f}% — the index rarely indexes liens to a parcel)")
    print("\nBy document type")
    width = max(len(label) for label in by_type)
    for label, count in by_type.most_common():
        print(f"  {label:<{width}}  {count:>7,}")
    if len(by_year) > 1:
        print("\nBy year")
        for year in sorted(by_year):
            print(f"  {year}  {by_year[year]:>7,}")
    print("\nMost active lien filers")
    for name, count in filers.most_common(12):
        print(f"  {count:>5}  {name[:66]}")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--years", default="2025", help="year, range (2015-2025), or comma list")
    parser.add_argument("--out", type=Path, default=LIENS_DIR)
    parser.add_argument("--cache", type=Path, default=CACHE_DIR, help="where the downloaded index files are kept")
    parser.add_argument("--types", nargs="+", help="override the document types to keep")
    parser.add_argument("--all-parties", action="store_true", help="keep every lien, not just association-filed ones")
    parser.add_argument("--min-liens", type=int, default=0,
                        help="only keep associations with at least this many filings")


def run(args: argparse.Namespace) -> int:
    try:
        years = parse_years(args.years)
    except ValueError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    started = time.time()
    retrieved_at = now_iso()
    log.info("collecting Broward lien records for %s", ", ".join(str(year) for year in years))
    try:
        records = records_broward.fetch_years(years, args.cache, keep_types=args.types,
                                              associations_only=not args.all_parties, retrieved_at=retrieved_at)
    except Exception as exc:
        print(f"\nBroward SFTP failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    rows = [record.as_row() for record in records]
    if args.min_liens:
        counts = Counter(row["association"] for row in rows if row.get("association"))
        keep = {name for name, count in counts.items() if count >= args.min_liens}
        before = len(rows)
        rows = [row for row in rows if row.get("association") in keep]
        log.info("min-liens %d: kept %d of %d records", args.min_liens, len(rows), before)

    sources = {
        "retrieved_at": retrieved_at,
        "source": "Broward County Records, Taxes and Treasury — Official Records index",
        "access": f"sftp://{records_broward.SFTP_HOST} (public credentials)",
        "page": records_broward.SOURCE_PAGE,
        "years": years,
        "document_types": args.types or records_broward.DEFAULT_TYPES,
        "associations_only": not args.all_parties,
        "records": len(rows),
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("The index carries a parcel id for ~0.5% of liens versus 100% of "
                   "deeds, so these records identify parties and dates but not "
                   "property addresses."),
    }
    write_outputs(rows, sources, args.out)
    print_summary(rows)
    print(f"\nWrote {len(rows):,} records to {args.out}/")
    return 0
