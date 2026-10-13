"""Court records naming community associations, nationwide, from the
CourtListener search API (Free Law Project): RECAP federal dockets where
an association is a party, and state supreme and appellate opinions with
an association in the caption.

    python -m hoaspy courts                     # everything
    python -m hoaspy courts --only-opinions     # skip the federal dockets

The court list, and each court's state, come from CourtListener's own
courts API, so all 50 states and DC are covered without a hand-kept map.
The search API throttles an anonymous client hard (five requests a
minute, then a daily cap); the bulk files (courts-bulk) have no limit.

Output (default courts/): dockets.jsonl (Case records plus `docket_id`,
`court_id` and `parties`) and opinions.jsonl (Opinion records plus
`court_id`). Existing records are kept and merged (by docket id and
opinion URL), and the files are rewritten after every query, so the
collector is safe to re-run and safe to kill.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from collections import Counter
from pathlib import Path

import requests

from hoaspy.collect.courts import COURTS_DIR
from hoaspy.lib import http
from hoaspy.lib.jsonl import now_iso, read_rows, write_json, write_rows
from hoaspy.lib.names import LIEN_PARTY_RE
from hoaspy.lib.pacing import Pacer
from hoaspy.lib.records import Case, Opinion
from hoaspy.lib.states import state_in

SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
COURTS_URL = "https://www.courtlistener.com/api/rest/v4/courts/"
SITE_URL = "https://www.courtlistener.com"
SOURCE_PAGE = "https://free.law/recap"
DOCKET_SOURCE = "courtlistener-recap"
OPINION_SOURCE = "courtlistener-opinions"
PARTY_QUERIES = [
    'party:"homeowners association"',
    'party:"condominium association"',
    'party:"property owners association"',
    'party:"community association"',
    'party:"owners association"',
]
OPINION_QUERIES = [
    'caseName:"homeowners association"',
    'caseName:"condominium association"',
    'caseName:"property owners association"',
    'caseName:"community association"',
    'caseName:"owners association"',
]
PACING = {"between_requests": [1.0, 2.5], "backoff_base": 60, "backoff_max": 900, "max_429_retries": 8}

log = logging.getLogger(__name__)


def fetch_court_maps(session: requests.Session) -> tuple[dict, dict]:
    """({district court id: state}, {appellate court id: state}) for the
    whole country, from the courts API. Criminal appeals courts, territories
    and specialty courts fall out."""

    def fetch(jurisdiction: str) -> dict:
        out: dict[str, str] = {}
        url, params = COURTS_URL, {"jurisdiction": jurisdiction, "page_size": 200}
        while url:
            response = session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            for court in data["results"]:
                if not court.get("in_use") or "criminal" in court["full_name"].lower():
                    continue
                state = state_in(court["full_name"])
                if state:
                    out[court["id"]] = state
            url, params = data.get("next"), None
        return out

    districts = fetch("FD")
    appellate = {**fetch("S"), **fetch("SA")}
    log.info("courts api: %d federal districts, %d state appellate courts", len(districts), len(appellate))
    return districts, appellate


def fetch_query(session: requests.Session, pacer: Pacer, query: str, max_pages: int, kind: str, courts: str,
                timeout: float = 30.0):
    """The results of one search query, page by page, paced and backed off on 429."""
    url, params = SEARCH_URL, {"q": query, "type": kind, "court": courts}
    page = 0
    while url and page < max_pages:
        pacer.wait("between_requests")
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("request failed (%s), backing off", exc)
            if not pacer.backoff(query):
                return
            continue
        if response.status_code == 429:
            if not pacer.backoff(query):
                return
            continue
        response.raise_for_status()
        pacer.ok()
        data = response.json()
        page += 1
        yield from data.get("results") or []
        url, params = data.get("next"), None  # the cursor URL carries everything
    if url:
        log.info("stopped %r at the %d-page cap with more remaining", query, max_pages)


def load_keyed(path: Path, key: str) -> dict:
    """{row[key]: row} for the rows already in `path`."""
    return {row[key]: row for row in read_rows(path)}


def docket_row(hit: dict, districts: dict, retrieved_at: str) -> dict:
    parties = hit.get("party") or []
    return Case(
        case_name=hit.get("caseName") or "", docket_number=hit.get("docketNumber") or "", court=hit.get("court") or "",
        state=districts.get(hit.get("court_id") or "", ""), date_filed=hit.get("dateFiled") or "",
        date_terminated=hit.get("dateTerminated") or "", nature_of_suit=hit.get("suitNature") or "",
        cause=hit.get("cause") or "", associations=sorted({party for party in parties if LIEN_PARTY_RE.search(party)}),
        url=SITE_URL + (hit.get("docket_absolute_url") or ""), source=DOCKET_SOURCE, retrieved_at=retrieved_at,
        extras={"docket_id": hit.get("docket_id"), "court_id": hit.get("court_id") or "", "parties": parties}).as_row()


def opinion_row(hit: dict, url: str, appellate: dict, retrieved_at: str) -> dict:
    segments = re.split(r"\s+v\.?\s+", hit.get("caseName") or "", flags=re.I)
    return Opinion(
        case_name=hit.get("caseName") or "", court=hit.get("court") or "",
        state=appellate.get(hit.get("court_id") or "", ""), date_filed=hit.get("dateFiled") or "",
        associations=sorted({segment.strip(" ,") for segment in segments if LIEN_PARTY_RE.search(segment)}),
        url=url, source=OPINION_SOURCE, retrieved_at=retrieved_at,
        extras={"court_id": hit.get("court_id") or ""}).as_row()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, default=COURTS_DIR)
    parser.add_argument("--max-pages", type=int, default=400, help="page cap per query (20 results per page)")
    parser.add_argument("--only-opinions", action="store_true", help="skip the federal-docket queries")
    parser.add_argument("--only-dockets", action="store_true", help="skip the state-opinion queries")


def run(args: argparse.Namespace) -> int:
    session = http.session()
    pacer = Pacer(PACING)
    districts, appellate = fetch_court_maps(session)
    district_param = " ".join(sorted(districts))
    appellate_param = " ".join(sorted(appellate))
    started = time.time()
    retrieved_at = now_iso()

    dockets = load_keyed(args.out / "dockets.jsonl", "docket_id")
    if not args.only_opinions:
        for query in PARTY_QUERIES:
            found = 0
            for hit in fetch_query(session, pacer, query, args.max_pages, "r", district_param):
                found += 1
                docket_id = hit.get("docket_id")
                if not docket_id:
                    continue
                entry = dockets.setdefault(docket_id, docket_row(hit, districts, retrieved_at))
                if query not in entry["queries"]:
                    entry["queries"].append(query)
            write_rows(args.out / "dockets.jsonl", dockets.values())
            log.info("%-44s %5d results (%d dockets total) [checkpointed]", query, found, len(dockets))

    opinions = load_keyed(args.out / "opinions.jsonl", "url")
    if not args.only_dockets:
        for query in OPINION_QUERIES:
            found = 0
            for hit in fetch_query(session, pacer, query, args.max_pages, "o", appellate_param):
                found += 1
                url = SITE_URL + (hit.get("absolute_url") or "")
                if url == SITE_URL:
                    continue
                entry = opinions.setdefault(url, opinion_row(hit, url, appellate, retrieved_at))
                if query not in entry["queries"]:
                    entry["queries"].append(query)
            write_rows(args.out / "opinions.jsonl", opinions.values())
            log.info("%-44s %5d results (%d opinions total) [checkpointed]", query, found, len(opinions))

    write_json(args.out / "sources.json", {
        "retrieved_at": retrieved_at,
        "source": "CourtListener (Free Law Project): RECAP dockets + state opinions",
        "page": SOURCE_PAGE,
        "endpoint": SEARCH_URL,
        "scope": "all US states + DC",
        "federal_districts": len(districts),
        "state_appellate_courts": len(appellate),
        "queries": PARTY_QUERIES + OPINION_QUERIES,
        "dockets": len(dockets),
        "opinions": len(opinions),
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("RECAP holds what PACER users have shared; state coverage is appellate only — "
                   "trial-court records have no free national index."),
    })
    for label, items in (("dockets", dockets), ("opinions", opinions)):
        by_state = Counter(row["state"] for row in items.values() if row.get("state"))
        top = ", ".join(f"{state} {count:,}" for state, count in by_state.most_common(8))
        print(f"{len(items):,} {label} — top states: {top}")
    return 0
