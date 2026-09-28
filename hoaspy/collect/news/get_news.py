#!/usr/bin/env python
"""Collect news coverage of Florida and Texas community associations via the
GDELT DOC 2.0 API (a free, query-based index of worldwide news).

Two kinds of query:

  sweep     state-level topic searches ("homeowners association" florida
            lawsuit / fraud / receivership / ...) — catches stories about
            associations we don't know to ask about
  targeted  one query per known problem association: the top lien filers from
            the Broward index and the largest delinquent DBPR registrants,
            searched by the distinctive part of their name

    ./venv/bin/python -m hoaspy.collect.news.get_news
    ./venv/bin/python -m hoaspy.collect.news.get_news --no-sweep --top-liens 40   # quick pass

Output (default ./news/): articles.jsonl, one record per distinct URL, with
the queries that surfaced it. Existing articles are kept and merged, so the
collector is safe to re-run.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy import ROOT
DEFAULT_OUT = ROOT / "news"

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
SOURCE_PAGE = "https://www.gdeltproject.org/"
# GDELT's article index begins in 2017; without an explicit start it only
# searches the last 3 months.
START = "20170101000000"

USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"

log = logging.getLogger("news")

SWEEP_TOPICS = [
    "lawsuit", "fraud", "embezzlement", "foreclosure", "receivership",
    '"special assessment"', "investigation", "arrested", "fines",
    '"class action"', "settlement",
]
STATES = {"FL": "florida", "TX": "texas"}

# One phrase per query: GDELT throttles expensive OR-of-phrases queries far
# more aggressively than simple ones, and two cheap queries beat one rejected.
SWEEP_PHRASES = ['"homeowners association"', '"condominium association"']

# Words that carry no identity — stripped when deriving the searchable core of
# an association name. VILLAGE/LAKES/OAKS etc. stay: they are the identity.
GENERIC = {
    "THE", "OF", "A", "AT", "AND", "FOR", "INC", "INCORPORATED", "CORP",
    "ASSOCIATION", "ASSOCIATIONS", "ASSN", "ASSOC", "HOMEOWNERS", "HOMEOWNER",
    "HOME", "OWNERS", "OWNER", "PROPERTY", "COMMUNITY", "MASTER",
    "CONDOMINIUM", "CONDOMINIUMS", "CONDO", "CONDOS", "RESIDENCES",
    "RECREATION", "MAINTENANCE", "NO", "PHASE", "SECTION", "PART", "UNIT",
    "BLDG", "BUILDING",
}
ROMAN = re.compile(r"^[IVXL]{1,6}$")


def core_phrase(name: str) -> str | None:
    """The distinctive part of an association name, or None when nothing
    distinctive is left (searching for it would only return noise)."""
    tokens = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    kept = [t for t in tokens
            if t not in GENERIC and not t.isdigit() and not ROMAN.match(t)]
    core = " ".join(kept[:4])
    if len(kept) >= 2 or (len(kept) == 1 and len(kept[0]) >= 6):
        return core.title()
    return None


class AdaptiveDelay:
    """Find the request interval GDELT actually accepts and sit on it.

    GDELT's 429 message says "one every 5 seconds", but a penalised IP gets a
    much longer effective window. Exponential backoff oscillates around it
    (each recovery immediately re-trips the limiter); climbing an interval
    ladder and only easing off after a run of successes converges instead.
    """

    LEVELS = [7, 15, 30, 60, 90, 150]

    def __init__(self) -> None:
        self.i = 2  # start at 30s: this IP has already been penalised once
        self.streak = 0

    def wait(self) -> None:
        time.sleep(self.LEVELS[self.i] + random.uniform(0, 3))

    def rate_limited(self) -> None:
        if self.i < len(self.LEVELS) - 1:
            self.i += 1
        self.streak = 0
        log.info("gdelt 429 — interval now %ds", self.LEVELS[self.i])

    def succeeded(self) -> None:
        self.streak += 1
        if self.streak >= 6 and self.i > 0:
            self.i -= 1
            self.streak = 0
            log.info("gdelt steady — easing interval to %ds", self.LEVELS[self.i])


def gdelt(session: requests.Session, delay: AdaptiveDelay, query: str,
          maxrecords: int, sort: str, timeout: float = 30.0) -> list[dict]:
    params = {
        "query": query, "mode": "artlist", "format": "json",
        "maxrecords": maxrecords, "startdatetime": START, "sort": sort,
    }
    for attempt in range(8):
        delay.wait()
        try:
            resp = session.get(ENDPOINT, params=params, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("gdelt request failed (%s), retrying", exc)
            delay.rate_limited()
            continue
        if resp.status_code == 429:
            delay.rate_limited()
            continue
        try:
            data = resp.json()
        except ValueError:
            # GDELT signals query errors as plain text with HTTP 200.
            log.debug("non-JSON reply for %r: %s", query, resp.text[:120])
            delay.succeeded()
            return []
        delay.succeeded()
        return data.get("articles") or []
    log.warning("giving up on query %r", query)
    return []


def load_top_lien_filers(path: Path, n: int) -> list[dict]:
    if not path.exists():
        log.warning("%s missing — run get_liens.py first; skipping targeted lien queries", path)
        return []
    rows = []
    with path.open() as fh:
        for row in csv.DictReader(fh):
            rows.append({"name": row["association"], "state": "FL",
                         "weight": int(row["liens"]) + int(row["lis_pendens"])})
    rows.sort(key=lambda r: -r["weight"])
    return rows[:n]


def load_delinquent(path: Path, n: int) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as fh:
        for line in fh:
            r = json.loads(line)
            if (r.get("status_detail") or "").lower() == "delinquent" and r.get("units"):
                rows.append({"name": r["name"], "state": r["state"],
                             "weight": r["units"] or 0})
    rows.sort(key=lambda r: -r["weight"])
    return rows[:n]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--top-liens", type=int, default=120,
                    help="targeted queries for the N most active lien filers")
    ap.add_argument("--top-delinquent", type=int, default=50,
                    help="targeted queries for the N largest delinquent registrants")
    ap.add_argument("--no-sweep", action="store_true")
    ap.add_argument("--sweep-records", type=int, default=100)
    ap.add_argument("--targeted-records", type=int, default=75)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    delay = AdaptiveDelay()

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "articles.jsonl"

    # Merge into whatever a previous run collected.
    articles: dict[str, dict] = {}
    if out_path.exists():
        with out_path.open() as fh:
            for line in fh:
                a = json.loads(line)
                articles[a["url"]] = a
    known_before = len(articles)

    def record(art: dict, kind: str, query: str, association: str, state: str) -> None:
        url = art.get("url") or ""
        if not url or (art.get("language") or "").lower() not in ("english", ""):
            return
        entry = articles.setdefault(url, {
            "url": url,
            "title": " ".join((art.get("title") or "").split()),
            "domain": art.get("domain") or "",
            "date": art.get("seendate") or "",
            "sourcecountry": art.get("sourcecountry") or "",
            "queries": [],
        })
        q = {"kind": kind, "query": query, "association": association, "state": state}
        if q not in entry["queries"]:
            entry["queries"].append(q)

    started = time.time()
    n_queries = 0

    def flush() -> None:
        tmp = out_path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            for a in articles.values():
                fh.write(json.dumps(a, ensure_ascii=False) + "\n")
        tmp.replace(out_path)

    if not args.no_sweep:
        for state, statename in STATES.items():
            for topic in SWEEP_TOPICS:
                for phrase in SWEEP_PHRASES:
                    query = f"{phrase} {statename} {topic}"
                    arts = gdelt(session, delay, query, args.sweep_records, "datedesc")
                    n_queries += 1
                    if n_queries % 10 == 0:
                        flush()
                    for a in arts:
                        record(a, "sweep", query, "", state)
                    log.info("sweep %s %-20s %3d articles (%d distinct so far)",
                             state, topic, len(arts), len(articles))

    targets: list[tuple[dict, str]] = []
    for t in load_top_lien_filers(ROOT / "liens" / "by_association.csv", args.top_liens):
        targets.append((t, "liens"))
    for t in load_delinquent(ROOT / "records" / "associations.jsonl", args.top_delinquent):
        targets.append((t, "delinquent"))

    seen_cores = set()
    for target, why in targets:
        core = core_phrase(target["name"])
        if not core or core.lower() in seen_cores:
            continue
        seen_cores.add(core.lower())
        statename = STATES.get(target["state"], "florida")
        query = f'"{core}" (hoa OR homeowners OR condominium OR condo OR association) {statename}'
        arts = gdelt(session, delay, query, args.targeted_records, "hybridrel")
        n_queries += 1
        if n_queries % 10 == 0:
            flush()
        for a in arts:
            record(a, f"targeted-{why}", query, target["name"], target["state"])
        if arts:
            log.info("targeted %-40s %3d articles", core[:40], len(arts))

    flush()

    (args.out / "sources.json").write_text(json.dumps({
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "GDELT DOC 2.0 API",
        "page": SOURCE_PAGE,
        "queries_run": n_queries,
        "articles_total": len(articles),
        "articles_new": len(articles) - known_before,
        "duration_seconds": round(time.time() - started, 1),
    }, indent=2))

    by_kind = Counter(q["kind"] for a in articles.values() for q in a["queries"])
    print(f"\n{len(articles):,} distinct articles ({len(articles) - known_before:,} new) "
          f"from {n_queries} queries")
    for kind, n in by_kind.most_common():
        print(f"  {kind:<20} {n:>6,} query-hits")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
