#!/usr/bin/env python
"""Collect Cook County (IL) association liens into liens/liens_cook.jsonl.

Source: the Cook County Clerk's Recordings System, the county recorder's
index (https://crs.cookcountyclerkil.gov/Search). Anonymous — no login, no
captcha. How the site is searched and parsed is in records_cook.py.

    ./venv/bin/python -m hoaspy.collect.liens.get_cook_liens                          # sweep + the detail pages that are needed
    ./venv/bin/python -m hoaspy.collect.liens.get_cook_liens --details all            # …and every other detail page (addresses, co-parties)
    ./venv/bin/python -m hoaspy.collect.liens.get_cook_liens --no-sweep --details none   # rebuild the output from the cache, no network
    ./venv/bin/python -m hoaspy.collect.liens.get_cook_liens --terms "UNIT OWNERS" --limit-windows 1 -v   # smoke test

Two phases, both resumable:

1. **Sweep.** Each search term in TERMS is walked back from today through
   the Advanced Search's 1,000-row windows (records_cook.next_cursor), in
   the two PASSES: the lien types with the term on either side, and the
   lis pendens types with the term on the grantor side only — a lender's
   foreclosure that merely joins the association as a defendant is 70% of
   what an either-side search returns and is not wanted. Every results row
   goes into `.cache/cook_liens/index.jsonl`; `.cache/cook_liens/sweep.json`
   keeps each term-and-pass cursor, so a stopped run continues where it was.
2. **Details.** A row is enough to shape a record when one of its two first
   parties is the association. `--details needed` (the default) fetches the
   detail page where neither is — the association is a co-party — and
   where the results page cut a party name short at 50 characters.
   `--details all` also fetches the rest of the kept documents, newest
   first, for the property address and the full party lists. Pages are cached under `.cache/cook_liens/details/`.

The output is rebuilt from the index and the cache on every stop, so it is
always usable. Paced, sequential, and it stops after STOP_LOSS failures in
a row (exit 3; run it again to resume).

Outputs (git-ignored): liens/liens_cook.jsonl in the shared lien shape
(docs/LIENS.md) and liens/sources_cook.json — per-term date ranges actually
reached, counts, what was dropped and why. coverage.json gets
`states.IL.counties.Cook.liens`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.liens import records_cook as rc

OUT = ROOT / "liens" / "liens_cook.jsonl"
SOURCES = ROOT / "liens" / "sources_cook.json"
COVERAGE = ROOT / "coverage.json"
CACHE = ROOT / ".cache" / "cook_liens"
DETAILS = CACHE / "details"
INDEX = CACHE / "index.jsonl"
SWEEP = CACHE / "sweep.json"

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

log = logging.getLogger("cook_liens")


# -- cache ------------------------------------------------------------------

def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def load_index() -> dict[str, dict]:
    """Document number -> the results row that found it (first sighting wins)."""
    index: dict[str, dict] = {}
    if INDEX.exists():
        for line in INDEX.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                    # a torn last line from a hard stop
            index.setdefault(row["doc_number"], row)
    return index


def load_sweep() -> dict:
    return json.loads(SWEEP.read_text()) if SWEEP.exists() else {"terms": {}}


def detail_path(doc: str) -> Path:
    return DETAILS / f"{doc}.html"


# -- phase 1: the sweep -----------------------------------------------------

class Stopped(RuntimeError):
    """Too many failures in a row; progress is saved."""


def _with_retry(crs: rc.CookCRS, call, what: str, failures: list[int]):
    """Run `call`; on a lost session or a network error re-warm, re-post the
    current window and try again, giving up after STOP_LOSS in a row."""
    while True:
        try:
            out = call()
            failures[0] = 0
            return out
        except (rc.CookCRSError, requests.RequestException) as exc:
            failures[0] += 1
            log.warning("%s failed (%d in a row): %s", what, failures[0], exc)
            if failures[0] >= STOP_LOSS:
                raise Stopped(what) from exc
            time.sleep(min(60, 5 * failures[0]))
            try:
                crs.warm()
                if crs_window := getattr(crs, "_reposted", None):
                    crs.post_window(*crs_window)
            except (rc.CookCRSError, requests.RequestException) as again:
                log.warning("re-warming failed: %s", again)


def sweep_term(crs: rc.CookCRS, term: str, sweep: dict, index: dict[str, dict],
               earliest: date, limit_windows: int | None, failures: list[int]) -> None:
    for name, types, side in PASSES:
        sweep_pass(crs, term, name, list(types), side, sweep, index, earliest, limit_windows, failures)


def sweep_pass(crs: rc.CookCRS, term: str, name: str, types: list[str], side: str, sweep: dict,
               index: dict[str, dict], earliest: date, limit_windows: int | None,
               failures: list[int]) -> None:
    label = f"{term} / {name}"
    st = sweep["terms"].setdefault(label, {"windows": 0, "rows": 0, "new": 0, "done": False,
                                           "to": None, "oldest": None, "newest": None,
                                           "truncated_days": []})
    if st["done"]:
        log.info("%s: complete (back to %s)", label, st["oldest"])
        return
    to = date.fromisoformat(st["to"]) if st["to"] else date.today()
    done_now = 0
    while True:
        window = (term, rc.us_date(earliest), rc.us_date(to), types, side)
        crs._reposted = window
        html, npages = _with_retry(crs, lambda: crs.post_window(*window), f"{label} ..{to}", failures)
        total = rc.result_total(html)
        rows, _ = rc.parse_result_page(html)
        for n in range(2, min(npages, rc.PAGE_CAP) + 1):
            page = _with_retry(crs, lambda n=n: crs.page(n), f"{label} ..{to} page {n}", failures)
            rows += rc.parse_result_page(page)[0]
        fresh = []
        for r in rows:                      # a document is indexed once, however often it is read
            if r["doc_number"] not in index:
                r["term"], r["pass"] = term, name
                index[r["doc_number"]] = r
                fresh.append(r)
        if fresh:
            INDEX.parent.mkdir(parents=True, exist_ok=True)
            with INDEX.open("a") as fh:
                for r in fresh:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        days = [d for d in (rc.parse_us_date(r["recorded"]) for r in rows) if d]
        nxt = rc.next_cursor(rows, total, to)
        st["windows"] += 1
        st["rows"] += len(rows)
        st["new"] += len(fresh)
        if days:
            st["oldest"] = min(days).isoformat()
            st["newest"] = max(st["newest"] or "", max(days).isoformat())
        log.info("%s ..%s: %s documents (%d rows read, %d new) %s", label, to, total, len(rows),
                 len(fresh), "— complete" if nxt is None else f"— capped, next ends {nxt}")
        if nxt is None:
            st["done"], st["to"] = True, None
        else:
            if days and min(days) == to:        # one day filled the cap: its tail is out of reach
                st["truncated_days"].append(to.isoformat())
            to = nxt
            st["to"] = to.isoformat()
            if to < earliest:
                st["done"], st["to"] = True, None
        _atomic(SWEEP, json.dumps(sweep, indent=1))
        done_now += 1
        if st["done"] or (limit_windows and done_now >= limit_windows):
            return


# -- phase 2: detail pages --------------------------------------------------

def detail_queue(index: dict[str, dict], mode: str) -> list[dict]:
    """Rows whose detail page should be fetched, most useful first: the ones
    a record cannot be shaped without, then (mode "all") the rest, newest
    first within each group."""
    if mode == "none":
        return []
    newest = lambda r: rc.parse_us_date(r.get("recorded", "")) or date.min   # noqa: E731
    missing = [r for r in index.values() if r.get("detail_href") and not detail_path(r["doc_number"]).exists()]
    kept = lambda r: not rc.needs_detail(r) and not rc.drop_reason(      # noqa: E731
        r.get("doc_type_label", ""), *(rc.row_to_parsed(r)[k] for k in ("filers", "respondents")))
    needed = sorted((r for r in missing if rc.needs_detail(r) or (kept(r) and rc.truncated(r))),
                    key=newest, reverse=True)
    if mode != "all":
        return needed
    first = {r["doc_number"] for r in needed}
    rest = sorted((r for r in missing if kept(r) and r["doc_number"] not in first), key=newest, reverse=True)
    return needed + rest


# -- shaping ----------------------------------------------------------------

def build_records(index: dict[str, dict], retrieved_at: str) -> tuple[list[dict], dict]:
    """Every document in the index or the detail cache -> records + counts."""
    records: dict[str, dict] = {}
    stats: Counter = Counter()
    docs = set(index) | {p.stem for p in DETAILS.glob("*.html")} if DETAILS.is_dir() else set(index)
    for doc in sorted(docs):
        row = index.get(doc)
        path = detail_path(doc)
        if path.exists():
            parsed = rc.parse_detail(path.read_text(errors="replace"))
            if not parsed["doc_number"]:
                stats["unparsable_detail"] += 1
                continue
            if not parsed["pin"] and row:
                parsed["pin"] = row.get("pin", "")
            stats["from_detail_page"] += 1
            detail = True
        elif row is None:
            continue
        elif rc.needs_detail(row):
            stats["awaiting_detail_page"] += 1
            continue
        else:
            parsed = rc.row_to_parsed(row)
            stats["from_results_row"] += 1
            detail = False
        why = rc.drop_reason(parsed["doc_type"], parsed["filers"], parsed["respondents"])
        if why:
            stats["dropped_" + why] += 1
            continue
        rec = rc.to_record(parsed, retrieved_at)
        if not rec or not rec["doc_id"] or not rec["year"]:
            stats["dropped_no_date_or_number"] += 1
            continue
        rec["executed_date"] = rc.iso_date(parsed.get("executed") or "")
        rec["detail_page"] = detail
        records[rec["doc_id"]] = rec
    out = sorted(records.values(), key=lambda r: (r["recorded_ymd"], r["doc_id"]), reverse=True)
    return out, dict(stats)


def write_outputs(index: dict[str, dict], sweep: dict, started: float) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    records, stats = build_records(index, now)
    _atomic(OUT, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    years = sorted(r["year"] for r in records)
    types = Counter(r["doc_type_label"] for r in records)
    terms = {t: {k: s.get(k) for k in ("done", "windows", "rows", "new", "oldest", "newest", "truncated_days")}
             for t, s in sweep.get("terms", {}).items()}
    complete = (all(s["done"] for s in terms.values())
                and set(terms) >= {f"{t} / {name}" for t in TERMS for name, _, _ in PASSES})
    info = {
        "retrieved_at": now,
        "source": "Cook County Clerk — Recordings System (CRS), Advanced Search by party name",
        "page": rc.SEARCH_PAGE,
        "records": len(records),
        "associations": len({r["association"] for r in records}),
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
    _atomic(SOURCES, json.dumps(info, indent=2))
    update_coverage(info)
    return records


def update_coverage(info: dict) -> None:
    """states.IL.counties.Cook.liens in coverage.json, the way the other
    county lien indexes are recorded."""
    if not COVERAGE.exists() or not info["records"]:
        return
    cov = json.loads(COVERAGE.read_text())
    il = cov.setdefault("states", {}).setdefault("IL", {"name": "Illinois", "collected": {}})
    reached = sorted(s["oldest"] for s in info["terms"].values() if s.get("oldest"))
    partial = [t for t, s in info["terms"].items() if not s["done"]]
    il.setdefault("counties", {}).setdefault("Cook", {})["liens"] = {
        "source": info["source"],
        "collector": "get_cook_liens.py",
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
    il["unavailable"] = [u for u in il.get("unavailable", [])
                         if u.get("source") != "Cook County Clerk current recordings"]
    cov["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic(COVERAGE, json.dumps(cov, indent=2))


# -- run --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--terms", nargs="+", help="search terms to sweep (default: all of TERMS)")
    ap.add_argument("--no-sweep", action="store_true", help="skip phase 1; use the index as it is")
    ap.add_argument("--details", choices=("needed", "all", "none"), default="needed",
                    help="which detail pages to fetch (default: only where the results row is not enough)")
    ap.add_argument("--limit-windows", type=int, help="stop each term after this many windows (smoke test)")
    ap.add_argument("--limit-details", type=int, help="fetch at most this many detail pages")
    ap.add_argument("--since", default=rc.EARLIEST.isoformat(), help="earliest recording date, YYYY-MM-DD")
    ap.add_argument("--pace", type=float, default=1.5, help="seconds between requests (never under 1.5)")
    ap.add_argument("--fresh", action="store_true", help="forget the sweep cursors and the index (detail pages are kept)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    started = time.time()
    if args.fresh:
        for p in (INDEX, SWEEP):
            p.unlink(missing_ok=True)
    index, sweep = load_index(), load_sweep()
    queue_len = 0
    code = 0
    crs = None
    failures = [0]
    try:
        if not args.no_sweep:
            crs = rc.CookCRS(pace=max(1.5, args.pace))
            for term in args.terms or TERMS:
                sweep_term(crs, term, sweep, index, date.fromisoformat(args.since), args.limit_windows, failures)
        queue = detail_queue(index, args.details)
        if args.limit_details is not None:
            queue = queue[:args.limit_details]
        queue_len = len(queue)
        if queue:
            crs = crs or rc.CookCRS(pace=max(1.5, args.pace))
            crs._reposted = None
            log.info("fetching %d detail pages", len(queue))
            DETAILS.mkdir(parents=True, exist_ok=True)
            for i, row in enumerate(queue, 1):
                page = _with_retry(crs, lambda row=row: crs.detail(row["detail_href"]),
                                   f"detail {row['doc_number']}", failures)
                _atomic(detail_path(row["doc_number"]), page)
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
    records = write_outputs(index, sweep, started)
    by_type = Counter(r["doc_type"] for r in records)
    years = sorted(r["year"] for r in records)
    print(f"\n{len(records):,} Cook County lien records -> {OUT.relative_to(ROOT)} "
          f"({len({r['association'] for r in records}):,} associations, {dict(by_type)}, "
          f"{years[0] if years else '-'}–{years[-1] if years else '-'}; {len(index):,} documents indexed, "
          f"{queue_len:,} detail pages queued this run)")
    return code


if __name__ == "__main__":
    sys.exit(main())
