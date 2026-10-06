"""Miami-Dade County, FL association liens, one association name at a time.

Miami-Dade sells no bulk index the way Broward's SFTP hands one out, so
this queries the Clerk's public search API by party name — the mode that
stays under the API's 500-row cap (records_miamidade.py says why).

    python -m hoaspy miamidade-liens --cookie '<.PremierIDDade value>'
    python -m hoaspy miamidade-liens --cookie ... --limit 200
    python -m hoaspy miamidade-liens --cookie ... --names names.txt

The `.PremierIDDade` cookie is an anonymous session cookie (no login); copy
its value from a browser at onlineservices.miamidadeclerk.gov (DevTools ->
Application -> Cookies). It is never committed.

Coverage is best-effort, not complete, and sources.json says so: an
association is found only when the queried name matches the Clerk's
spelling ("AMARETTO HOMEOWNERS ASSN INC" versus "AMARETTO CONDO" misses),
and a name broad enough to hit the cap is flagged, not silently cut. The
run is checkpointed per name and resumes where it stopped.

Output (default liens/):
    liens_miamidade.jsonl   one record per lien-family document
    liens_miamidade.csv     the same rows, the CSV_COLUMNS only
    sources.json            provenance and the coverage caveat (per region)
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path

from hoaspy import ROOT
from hoaspy.collect.liens import LIENS_DIR, records_miamidade
from hoaspy.lib.checkpoint import NameCheckpoint
from hoaspy.lib.jsonl import now_iso, read_json, read_rows, write_json, write_rows

DEFAULT_NAMES_PATH = ROOT / "records" / "associations.jsonl"
CHECKPOINT_PREFIX = "miamidade"
MIAMI_DADE_COUNTIES = ("miami-dade", "miami dade", "dade")
MIN_QUERY_LENGTH = 4
CSV_COLUMNS = [
    "doc_id", "cfn", "year", "recorded_date", "doc_type", "doc_type_label",
    "state", "county", "association", "query_name", "respondent", "amount",
    "case_number", "parcel_id", "book_page", "address", "legal_description",
    "n_parties",
]
COMPASS_WORDS = (("WEST", "W"), ("EAST", "E"), ("NORTH", "N"), ("SOUTH", "S"))

log = logging.getLogger(__name__)


def query_name(name: str) -> str:
    """An association name as a party-name query the Clerk's index is
    likely to match: punctuation and filing noise stripped, the compass
    words abbreviated as the county abbreviates them, the distinctive head
    kept so the query neither misses nor broadens into the cap."""
    query = re.sub(r"[,\.'\"]", " ", name.upper())
    query = re.sub(r"\bCONDOMINIUM\b", "CONDO", query)
    for word, short in COMPASS_WORDS:
        query = re.sub(rf"\b{word}\b", short, query)
    query = re.sub(r"\b(A|AN|THE)\b", " ", query)          # filing noise the lien index usually omits
    return re.sub(r"\s+", " ", query).strip()


def miami_dade_names(path: Path, all_florida: bool) -> list[str]:
    """The association names of the Florida registry rows in Miami-Dade County (or all of Florida)."""
    names = []
    for row in read_rows(path):
        if row.get("state") != "FL" or not row.get("name"):
            continue
        county = (row.get("county") or "").strip().lower()
        if all_florida or county in MIAMI_DADE_COUNTIES:
            names.append(row["name"])
    return names


def load_names(args: argparse.Namespace) -> list[str]:
    """The distinct queries to run, from --names or the registry rows."""
    if args.names:
        raw = [line.strip() for line in Path(args.names).read_text().splitlines() if line.strip()]
    else:
        raw = miami_dade_names(args.assoc_file, args.all_florida)
    seen, out = set(), []
    for name in raw:
        query = query_name(name)
        if len(query) < MIN_QUERY_LENGTH or query in seen:
            continue
        seen.add(query)
        out.append(query)
    return out[:args.limit] if args.limit else out


def write_outputs(rows: list[dict], sources: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_rows(out_dir / "liens_miamidade.jsonl", rows)
    with (out_dir / "liens_miamidade.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "respondent": "; ".join(row.get("respondents") or [])[:200]})
    # sources.json is per region: merge, never clobber Broward's entry.
    path = out_dir / "sources.json"
    existing = read_json(path, {})
    if existing and "miami_dade" not in existing and "source" in existing:
        existing = {"broward": existing}        # migrate the old single-region file
    existing["miami_dade"] = sources
    write_json(path, existing)


def print_summary(rows: list[dict]) -> None:
    if not rows:
        print("\nNo lien-family records matched any queried name.")
        return
    by_type = Counter(row["doc_type_label"] for row in rows)
    filers = Counter(row["association"] or row["query_name"] for row in rows if row["doc_type"] == "LIE")
    with_parcel = sum(1 for row in rows if row.get("parcel_id"))
    print(f"\n{len(rows):,} lien-family documents")
    print(f"  {with_parcel:,} carry a folio/parcel id ({with_parcel / len(rows) * 100:.0f}%)")
    print("\nBy document type")
    for label, count in by_type.most_common():
        print(f"  {label:<28} {count:>6,}")
    print("\nMost active lien filers")
    for name, count in filers.most_common(12):
        print(f"  {count:>4}  {name[:64]}")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cookie", required=True, help="the .PremierIDDade cookie value (with or without name=)")
    parser.add_argument("--names", type=Path, help="file of association names, one per line")
    parser.add_argument("--assoc-file", type=Path, default=DEFAULT_NAMES_PATH,
                        help="source of association names when --names is not given")
    parser.add_argument("--all-florida", action="store_true",
                        help="query every FL association, not just Miami-Dade County")
    parser.add_argument("--types", nargs="+", help="override the Miami-Dade document types")
    parser.add_argument("--limit", type=int, help="cap the number of names queried")
    parser.add_argument("--pace", type=float, default=0.4, help="seconds between requests")
    parser.add_argument("--out", type=Path, default=LIENS_DIR)
    parser.add_argument("--fresh", action="store_true", help="ignore any checkpoint and start the run over")


def run(args: argparse.Namespace) -> int:
    try:
        names = load_names(args)
    except FileNotFoundError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    if not names:
        print("\nNo association names to query.\n", file=sys.stderr)
        return 1
    try:
        session = records_miamidade.open_session(args.cookie)
    except records_miamidade.MiamiDadeError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    started = time.time()
    args.out.mkdir(parents=True, exist_ok=True)
    checkpoint = NameCheckpoint(args.out, CHECKPOINT_PREFIX)
    if args.fresh:
        checkpoint.clear()
    prior, done = checkpoint.load()
    if prior:
        log.info("resuming: %d records from %d names already collected", len(prior), len(done))
    log.info("querying %d Miami-Dade association names (%d to go)", len(names),
             len([name for name in names if name not in done]))

    def progress(i: int, total: int, name: str, found: int) -> None:
        if args.verbose or i % 50 == 0 or i == total:
            log.info("  %d/%d names, %d new records (…%s)", i, total, found, name[:30])

    retrieved_at = now_iso()
    try:
        with checkpoint:
            fresh = records_miamidade.fetch_for_names(
                session, names, doctypes=args.types, pace=args.pace, progress=progress, skip=done,
                seen_ids={row["doc_id"] for row in prior}, on_name_done=checkpoint.add, retrieved_at=retrieved_at)
    except records_miamidade.MiamiDadeError as exc:
        print(f"\nMiami-Dade collection failed: {exc}\nProgress is checkpointed — rerun the same command to resume.\n",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed; rerun to resume.\n", file=sys.stderr)
        return 130

    rows = [dict(row, retrieved_at=row.get("retrieved_at") or retrieved_at) for row in prior + fresh]
    sources = {
        "retrieved_at": retrieved_at,
        "source": "Miami-Dade Clerk of the Court & Comptroller — Official Records search API",
        "access": "public search API (anonymous .PremierIDDade cookie); no login, no bulk feed",
        "page": records_miamidade.SEARCH_PAGE,
        "method": "per-association party-name query",
        "document_types": args.types or records_miamidade.DEFAULT_TYPES,
        "names_queried": len(names),
        "records": len(rows),
        "duration_seconds": round(time.time() - started, 1),
        "coverage": "best-effort, NOT complete",
        "caveat": ("Per-association name queries against the public search API. Coverage "
                   "is bounded by (a) name-spelling agreement between the association "
                   "list and the Clerk's party index, and (b) a 500-row result cap that "
                   "truncates over-broad names and busy days. This is targeted coverage, "
                   "not a full county index; the paid Official Records FTP folder is the "
                   "complete source."),
    }
    write_outputs(rows, sources, args.out)
    checkpoint.clear()
    print_summary(rows)
    print(f"\nWrote {len(rows):,} records to {args.out}/liens_miamidade.jsonl")
    return 0
