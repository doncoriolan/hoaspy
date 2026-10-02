#!/usr/bin/env python
"""Collect court records naming community associations, nationwide, from
CourtListener (Free Law Project): RECAP federal dockets where an association
is a party, and state supreme/appellate opinions with an association in the
caption.

The court list (and each court's state) comes from CourtListener's own courts
API, so all 50 states + DC are covered without a hand-maintained map.

    ./venv/bin/python -m hoaspy.collect.courts.get_courts                  # everything
    ./venv/bin/python -m hoaspy.collect.courts.get_courts --only-opinions  # skip federal dockets

Output (default ./courts/): dockets.jsonl, opinions.jsonl. Existing records
are kept and merged (dedup by docket id / opinion URL), and results are
checkpointed to disk after every query, so the collector is safe to re-run
and safe to kill.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy.lib.pacing import Pacer
from hoaspy.collect.liens.records_broward import ASSOCIATION_RE

from hoaspy import ROOT
DEFAULT_OUT = ROOT / "courts"

ENDPOINT = "https://www.courtlistener.com/api/rest/v4/search/"
COURTS_ENDPOINT = "https://www.courtlistener.com/api/rest/v4/courts/"
BASE_URL = "https://www.courtlistener.com"
SOURCE_PAGE = "https://free.law/recap"

USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
# Longest names first so "West Virginia" wins over "Virginia" and
# "Arkansas" over the "kansas" hiding inside it.
_STATE_BY_LEN = sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[1]))

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

log = logging.getLogger("courts")


def fetch_court_maps(session: requests.Session) -> tuple[dict, dict]:
    """{district_court_id: state}, {appellate_court_id: state} for the whole
    country, from the courts API."""

    def state_of_name(full_name: str) -> str | None:
        for code, name in _STATE_BY_LEN:
            if re.search(r"\b" + re.escape(name) + r"\b", full_name):
                return code
        return None

    def fetch(jurisdiction: str) -> dict:
        out: dict[str, str] = {}
        url, params = COURTS_ENDPOINT, {"jurisdiction": jurisdiction, "page_size": 200}
        while url:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for c in data["results"]:
                if not c.get("in_use"):
                    continue
                if "criminal" in c["full_name"].lower():
                    continue  # no HOA cases in criminal appeals
                state = state_of_name(c["full_name"])
                if state:  # territories and specialty courts fall out here
                    out[c["id"]] = state
            url, params = data.get("next"), None
        return out

    districts = fetch("FD")
    appellate = {**fetch("S"), **fetch("SA")}
    log.info("courts api: %d federal districts, %d state appellate courts",
             len(districts), len(appellate))
    return districts, appellate


def fetch_query(session: requests.Session, pacer: Pacer, query: str,
                max_pages: int, qtype: str, courts: str, timeout: float = 30.0):
    url, params = ENDPOINT, {"q": query, "type": qtype, "court": courts}
    page = 0
    while url and page < max_pages:
        pacer.wait("between_requests")
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("request failed (%s), backing off", exc)
            if not pacer.backoff(query):
                return
            continue
        if resp.status_code == 429:
            if not pacer.backoff(query):
                return
            continue
        resp.raise_for_status()
        pacer.ok()
        data = resp.json()
        page += 1
        yield from data.get("results") or []
        url, params = data.get("next"), None  # cursor URL carries everything
    if url:
        log.info("stopped %r at the %d-page cap with more remaining", query, max_pages)


def load_jsonl(path: Path, key: str) -> dict:
    out = {}
    if path.exists():
        with path.open() as fh:
            for line in fh:
                d = json.loads(line)
                out[d[key]] = d
    return out


def save_jsonl(path: Path, items: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for d in items.values():
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--max-pages", type=int, default=400,
                    help="page cap per query (20 results per page)")
    ap.add_argument("--only-opinions", action="store_true",
                    help="skip the federal-docket queries")
    ap.add_argument("--only-dockets", action="store_true",
                    help="skip the state-opinion queries")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    pacer = Pacer({"between_requests": [1.0, 2.5], "backoff_base": 60,
                   "backoff_max": 900, "max_429_retries": 8})

    districts, appellate = fetch_court_maps(session)
    district_param = " ".join(sorted(districts))
    appellate_param = " ".join(sorted(appellate))

    started = time.time()
    now = datetime.now(timezone.utc).isoformat()

    dockets = load_jsonl(args.out / "dockets.jsonl", "docket_id")
    if not args.only_opinions:
        for query in PARTY_QUERIES:
            found = 0
            for r in fetch_query(session, pacer, query, args.max_pages,
                                 "r", district_param):
                found += 1
                did = r.get("docket_id")
                if not did:
                    continue
                entry = dockets.setdefault(did, {
                    "docket_id": did,
                    "case_name": r.get("caseName") or "",
                    "docket_number": r.get("docketNumber") or "",
                    "court": r.get("court") or "",
                    "court_id": r.get("court_id") or "",
                    "state": districts.get(r.get("court_id") or "", ""),
                    "date_filed": r.get("dateFiled") or "",
                    "date_terminated": r.get("dateTerminated") or "",
                    "nature_of_suit": r.get("suitNature") or "",
                    "cause": r.get("cause") or "",
                    "parties": r.get("party") or [],
                    "associations": sorted({p for p in (r.get("party") or [])
                                            if ASSOCIATION_RE.search(p)}),
                    "url": BASE_URL + (r.get("docket_absolute_url") or ""),
                    "source": "courtlistener-recap",
                    "queries": [],
                    "retrieved_at": now,
                })
                if query not in entry["queries"]:
                    entry["queries"].append(query)
            save_jsonl(args.out / "dockets.jsonl", dockets)
            log.info("%-44s %5d results (%d dockets total) [checkpointed]",
                     query, found, len(dockets))

    opinions = load_jsonl(args.out / "opinions.jsonl", "url")
    if not args.only_dockets:
        for query in OPINION_QUERIES:
            found = 0
            for r in fetch_query(session, pacer, query, args.max_pages,
                                 "o", appellate_param):
                found += 1
                url = BASE_URL + (r.get("absolute_url") or "")
                if url == BASE_URL:
                    continue
                segments = re.split(r"\s+v\.?\s+", r.get("caseName") or "", flags=re.I)
                assocs = sorted({s.strip(" ,") for s in segments
                                 if ASSOCIATION_RE.search(s)})
                entry = opinions.setdefault(url, {
                    "case_name": r.get("caseName") or "",
                    "court": r.get("court") or "",
                    "court_id": r.get("court_id") or "",
                    "state": appellate.get(r.get("court_id") or "", ""),
                    "date_filed": r.get("dateFiled") or "",
                    "associations": assocs,
                    "url": url,
                    "source": "courtlistener-opinions",
                    "queries": [],
                    "retrieved_at": now,
                })
                if query not in entry["queries"]:
                    entry["queries"].append(query)
            save_jsonl(args.out / "opinions.jsonl", opinions)
            log.info("%-44s %5d results (%d opinions total) [checkpointed]",
                     query, found, len(opinions))

    (args.out / "sources.json").write_text(json.dumps({
        "retrieved_at": now,
        "source": "CourtListener (Free Law Project): RECAP dockets + state opinions",
        "page": SOURCE_PAGE,
        "endpoint": ENDPOINT,
        "scope": "all US states + DC",
        "federal_districts": len(districts),
        "state_appellate_courts": len(appellate),
        "queries": PARTY_QUERIES + OPINION_QUERIES,
        "dockets": len(dockets),
        "opinions": len(opinions),
        "duration_seconds": round(time.time() - started, 1),
        "caveat": ("RECAP holds what PACER users have shared; state coverage "
                   "is appellate only — trial-court records have no free "
                   "national index."),
    }, indent=2))

    for label, items in (("dockets", dockets), ("opinions", opinions)):
        by_state = Counter(d["state"] for d in items.values() if d.get("state"))
        top = ", ".join(f"{s} {n:,}" for s, n in by_state.most_common(8))
        print(f"{len(items):,} {label} — top states: {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
