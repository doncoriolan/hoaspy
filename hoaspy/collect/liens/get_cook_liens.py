"""Cook County, IL association liens from the Clerk's Recordings System.

    python -m hoaspy cook-liens                              # the sweep, then the detail pages it needs
    python -m hoaspy cook-liens --details all                # … and every other detail page (addresses, co-parties)
    python -m hoaspy cook-liens --no-sweep --details none    # rebuild the output from the cache, no network
    python -m hoaspy cook-liens --terms "UNIT OWNERS" --limit-windows 1 -v   # smoke test

Source: the county recorder's index at https://crs.cookcountyclerkil.gov/Search,
anonymous (no login, no captcha). How the site is searched and parsed is
in records_cook.py. Two phases, both resumable:

1. **The sweep.** Each search term in TERMS is walked back from today
   through the Advanced Search's 1,000-row windows (records_cook.next_cursor),
   in the two PASSES: the lien types with the term on either side, and the
   lis pendens types with the term on the grantor side only — a lender's
   foreclosure that merely joins the association as a defendant is 70% of
   what an either-side search returns and is not wanted. Every results row
   goes into .cache/cook_liens/index.jsonl; .cache/cook_liens/sweep.json
   keeps each term-and-pass cursor, so a stopped run continues where it was.
2. **The detail pages.** A row is enough to shape a record when one of its
   two first parties is the association. `--details needed` (the default)
   fetches the detail page where neither is — the association is a
   co-party — and where the results page cut a party name short at 50
   characters. `--details all` also fetches the rest of the kept documents,
   newest first, for the property address and the full party lists. Pages
   are cached under .cache/cook_liens/details/.

The output is rebuilt from the index and the cache on every stop, so it is
always usable. Requests are paced and sequential; the run stops after
STOP_LOSS failures in a row (exit 3; run it again to resume).

Outputs: liens/liens_cook.jsonl in the shared lien shape plus
`executed_date` and `detail_page` (whether the record came from a detail
page or a results row), liens/sources_cook.json (the recording dates
actually reached per term, counts, what was dropped and why), and
coverage.json states.IL.counties.Cook.liens.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.liens import LIENS_DIR, records_cook
from hoaspy.lib import coverage
from hoaspy.lib.jsonl import append_row, now_iso, read_json, read_rows, write_json, write_rows

OUTPUT_PATH = LIENS_DIR / "liens_cook.jsonl"
SOURCES_PATH = LIENS_DIR / "sources_cook.json"
COVERAGE_PATH = ROOT / "coverage.json"
CACHE_DIR = ROOT / ".cache" / "cook_liens"
DETAILS_DIR = CACHE_DIR / "details"
INDEX_PATH = CACHE_DIR / "index.jsonl"
SWEEP_PATH = CACHE_DIR / "sweep.json"

# One token (or pair) per spelling the clerk uses. The search needs every
# token of a term in one party name, so "CONDO ASSN" would miss "X CONDO
# ASSOCIATION"; the bare community words catch every variant.
TERMS = ("CONDO", "CONDOMINIUM", "HOMEOWNERS", "HOMEOWNER", "TOWNHOME", "TOWNHOMES",
         "UNIT OWNERS", "PROPERTY OWNERS", "OWNERS ASSN", "OWNERS ASSOCIATION",
         "MASTER ASSN", "MASTER ASSOCIATION", "TOWNHOUSE", "TOWNHOUSES", "HOME OWNERS",
         "COMMUNITY ASSN", "COMMUNITY ASSOCIATION", "IMPROVEMENT ASSN", "IMPROVEMENT ASSOCIATION")
# (name, document-type codes, party side). "D" = the term must be in a
# grantor's name: the foreclosures an association filed, not the lenders'.
PASSES = (("liens", ("LIEN", "CORL", "MECL"), ""),
          ("foreclosures", ("LISF", "AMLF", "COLF"), "D"))
STOP_LOSS = 8
SAVE_EVERY = 200        # rewrite the output after this many detail pages
MIN_PACE = 1.5
SEARCH_ONLY_ENTRY = "Cook County Clerk current recordings"     # the coverage.json `unavailable` entry this run replaces

log = logging.getLogger(__name__)


# ---- the cache ------------------------------------------------------------------

def load_index() -> dict[str, dict]:
    """Document number -> the results row that found it (the first sighting wins)."""
    index: dict[str, dict] = {}
    for row in read_rows(INDEX_PATH, skip_bad=True):
        index.setdefault(row["doc_number"], row)
    return index


def load_sweep() -> dict:
    return read_json(SWEEP_PATH, {"terms": {}})


def detail_path(doc_number: str) -> Path:
    return DETAILS_DIR / f"{doc_number}.html"


# ---- phase 1: the sweep ---------------------------------------------------------

class Stopped(RuntimeError):
    """Too many failures in a row; progress is saved."""


def with_retry(crs: records_cook.CookCRS, call, what: str, failures: list[int]):
    """Run `call`; on a lost session or a network error re-warm, re-post
    the current window and try again, giving up after STOP_LOSS in a row."""
    while True:
        try:
            result = call()
            failures[0] = 0
            return result
        except (records_cook.CookCRSError, requests.RequestException) as exc:
            failures[0] += 1
            log.warning("%s failed (%d in a row): %s", what, failures[0], exc)
            if failures[0] >= STOP_LOSS:
                raise Stopped(what) from exc
            time.sleep(min(60, 5 * failures[0]))
            try:
                crs.retry_window()
            except (records_cook.CookCRSError, requests.RequestException) as again:
                log.warning("re-warming failed: %s", again)


def sweep_term(crs: records_cook.CookCRS, term: str, sweep: dict, index: dict[str, dict],
               earliest: date, limit_windows: int | None, failures: list[int]) -> None:
    for name, types, side in PASSES:
        sweep_pass(crs, term, name, list(types), side, sweep, index, earliest, limit_windows, failures)


def sweep_pass(crs: records_cook.CookCRS, term: str, name: str, types: list[str], side: str, sweep: dict,
               index: dict[str, dict], earliest: date, limit_windows: int | None, failures: list[int]) -> None:
    """Walk one term and pass back through the row cap, indexing every row once."""
    label = f"{term} / {name}"
    state = sweep["terms"].setdefault(label, {"windows": 0, "rows": 0, "new": 0, "done": False,
                                              "to": None, "oldest": None, "newest": None, "truncated_days": []})
    if state["done"]:
        log.info("%s: complete (back to %s)", label, state["oldest"])
        return
    to = date.fromisoformat(state["to"]) if state["to"] else date.today()
    done_now = 0
    while True:
        window = (term, records_cook.us_date(earliest), records_cook.us_date(to), types, side)
        page_html, pages = with_retry(crs, lambda window=window: crs.post_window(*window), f"{label} ..{to}",
                                      failures)
        total = records_cook.result_total(page_html)
        rows, _ = records_cook.parse_result_page(page_html)
        for number in range(2, min(pages, records_cook.PAGE_CAP) + 1):
            page_html = with_retry(crs, lambda number=number: crs.page(number), f"{label} ..{to} page {number}",
                                   failures)
            rows += records_cook.parse_result_page(page_html)[0]
        fresh = []
        for row in rows:                      # a document is indexed once, however often it is read
            if row["doc_number"] not in index:
                row["term"], row["pass"] = term, name
                index[row["doc_number"]] = row
                fresh.append(row)
        if fresh:
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            with INDEX_PATH.open("a", encoding="utf-8") as fh:
                for row in fresh:
                    append_row(fh, row)
        days = [day for day in (records_cook.parse_us_date(row["recorded"]) for row in rows) if day]
        following = records_cook.next_cursor(rows, total, to)
        state["windows"] += 1
        state["rows"] += len(rows)
        state["new"] += len(fresh)
        if days:
            state["oldest"] = min(days).isoformat()
            state["newest"] = max(state["newest"] or "", max(days).isoformat())
        log.info("%s ..%s: %s documents (%d rows read, %d new) %s", label, to, total, len(rows), len(fresh),
                 "— complete" if following is None else f"— capped, next ends {following}")
        if following is None:
            state["done"], state["to"] = True, None
        else:
            if days and min(days) == to:        # one day filled the cap: its tail is out of reach
                state["truncated_days"].append(to.isoformat())
            to = following
            state["to"] = to.isoformat()
            if to < earliest:
                state["done"], state["to"] = True, None
        write_json(SWEEP_PATH, sweep)
        done_now += 1
        if state["done"] or (limit_windows and done_now >= limit_windows):
            return


# ---- phase 2: the detail pages --------------------------------------------------

def detail_queue(index: dict[str, dict], mode: str) -> list[dict]:
    """The rows whose detail page should be fetched, most useful first: the
    ones a record cannot be shaped without, then (mode "all") the rest,
    newest first within each group."""
    if mode == "none":
        return []

    def newest(row: dict) -> date:
        return records_cook.parse_us_date(row.get("recorded", "")) or date.min

    def kept(row: dict) -> bool:
        parsed = records_cook.row_to_parsed(row)
        return not records_cook.needs_detail(row) and not records_cook.drop_reason(
            row.get("doc_type_label", ""), parsed["filers"], parsed["respondents"])

    missing = [row for row in index.values()
               if row.get("detail_href") and not detail_path(row["doc_number"]).exists()]
    needed = sorted((row for row in missing
                     if records_cook.needs_detail(row) or (kept(row) and records_cook.truncated(row))),
                    key=newest, reverse=True)
    if mode != "all":
        return needed
    first = {row["doc_number"] for row in needed}
    rest = sorted((row for row in missing if kept(row) and row["doc_number"] not in first), key=newest, reverse=True)
    return needed + rest


# ---- shaping --------------------------------------------------------------------

def build_records(index: dict[str, dict], retrieved_at: str) -> tuple[list[dict], dict]:
    """Every document in the index or the detail cache -> (rows, counts)."""
    records: dict[str, dict] = {}
    stats: Counter = Counter()
    documents = set(index) | ({path.stem for path in DETAILS_DIR.glob("*.html")} if DETAILS_DIR.is_dir() else set())
    for doc_number in sorted(documents):
        row = index.get(doc_number)
        path = detail_path(doc_number)
        if path.exists():
            parsed = records_cook.parse_detail(path.read_text(errors="replace"))
            if not parsed["doc_number"]:
                stats["unparsable_detail"] += 1
                continue
            if not parsed["pin"] and row:
                parsed["pin"] = row.get("pin", "")
            stats["from_detail_page"] += 1
            from_detail = True
        elif row is None:
            continue
        elif records_cook.needs_detail(row):
            stats["awaiting_detail_page"] += 1
            continue
        else:
            parsed = records_cook.row_to_parsed(row)
            stats["from_results_row"] += 1
            from_detail = False
        why = records_cook.drop_reason(parsed["doc_type"], parsed["filers"], parsed["respondents"])
        if why:
            stats["dropped_" + why] += 1
            continue
        record = records_cook.to_record(parsed, retrieved_at)
        if not record or not record.doc_id or not record.year:
            stats["dropped_no_date_or_number"] += 1
            continue
        record.extras["executed_date"] = records_cook.iso_date(parsed.get("executed") or "")
        record.extras["detail_page"] = from_detail
        records[record.doc_id] = record.as_row()
    rows = sorted(records.values(), key=lambda row: (row["recorded_ymd"], row["doc_id"]), reverse=True)
    return rows, dict(stats)


def write_outputs(index: dict[str, dict], sweep: dict, started: float) -> list[dict]:
    retrieved_at = now_iso()
    rows, stats = build_records(index, retrieved_at)
    write_rows(OUTPUT_PATH, rows)
    years = sorted(row["year"] for row in rows)
    types = Counter(row["doc_type_label"] for row in rows)
    kept_keys = ("done", "windows", "rows", "new", "oldest", "newest", "truncated_days")
    terms = {label: {key: state.get(key) for key in kept_keys} for label, state in sweep.get("terms", {}).items()}
    complete = (all(state["done"] for state in terms.values())
                and set(terms) >= {f"{term} / {name}" for term in TERMS for name, _, _ in PASSES})
    info = {
        "retrieved_at": retrieved_at,
        "source": "Cook County Clerk — Recordings System (CRS), Advanced Search by party name",
        "page": records_cook.SEARCH_PAGE,
        "records": len(rows),
        "associations": len({row["association"] for row in rows}),
        "years": f"{years[0]}-{years[-1]}" if years else "",
        "document_types": dict(types.most_common()),
        "documents_indexed": len(index),
        "counts": stats,
        "terms": terms,
        "sweep_complete": complete,
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("LIEN, CORRECTED LIEN and MECHANICS LIEN documents in which a party name on either side "
                   "carries one of the search terms, and LIS PENDENS FORECLOSURE documents (with the amended "
                   "and corrected forms) in which a grantor's name does. `terms` gives the recording dates "
                   "actually reached per term and pass; one that is not `done` stops at its `oldest`. A "
                   "record with `detail_page: false` was shaped from the results row: it has the first "
                   "grantor and first grantee only and no property address. A lender's foreclosure that "
                   "joins the association as a defendant is not collected."),
    }
    write_json(SOURCES_PATH, info)
    update_coverage(info)
    return rows


def update_coverage(info: dict) -> None:
    """states.IL.counties.Cook.liens in coverage.json, the way the other
    county lien indexes are recorded; the search-only `unavailable` entry
    for the recorder is dropped."""
    if not COVERAGE_PATH.exists() or not info["records"]:
        return
    data = coverage.load(COVERAGE_PATH)
    illinois = data["states"].setdefault("IL", {"name": "Illinois", "collected": {}})
    reached = sorted(state["oldest"] for state in info["terms"].values() if state.get("oldest"))
    partial = [label for label, state in info["terms"].items() if not state["done"]]
    illinois.setdefault("counties", {}).setdefault("Cook", {})["liens"] = {
        "source": info["source"],
        "collector": "cook-liens",
        "coverage": ("party-name sweep: liens naming an association on either side, foreclosures it filed"
                     + (f"; complete back to {reached[0]}" if info["sweep_complete"] and reached
                        else f"; sweep unfinished for {', '.join(partial) or 'some terms'}")
                     + f"; {info['counts'].get('from_results_row', 0):,} records shaped from the results row "
                       "(first parties only, no address); lender foreclosures naming an association not collected"),
        "records": info["records"],
        "associations": info["associations"],
        "years": info["years"],
        "document_types": info["document_types"],
    }
    illinois["unavailable"] = [entry for entry in illinois.get("unavailable", [])
                               if entry.get("source") != SEARCH_ONLY_ENTRY]
    coverage.save(data, COVERAGE_PATH)


# ---- the command ----------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--terms", nargs="+", help="search terms to sweep (default: all of TERMS)")
    parser.add_argument("--no-sweep", action="store_true", help="skip phase 1; use the index as it is")
    parser.add_argument("--details", choices=("needed", "all", "none"), default="needed",
                        help="which detail pages to fetch (default: only where the results row is not enough)")
    parser.add_argument("--limit-windows", type=int, help="stop each term after this many windows (smoke test)")
    parser.add_argument("--limit-details", type=int, help="fetch at most this many detail pages")
    parser.add_argument("--since", default=records_cook.EARLIEST.isoformat(),
                        help="earliest recording date, YYYY-MM-DD")
    parser.add_argument("--pace", type=float, default=MIN_PACE,
                        help=f"seconds between requests (never under {MIN_PACE})")
    parser.add_argument("--fresh", action="store_true",
                        help="forget the sweep cursors and the index (detail pages are kept)")


def run(args: argparse.Namespace) -> int:
    started = time.time()
    if args.fresh:
        for path in (INDEX_PATH, SWEEP_PATH):
            path.unlink(missing_ok=True)
    index, sweep = load_index(), load_sweep()
    queued = 0
    code = 0
    crs = None
    failures = [0]
    try:
        if not args.no_sweep:
            crs = records_cook.CookCRS(pace=max(MIN_PACE, args.pace))
            for term in args.terms or TERMS:
                sweep_term(crs, term, sweep, index, date.fromisoformat(args.since), args.limit_windows, failures)
        queue = detail_queue(index, args.details)
        if args.limit_details is not None:
            queue = queue[:args.limit_details]
        queued = len(queue)
        if queue:
            crs = crs or records_cook.CookCRS(pace=max(MIN_PACE, args.pace))
            crs.window = None
            log.info("fetching %d detail pages", len(queue))
            DETAILS_DIR.mkdir(parents=True, exist_ok=True)
            for i, row in enumerate(queue, 1):
                page_html = with_retry(crs, lambda row=row: crs.detail(row["detail_href"]),
                                       f"detail {row['doc_number']}", failures)
                target = detail_path(row["doc_number"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(page_html)
                if i % SAVE_EVERY == 0:
                    write_outputs(index, sweep, started)
                    log.info("  %d/%d detail pages", i, len(queue))
    except Stopped as exc:
        print(f"\nstopped after {STOP_LOSS} failures in a row ({exc}); progress is saved — run again to resume",
              file=sys.stderr)
        code = 3
    except KeyboardInterrupt:
        print("\ninterrupted; progress is saved — run again to resume", file=sys.stderr)
        code = 130
    rows = write_outputs(index, sweep, started)
    by_type = Counter(row["doc_type"] for row in rows)
    years = sorted(row["year"] for row in rows)
    print(f"\n{len(rows):,} Cook County lien records -> {OUTPUT_PATH.relative_to(ROOT)} "
          f"({len({row['association'] for row in rows}):,} associations, {dict(by_type)}, "
          f"{years[0] if years else '-'}–{years[-1] if years else '-'}; {len(index):,} documents indexed, "
          f"{queued:,} detail pages queued this run)")
    return code
