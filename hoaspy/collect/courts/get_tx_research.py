"""Texas trial-court cases for community associations, through re:SearchTX.

    python -m hoaspy tx-research --cookie-file tx_cookie.txt
    python -m hoaspy tx-research --cookie-file tx_cookie.txt --limit 200
    python -m hoaspy tx-research --cookie-file tx_cookie.txt --names names.txt

CourtListener holds Texas *appellate* opinions, but almost all HOA
litigation is in county and district *trial* courts, which it does not
index. The state's portal does: re:SearchTX (the Office of Court
Administration's research site, run on Tyler's re:Search) exposes every
participating county's trial-court docket through one JSON search API.
This collector drives that API by association name.

1. **Auth is your own logged-in research session.** re:SearchTX requires
   a (free) registered account; a search runs against the signed-in
   user's subscription. Copy the whole Cookie header from a logged-in
   browser tab (DevTools -> Network -> any /CourtRecordsSearch/search
   request -> Copy value) into a file and pass `--cookie-file`, or set
   HW_TXRESEARCH_COOKIE. It carries the session, a signed JWT and the AWS
   WAF token; it is personal and short-lived, so it is never committed.
   The JWT expires in hours: re-copy when the run starts answering 401/403.
2. **Deep paging is capped (~1,000 results a query).** A bare full-text
   term such as "homeowners association" reports tens of thousands of
   hits but only pages through the first 1,000, and full-text is noisy
   (it pulls in tax suits that merely mention an HOA). So the query is
   **by association name**, one quoted name at a time, which stays far
   under the cap and matches precisely. The names come from the Texas
   rows of records/associations.jsonl (the TREC management certificates).
3. **A name query still returns cross-references,** so a hit is kept
   only when one of its own party names is association-shaped AND matches
   the queried association.

The API (reverse-engineered; no bulk feed is offered):

    POST /CourtRecordsSearch/search?timeZoneOffsetInMinutes=<tz>
         {"queryString": "<term>", "searchIndexType": "Cases",
          "pageSize": <n>, "pageNumber": <1-based>}
      -> {"result": {"searchResults": {"actualTotal", "paginationTotal",
                                       "hits": [ <case> ... ]}}}

Each hit carries the jurisdiction, case number, category and type codes,
filing date, status, a caseDataID (the deep-link key) and a parties list.

Output: courts/tx_research.jsonl in the shared docket shape and a
`tx_research` entry in courts/sources.json. Checkpoint:
courts/.tx_research_partial.jsonl and courts/.tx_research_done.txt.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.courts import COURTS_DIR
from hoaspy.lib.checkpoint import NameCheckpoint
from hoaspy.lib.http import BROWSER_USER_AGENT
from hoaspy.lib.jsonl import merge_json, now_iso, read_rows, write_rows
from hoaspy.lib.records import Case

OUTPUT_NAME = "tx_research.jsonl"
DEFAULT_NAMES_PATH = ROOT / "records" / "associations.jsonl"
CHECKPOINT_PREFIX = "tx_research"
PORTAL_URL = "https://research.txcourts.gov/CourtRecordsSearch"
SEARCH_URL = f"{PORTAL_URL}/search"
CASE_URL = f"{PORTAL_URL}/ui/case/{{}}"       # the official deep link, keyed by caseDataID
SOURCE = "researchtx"
COOKIE_ENV = "HW_TXRESEARCH_COOKIE"
PAGE_SIZE = 50
MAX_PAGES = 20
EXIT_EXPIRED, EXIT_QUOTA, EXIT_INTERRUPTED = 3, 4, 130

# The lien vocabulary (hoaspy/lib/names.py) plus the "OWNERS ASSOCIATION"
# forms Texas captions use, so every source classifies a community alike.
PARTY_RE = re.compile(
    r"(HOMEOWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNER|MASTER ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|\bHOA\b|TOWNHOUSE|TOWNHOME|VILLAS? OF|"
    r"OWNERS ASSOCIATION|OWNERS ASSN|ASSOCIATION,? INC|ASSN,? INC|\bASSN\b)", re.I)
# re:Search wraps matched terms in <b><mark>..</mark></b>.
HIGHLIGHT_TAGS_RE = re.compile(r"</?(?:b|mark)>")
# query_string operators re:Search/OpenSearch would choke on inside a name.
QUERY_UNSAFE_RE = re.compile(r'["\\/:~^?*!(){}\[\]<>|&+]')

log = logging.getLogger(__name__)


def clean(text: str) -> str:
    """The text without the highlight tags re:Search injects, HTML entities
    decoded (`&#x2F;`, `&#x27;` …), whitespace squashed."""
    return re.sub(r"\s+", " ", html.unescape(HIGHLIGHT_TAGS_RE.sub("", text or ""))).strip()


def normalize(name: str) -> str:
    """A loose key for matching a party name to the queried name: upper
    case, punctuation to spaces, whitespace squashed."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", (name or "").upper())).strip()


