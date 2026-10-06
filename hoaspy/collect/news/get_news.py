"""News coverage of Florida and Texas community associations, from the
GDELT DOC 2.0 API (a free, query-based index of worldwide news). Retired
2026-09-07; kept for reference.

    python -m hoaspy news
    python -m hoaspy news --no-sweep --top-liens 40    # a quick pass

Two kinds of query:

    sweep     state-level topic searches ("homeowners association" florida
              lawsuit / fraud / receivership …) — stories about associations
              nobody knew to ask about
    targeted  one query per known problem association: the top lien filers
              from the Broward index and the largest delinquent DBPR
              registrants, searched by the distinctive part of their name

Output (default news/): articles.jsonl, one Article record per distinct
URL (plus `sourcecountry`) with the queries that surfaced it. Existing
articles are kept and merged, so the collector is safe to re-run.
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import time
from collections import Counter
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.news import NEWS_DIR
from hoaspy.lib import http
from hoaspy.lib.jsonl import now_iso, read_rows, write_json, write_rows
from hoaspy.lib.records import Article

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SOURCE_PAGE = "https://www.gdeltproject.org/"
FIRST_DATETIME = "20170101000000"    # GDELT's article index begins in 2017; without a start it searches 3 months
LIEN_FILERS_PATH = ROOT / "liens" / "by_association.csv"
REGISTRY_PATH = ROOT / "records" / "associations.jsonl"
SWEEP_TOPICS = [
    "lawsuit", "fraud", "embezzlement", "foreclosure", "receivership",
    '"special assessment"', "investigation", "arrested", "fines",
    '"class action"', "settlement",
]
STATES = {"FL": "florida", "TX": "texas"}
# One phrase per query: GDELT throttles OR-of-phrases queries far more
# aggressively than simple ones, and two cheap queries beat one rejected.
SWEEP_PHRASES = ['"homeowners association"', '"condominium association"']
# Words that carry no identity, dropped when deriving the searchable core
# of an association name. VILLAGE, LAKES, OAKS … stay: they are the identity.
GENERIC_WORDS = {
    "THE", "OF", "A", "AT", "AND", "FOR", "INC", "INCORPORATED", "CORP",
    "ASSOCIATION", "ASSOCIATIONS", "ASSN", "ASSOC", "HOMEOWNERS", "HOMEOWNER",
    "HOME", "OWNERS", "OWNER", "PROPERTY", "COMMUNITY", "MASTER",
    "CONDOMINIUM", "CONDOMINIUMS", "CONDO", "CONDOS", "RESIDENCES",
    "RECREATION", "MAINTENANCE", "NO", "PHASE", "SECTION", "PART", "UNIT",
    "BLDG", "BUILDING",
}
ROMAN_RE = re.compile(r"^[IVXL]{1,6}$")
FLUSH_EVERY = 10

log = logging.getLogger(__name__)


def core_phrase(name: str) -> str | None:
    """The distinctive part of an association name, or None when nothing
    distinctive is left (searching for it would only return noise)."""
    tokens = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    kept = [token for token in tokens
            if token not in GENERIC_WORDS and not token.isdigit() and not ROMAN_RE.match(token)]
    core = " ".join(kept[:4])
    if len(kept) >= 2 or (len(kept) == 1 and len(kept[0]) >= 6):
        return core.title()
    return None


class AdaptiveDelay:
    """Find the request interval GDELT accepts and sit on it. GDELT's 429
    message says "one every 5 seconds", but a penalised IP gets a much
    longer effective window. Exponential backoff oscillates around it (each
    recovery immediately re-trips the limiter); climbing an interval ladder
    and only easing off after a run of successes converges instead."""

    LEVELS = [7, 15, 30, 60, 90, 150]
    EASE_AFTER = 6

    def __init__(self) -> None:
        self.level = 2  # start at 30 s
        self.streak = 0

    def wait(self) -> None:
        time.sleep(self.LEVELS[self.level] + random.uniform(0, 3))

    def rate_limited(self) -> None:
        if self.level < len(self.LEVELS) - 1:
            self.level += 1
        self.streak = 0
        log.info("gdelt 429 — interval now %ds", self.LEVELS[self.level])

    def succeeded(self) -> None:
        self.streak += 1
        if self.streak >= self.EASE_AFTER and self.level > 0:
            self.level -= 1
            self.streak = 0
            log.info("gdelt steady — easing interval to %ds", self.LEVELS[self.level])


def query_gdelt(session: requests.Session, delay: AdaptiveDelay, query: str, max_records: int, sort: str,
                timeout: float = 30.0) -> list[dict]:
    """The articles one GDELT query returns; [] after eight failed attempts
    or when GDELT rejects the query (a plain-text answer with HTTP 200)."""
    params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": max_records,
              "startdatetime": FIRST_DATETIME, "sort": sort}
    for _ in range(8):
        delay.wait()
        try:
            response = session.get(API_URL, params=params, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("gdelt request failed (%s), retrying", exc)
            delay.rate_limited()
            continue
        if response.status_code == 429:
            delay.rate_limited()
            continue
        try:
            data = response.json()
        except ValueError:
            log.debug("non-JSON reply for %r: %s", query, response.text[:120])
            delay.succeeded()
            return []
        delay.succeeded()
        return data.get("articles") or []
    log.warning("giving up on query %r", query)
    return []


def load_top_lien_filers(path: Path, count: int) -> list[dict]:
    if not path.exists():
        log.warning("%s missing — run the liens command first; skipping targeted lien queries", path)
        return []
    rows = []
    with path.open() as fh:
        for row in csv.DictReader(fh):
            rows.append({"name": row["association"], "state": "FL",
                         "weight": int(row["liens"]) + int(row["lis_pendens"])})
    rows.sort(key=lambda row: -row["weight"])
    return rows[:count]


def load_delinquent(path: Path, count: int) -> list[dict]:
    rows = [{"name": row["name"], "state": row["state"], "weight": row["units"] or 0}
            for row in read_rows(path)
            if (row.get("status_detail") or "").lower() == "delinquent" and row.get("units")]
    rows.sort(key=lambda row: -row["weight"])
    return rows[:count]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, default=NEWS_DIR)
    parser.add_argument("--top-liens", type=int, default=120,
                        help="targeted queries for the N most active lien filers")
    parser.add_argument("--top-delinquent", type=int, default=50,
                        help="targeted queries for the N largest delinquent registrants")
    parser.add_argument("--no-sweep", action="store_true")
    parser.add_argument("--sweep-records", type=int, default=100)
    parser.add_argument("--targeted-records", type=int, default=75)


def run(args: argparse.Namespace) -> int:
    session = http.session()
    delay = AdaptiveDelay()
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "articles.jsonl"
    retrieved_at = now_iso()

    # Merge into whatever a previous run collected.
    articles: dict[str, dict] = {row["url"]: row for row in read_rows(out_path)}
    known_before = len(articles)
    queries = 0

    def note(article: dict, kind: str, query: str, association: str, state: str) -> None:
        url = article.get("url") or ""
        if not url or (article.get("language") or "").lower() not in ("english", ""):
            return
        entry = articles.setdefault(url, Article(
            url=url, title=" ".join((article.get("title") or "").split()), domain=article.get("domain") or "",
            date=article.get("seendate") or "", retrieved_at=retrieved_at,
            extras={"sourcecountry": article.get("sourcecountry") or ""}).as_row())
        hit = {"kind": kind, "query": query, "association": association, "state": state}
        if hit not in entry["queries"]:
            entry["queries"].append(hit)

    def flush() -> None:
        write_rows(out_path, articles.values())

    started = time.time()
    if not args.no_sweep:
        for state, state_name in STATES.items():
            for topic in SWEEP_TOPICS:
                for phrase in SWEEP_PHRASES:
                    query = f"{phrase} {state_name} {topic}"
                    found = query_gdelt(session, delay, query, args.sweep_records, "datedesc")
                    queries += 1
                    if queries % FLUSH_EVERY == 0:
                        flush()
                    for article in found:
                        note(article, "sweep", query, "", state)
                    log.info("sweep %s %-20s %3d articles (%d distinct so far)",
                             state, topic, len(found), len(articles))

    targets = [(target, "liens") for target in load_top_lien_filers(LIEN_FILERS_PATH, args.top_liens)]
    targets += [(target, "delinquent") for target in load_delinquent(REGISTRY_PATH, args.top_delinquent)]
    seen_cores = set()
    for target, why in targets:
        core = core_phrase(target["name"])
        if not core or core.lower() in seen_cores:
            continue
        seen_cores.add(core.lower())
        state_name = STATES.get(target["state"], "florida")
        query = f'"{core}" (hoa OR homeowners OR condominium OR condo OR association) {state_name}'
        found = query_gdelt(session, delay, query, args.targeted_records, "hybridrel")
        queries += 1
        if queries % FLUSH_EVERY == 0:
            flush()
        for article in found:
            note(article, f"targeted-{why}", query, target["name"], target["state"])
        if found:
            log.info("targeted %-40s %3d articles", core[:40], len(found))
    flush()

    write_json(args.out / "sources.json", {
        "retrieved_at": retrieved_at,
        "source": "GDELT DOC 2.0 API",
        "page": SOURCE_PAGE,
        "queries_run": queries,
        "articles_total": len(articles),
        "articles_new": len(articles) - known_before,
        "duration_seconds": round(time.time() - started, 1),
    })
    by_kind = Counter(hit["kind"] for article in articles.values() for hit in article["queries"])
    print(f"\n{len(articles):,} distinct articles ({len(articles) - known_before:,} new) from {queries} queries")
    for kind, count in by_kind.most_common():
        print(f"  {kind:<20} {count:>6,} query-hits")
    print(f"\nWrote {out_path}")
    return 0
