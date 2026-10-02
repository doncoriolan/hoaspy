#!/usr/bin/env python
"""Collect HOA Claims of Lien from Broward County Official Records.

    ./venv/bin/python -m hoaspy.collect.liens.get_liens --years 2025
    ./venv/bin/python -m hoaspy.collect.liens.get_liens --years 2015-2025          # a decade
    ./venv/bin/python -m hoaspy.collect.liens.get_liens --years 2025 --all-parties # don't filter to HOAs

Output (default ./liens/):
    liens.jsonl        one record per lien-family document
    liens.csv          flattened
    by_association.csv filings per association, with escalation counts
    sources.json       provenance

Document types collected: claim of lien, amended lien, partial lien and its
satisfaction, notice of contest of lien, lis pendens. A lien plus a later lis
pendens on the same association is the escalation-to-foreclosure signal.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hoaspy.collect.liens import records_broward
from hoaspy import ROOT
DEFAULT_OUT = ROOT / "liens"
CACHE = ROOT / ".cache" / "broward"

log = logging.getLogger("liens")

FIELDS = [
    "doc_id", "year", "recorded_date", "doc_type", "doc_type_label", "state",
    "county", "association", "respondent", "amount", "case_number",
    "parcel_id", "legal_description", "n_parties",
]



def parse_years(spec: str) -> list[int]:
    """'2025', '2015-2025', or '2019,2021,2024'."""
    years: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if m := re.fullmatch(r"(\d{4})-(\d{4})", part):
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValueError(f"{part}: start year is after end year")
            years.extend(range(lo, hi + 1))
        elif re.fullmatch(r"\d{4}", part):
            years.append(int(part))
        else:
            raise ValueError(f"cannot parse year spec {part!r}")
    return sorted(set(years))


def write_outputs(records: list[dict], sources: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "liens.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    with (out_dir / "liens.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({**r, "respondent": "; ".join(r.get("respondents") or [])[:200]})

    # Per-association rollup: the unit of analysis is the association, not the doc.
    agg: dict[str, dict] = defaultdict(
        lambda: {"liens": 0, "lis_pendens": 0, "contested": 0, "other": 0,
                 "first_year": None, "last_year": None})
    for r in records:
        name = r.get("association") or "(unnamed)"
        a = agg[name]
        t = r["doc_type"]
        if t in ("LIE", "LIEX"):
            a["liens"] += 1
        elif t == "LP":
            a["lis_pendens"] += 1
        elif t == "NCL":
            a["contested"] += 1
        else:
            a["other"] += 1
        y = r.get("year")
        a["first_year"] = y if a["first_year"] is None else min(a["first_year"], y)
        a["last_year"] = y if a["last_year"] is None else max(a["last_year"], y)

    with (out_dir / "by_association.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["association", "liens", "lis_pendens", "contested", "other",
                         "first_year", "last_year", "total"])
        rows = sorted(agg.items(), key=lambda kv: -(kv[1]["liens"] + kv[1]["lis_pendens"]))
        for name, a in rows:
            writer.writerow([name, a["liens"], a["lis_pendens"], a["contested"],
                             a["other"], a["first_year"], a["last_year"],
                             a["liens"] + a["lis_pendens"] + a["contested"] + a["other"]])

    (out_dir / "sources.json").write_text(json.dumps(sources, indent=2))


def print_summary(records: list[dict]) -> None:
    if not records:
        print("\nNo records matched.")
        return
    by_type = Counter(r["doc_type_label"] for r in records)
    by_year = Counter(r["year"] for r in records)
    filers = Counter(r["association"] for r in records
                     if r["doc_type"] in ("LIE", "LIEX") and r.get("association"))
    with_parcel = sum(1 for r in records if r.get("parcel_id"))

    print(f"\n{len(records):,} lien-family documents")
    print(f"  {with_parcel:,} carry a parcel id "
          f"({with_parcel / len(records) * 100:.1f}% — the index rarely indexes liens to a parcel)")

    print("\nBy document type")
    w = max(len(k) for k in by_type)
    for k, v in by_type.most_common():
        print(f"  {k:<{w}}  {v:>7,}")

    if len(by_year) > 1:
        print("\nBy year")
        for y in sorted(by_year):
            print(f"  {y}  {by_year[y]:>7,}")

    print("\nMost active lien filers")
    for name, n in filers.most_common(12):
        print(f"  {n:>5}  {name[:66]}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", default="2025",
                    help="year, range (2015-2025), or comma list")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache", type=Path, default=CACHE,
                    help="where the downloaded index files are kept")
    ap.add_argument("--types", nargs="+", help="override document types to keep")
    ap.add_argument("--all-parties", action="store_true",
                    help="keep every lien, not just association-filed ones")
    ap.add_argument("--min-liens", type=int, default=0,
                    help="only keep associations with at least this many filings")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    try:
        years = parse_years(args.years)
    except ValueError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    started = time.time()
    log.info("collecting Broward lien records for %s",
             ", ".join(str(y) for y in years))

    try:
        records = records_broward.fetch_years(
            years, args.cache, keep_types=args.types,
            associations_only=not args.all_parties)
    except Exception as exc:
        print(f"\nBroward SFTP failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    if args.min_liens:
        counts = Counter(r["association"] for r in records if r.get("association"))
        keep = {n for n, c in counts.items() if c >= args.min_liens}
        before = len(records)
        records = [r for r in records if r.get("association") in keep]
        log.info("min-liens %d: kept %d of %d records", args.min_liens, len(records), before)

    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        r["retrieved_at"] = now

    sources = {
        "retrieved_at": now,
        "source": "Broward County Records, Taxes and Treasury — Official Records index",
        "access": f"sftp://{records_broward.HOST} (public credentials)",
        "page": records_broward.SOURCE_PAGE,
        "years": years,
        "document_types": args.types or records_broward.DEFAULT_TYPES,
        "associations_only": not args.all_parties,
        "records": len(records),
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("The index carries a parcel id for ~0.5% of liens versus 100% of "
                   "deeds, so these records identify parties and dates but not "
                   "property addresses."),
    }

    write_outputs(records, sources, args.out)
    print_summary(records)
    print(f"\nWrote {len(records):,} records to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