def association_parties(hit: dict) -> list[str]:
    """The association-shaped party names on a case (distinct, tags stripped)."""
    out, seen = [], set()
    for party in hit.get("parties") or []:
        name = clean(party.get("name", ""))
        if name and PARTY_RE.search(name) and name.upper() not in seen:
            seen.add(name.upper())
            out.append(name)
    return out


def to_record(hit: dict, queried: str) -> Case | None:
    """One re:Search hit as a docket record, or None when no
    association-shaped party is present (full-text cross-reference noise)
    or none shares the queried name's core — so a busy caption does not
    graft the case onto every association it happens to name."""
    associations = association_parties(hit)
    if not associations:
        return None
    wanted = normalize(queried)
    matched = [name for name in associations
               if normalize(name) == wanted or wanted in normalize(name) or normalize(name) in wanted]
    if not matched:
        return None
    case_id = hit.get("caseDataID") or hit.get("ofsCaseDataID") or ""
    category, kind = clean(hit.get("caseCategoryCode") or ""), clean(hit.get("caseTypeCode") or "")
    return Case(
        case_name=clean(hit.get("description") or ""), court=clean(hit.get("jurisdiction") or ""),
        docket_number=clean(hit.get("caseNumber") or ""), date_filed=(hit.get("dateFiled") or "")[:10],
        nature_of_suit=" — ".join(part for part in (category, kind) if part), cause=clean(hit.get("status") or ""),
        state="TX", associations=matched, url=CASE_URL.format(case_id) if case_id else PORTAL_URL,
        case_data_id=case_id, source=SOURCE, queries=[queried], retrieved_at=now_iso())


def open_session(cookie: str) -> requests.Session:
    """A session carrying the browser's Cookie header and the site's own request headers."""
    cookie = cookie.strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    if not cookie:
        raise ValueError("empty re:SearchTX cookie")
    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_USER_AGENT, "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json", "Origin": "https://research.txcourts.gov",
        "Referer": f"{PORTAL_URL}/ui/advancedSearch", "x-show-loading-spinner": "true", "Cookie": cookie,
    })
    return session


class QuotaError(Exception):
    """The subscription's hourly search quota is exhausted (HTTP 429);
    carries the server's Retry-After in seconds."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"re:SearchTX quota exceeded; retry after {retry_after}s")


def search(session: requests.Session, query: str, page: int, size: int, tz: int = 0) -> dict:
    """One page of search results. 401/403 or a non-JSON answer (an expired
    session's login page) raise PermissionError; a 429 raises QuotaError;
    a 5xx is retried three times."""
    body = {"queryString": query, "searchIndexType": "Cases", "pageSize": size, "pageNumber": page}
    url = f"{SEARCH_URL}?timeZoneOffsetInMinutes={tz}"
    last = None
    for attempt in range(3):
        response = session.post(url, data=json.dumps(body), timeout=90)
        if response.status_code in (401, 403):
            raise PermissionError(f"{response.status_code} from re:SearchTX — the session cookie/JWT has "
                                  "expired; re-copy the Cookie header from a logged-in browser tab.")
        if response.status_code == 429:
            try:
                retry_after = int(response.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
            raise QuotaError(retry_after)
        if response.status_code < 500:
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise PermissionError("re:SearchTX returned a non-JSON response (likely a login redirect) — "
                                      "the session cookie/JWT has expired; provide a fresh Cookie header.") from exc
            return data.get("result", {}).get("searchResults", {}) or {}
        last = response
        time.sleep(1.5 * (attempt + 1))
    last.raise_for_status()            # a persistent 5xx -> HTTPError (caught per name)
    return {}


def query_for(name: str) -> str:
    """A safe phrase query for an association name: punctuation that means
    something in the query_string grammar (slashes, quotes, colons, hyphens
    as negation …) becomes spaces, then the bare terms are quoted as a phrase."""
    bare = QUERY_UNSAFE_RE.sub(" ", clean(name)).replace("-", " ")
    bare = re.sub(r"\s+", " ", bare).strip()
    return f'"{bare}"' if bare else ""


def fetch_for_name(session: requests.Session, name: str, pace: float, size: int = PAGE_SIZE,
                   max_pages: int = MAX_PAGES) -> list[dict]:
    """Every Texas case whose caption matches `name`, one row per caseDataID."""
    query = query_for(name)
    if not query:
        return []
    out: dict[str, dict] = {}
    page = 1
    while page <= max_pages:
        results = search(session, query, page, size)
        hits = results.get("hits") or []
        for hit in hits:
            case = to_record(hit, name)
            if case:
                out[case.case_data_id or case.docket_number] = case.as_row()
        total = results.get("paginationTotal") or 0
        if len(hits) < size or page * size >= total:
            break
        page += 1
        time.sleep(pace)
    return list(out.values())


def tx_association_names(path: Path, limit: int | None = None) -> list[str]:
    """The distinct Texas association names in the registry rows, longest first."""
    seen: set[str] = set()
    names: list[str] = []
    for row in read_rows(path, skip_bad=True):
        if row.get("state") != "TX":
            continue
        name = (row.get("name") or "").strip()
        if name and name.upper() not in seen and PARTY_RE.search(name):
            seen.add(name.upper())
            names.append(name)
    names.sort(key=len, reverse=True)
    return names[:limit] if limit else names


def write_outputs(rows: list[dict], out_dir: Path) -> Path:
    """courts/tx_research.jsonl and the tx_research entry in courts/sources.json.
    An aborted or quota-blocked run that found nothing leaves the prior output intact."""
    out = out_dir / OUTPUT_NAME
    if not rows:
        if out.exists() and out.stat().st_size > 0:
            log.warning("no records this run — keeping existing %s untouched", out.name)
        return out
    write_rows(out, rows)
    merge_json(out_dir / "sources.json", "tx_research", {
        "name": "re:SearchTX (Texas OCA statewide court records)",
        "url": PORTAL_URL,
        "access": "registered research subscription; per-association name search",
        "coverage": "participating TX county/district trial courts, statewide",
        "caveat": "partial by construction — name-match recall + ~1000-row deep-paging cap; not a complete index",
        "records": len(rows),
        "retrieved_at": now_iso(),
    })
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cookie", help="the full re:SearchTX Cookie header value")
    parser.add_argument("--cookie-file", type=Path, help="file holding the Cookie header (preferred)")
    parser.add_argument("--names", type=Path, help="file of association names, one per line")
    parser.add_argument("--assoc-file", type=Path, default=DEFAULT_NAMES_PATH, help="source of TX association names")
    parser.add_argument("--limit", type=int, help="cap the number of names queried")
    parser.add_argument("--pace", type=float, default=0.5, help="seconds between requests")
    parser.add_argument("--out", type=Path, default=COURTS_DIR)
    parser.add_argument("--tz", type=int, default=0, help="timeZoneOffsetInMinutes (cosmetic; the server uses UTC)")
    parser.add_argument("--fresh", action="store_true", help="ignore any checkpoint and start over")
    parser.add_argument("--wait-on-quota", action="store_true",
                        help="on the 200/hr quota (HTTP 429), sleep Retry-After and keep going instead of "
                             "stopping (a full sweep is many hours; the run stays checkpointed either way)")


def run(args: argparse.Namespace) -> int:
    cookie = args.cookie or os.environ.get(COOKIE_ENV, "")
    if args.cookie_file:
        cookie = args.cookie_file.read_text()
    if not cookie.strip():
        print(f"Need a re:SearchTX cookie: --cookie-file, --cookie, or {COOKIE_ENV}. See the module docstring.",
              file=sys.stderr)
        return 2
    session = open_session(cookie)

    if args.names:
        names = [line.strip() for line in args.names.read_text().splitlines() if line.strip()]
    else:
        if not args.assoc_file.exists():
            print(f"{args.assoc_file} not found — run the records command first, or pass --names.", file=sys.stderr)
            return 1
        names = tx_association_names(args.assoc_file, args.limit)
    if args.limit:
        names = names[:args.limit]
    if not names:
        print("No TX association names to query.", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    checkpoint = NameCheckpoint(args.out, CHECKPOINT_PREFIX)
    if args.fresh:
        checkpoint.clear()
    rows, done = checkpoint.load()
    todo = [name for name in names if name not in done]
    log.info("%d TX association names (%d already done, %d to query)", len(names), len(names) - len(todo), len(todo))

    def fetch(name: str) -> list[dict]:
        """fetch_for_name, with a quota hit either slept off (--wait-on-quota) or re-raised."""
        while True:
            try:
                return fetch_for_name(session, name, args.pace)
            except QuotaError as exc:
                if not args.wait_on_quota:
                    raise
                nap = max(exc.retry_after, 60) + 15
                log.warning("quota reached — sleeping %ds (Retry-After) then resuming", nap)
                time.sleep(nap)

    completed = True
    code = 0
    try:
        with checkpoint:
            for i, name in enumerate(todo, 1):
                try:
                    found = fetch(name)
                except PermissionError as exc:
                    print(f"\n{exc}\nProgress is checkpointed — rerun the same command with a fresh cookie to resume.",
                          file=sys.stderr)
                    completed, code = False, EXIT_EXPIRED
                    break
                except QuotaError as exc:
                    print(f"\n{exc}\nHourly search quota (200/hr) reached — progress is checkpointed. Rerun to "
                          "resume, or add --wait-on-quota to trickle through automatically.", file=sys.stderr)
                    completed, code = False, EXIT_QUOTA
                    break
                except Exception as exc:
                    # One bad name (a server 5xx, an odd query) must not end the run;
                    # left out of the done file so a rerun retries it.
                    log.warning("  [%d/%d] %s -> skipped (%s)", i, len(todo), name, type(exc).__name__)
                    continue
                rows.extend(found)
                checkpoint.add(name, found)
                if i % 25 == 0 or found:
                    log.info("  [%d/%d] %s -> %d case(s)", i, len(todo), name, len(found))
                time.sleep(args.pace)
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed; rerun to resume.", file=sys.stderr)
        return EXIT_INTERRUPTED

    # Always write what there is, so the site can use it; the checkpoint
    # is cleared only when the whole list finished.
    out = write_outputs(rows, args.out)
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    log.info("wrote %s — %d cases across %d associations, %d courts", shown, len(rows),
             len({name for row in rows for name in row["associations"]}), len({row["court"] for row in rows}))
    if completed:
        checkpoint.clear()
    else:
        log.info("run paused before finishing — checkpoint kept; rerun to resume")
    return code
