#!/usr/bin/env python
"""Texas trial-court cases for community associations, via re:SearchTX.

CourtListener gives us TX *appellate* opinions statewide, but almost all HOA
litigation is in county/district *trial* courts, which CourtListener does not
index. Texas' statewide portal does: re:SearchTX (the Office of Court
Administration's public research site, run on Tyler's re:Search) exposes every
participating county's trial-court docket through one JSON search API. This
collector drives that API by association name and writes the same court-record
shape `build_site.py` already ingests (see courts/dockets.jsonl).

    ./venv/bin/python -m hoaspy.collect.courts.get_tx_research --cookie-file tx_cookie.txt
    ./venv/bin/python -m hoaspy.collect.courts.get_tx_research --cookie-file tx_cookie.txt --limit 200
    ./venv/bin/python -m hoaspy.collect.courts.get_tx_research --cookie-file tx_cookie.txt --names names.txt

Three things shape the collector:

1. **Auth is your own logged-in research session, not an anonymous cookie.**
   re:SearchTX requires a (free) registered account; a search runs against the
   signed-in user's subscription. Copy the whole `Cookie:` header from a
   logged-in browser tab (DevTools -> Network -> any /CourtRecordsSearch/search
   request -> Copy value) into a file and pass `--cookie-file`, or set
   HW_TXRESEARCH_COOKIE. It carries the session (FedAuth), a signed JWT
   (RSCH_JWT), and the AWS WAF token; it is personal and short-lived, so it is
   never committed. The JWT expires in hours — re-copy when
   the run starts returning 401/403.

2. **Deep paging is capped (~1000 results per query), like Miami-Dade's 500.**
   A bare full-text term such as "homeowners association" reports tens of
   thousands of hits but only pages through the first 1000, and full-text is
   noisy (it pulls in tax suits that merely mention an HOA). So we query **by
   association name** — the reliable mode — one quoted name at a time, which
   stays far under the cap and matches precisely. Names come from the TX rows of
   records/associations.jsonl (TREC management certificates).

3. **A name query still returns cross-references,** so a hit is kept only when
   one of its own party names is association-shaped (same ASSOCIATION_RE the
   Broward/Miami-Dade lien collectors use) AND matches the queried association.
   `build_site.py` then attaches the case conservatively by normalized name.

The API contract (reverse-engineered; no bulk feed is offered):
    POST /CourtRecordsSearch/search?timeZoneOffsetInMinutes=<tz>
         {"queryString": "<term>", "searchIndexType": "Cases",
          "pageSize": <n>, "pageNumber": <1-based>}
      -> {"result": {"searchResults": {"actualTotal", "paginationTotal",
                                       "hits": [ <case> ... ]}}}
Each hit carries jurisdiction, caseNumber, caseCategoryCode/caseTypeCode,
dateFiled, status, a caseDataID (the deep-link key), and a parties[] list.
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
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy import ROOT
DEFAULT_OUT = ROOT / "courts"
OUT_NAME = "tx_research.jsonl"

BASE = "https://research.txcourts.gov/CourtRecordsSearch"
SEARCH_URL = f"{BASE}/search"
CASE_URL = f"{BASE}/ui/case/{{}}"       # official deep link, keyed by caseDataID
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Same association-name test the lien collectors use (records_miamidade.py),
# so every source classifies a "community" the same way.
ASSOCIATION_RE = re.compile(
    r"(HOMEOWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNER|MASTER ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|\bHOA\b|TOWNHOUSE|TOWNHOME|VILLAS? OF|"
    r"OWNERS ASSOCIATION|OWNERS ASSN|ASSOCIATION,? INC|ASSN,? INC|\bASSN\b)",
    re.I)

# re:Search wraps matched terms in <b><mark>..</mark></b>; strip for clean text.
_TAGS = re.compile(r"</?(?:b|mark)>")
log = logging.getLogger("tx_research")


def clean(s: str) -> str:
    """Drop the highlight tags re:Search injects, decode HTML entities
    (descriptions arrive with `&#x2F;`, `&#x27;` etc.), normalize whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", s or ""))).strip()


def normalize(name: str) -> str:
    """Loose key for matching a party name to the queried name: uppercase,
    punctuation to spaces, collapse. Enough to tell 'CIRCLE C HOMEOWNERS ASSN
    INC' from an unrelated party in the same caption."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", (name or "").upper())).strip()


def association_parties(hit: dict) -> list[str]:
    """Association-shaped party names on a case (deduped, tags stripped)."""
    out, seen = [], set()
    for p in hit.get("parties") or []:
        nm = clean(p.get("name", ""))
        if nm and ASSOCIATION_RE.search(nm) and nm.upper() not in seen:
            seen.add(nm.upper())
            out.append(nm)
    return out


def to_record(hit: dict, queried: str) -> dict | None:
    """Shape one re:Search hit into a courts/*.jsonl docket record, or None if
    no association-shaped party is present (full-text cross-reference noise).

    `associations` — what build_site attaches the case to — is the association
    parties whose name shares the queried name's distinctive core, so a busy
    caption doesn't graft the case onto every association it happens to name.
    """
    assocs = association_parties(hit)
    if not assocs:
        return None
    qn = normalize(queried)
    matched = [a for a in assocs if normalize(a) == qn or qn in normalize(a)
               or normalize(a) in qn]
    if not matched:
        return None
    cid = hit.get("caseDataID") or hit.get("ofsCaseDataID") or ""
    cat = clean(hit.get("caseCategoryCode") or "")
    typ = clean(hit.get("caseTypeCode") or "")
    nature = " — ".join(x for x in (cat, typ) if x)
    return {
        "case_name": clean(hit.get("description") or ""),
        "court": clean(hit.get("jurisdiction") or ""),
        "docket_number": clean(hit.get("caseNumber") or ""),
        "date_filed": (hit.get("dateFiled") or "")[:10],
        "date_terminated": "",
        "nature_of_suit": nature,
        "cause": clean(hit.get("status") or ""),
        "state": "TX",
        "associations": matched,
        "url": CASE_URL.format(cid) if cid else BASE,
        "case_data_id": cid,
        "source": "researchtx",
        "queries": [queried],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def session(cookie: str) -> requests.Session:
    cookie = cookie.strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    if not cookie:
        raise ValueError("empty re:SearchTX cookie")
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://research.txcourts.gov",
        "Referer": f"{BASE}/ui/advancedSearch",
        "x-show-loading-spinner": "true",
        "Cookie": cookie,
    })
    return s


class QuotaError(Exception):
    """The subscription's hourly search quota is exhausted (HTTP 429). Carries
    the server's Retry-After (seconds) so the caller can pause or stop."""
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"re:SearchTX quota exceeded; retry after {retry_after}s")


def search(s: requests.Session, query: str, page: int, size: int,
           tz: int = 0) -> dict:
    body = {"queryString": query, "searchIndexType": "Cases",
            "pageSize": size, "pageNumber": page}
    url = f"{SEARCH_URL}?timeZoneOffsetInMinutes={tz}"
    # The backend 5xxs intermittently (and on some query shapes); retry a few
    # times before giving up on this page.
    last = None
    for attempt in range(3):
        r = s.post(url, data=json.dumps(body), timeout=90)
        if r.status_code in (401, 403):
            raise PermissionError(
                f"{r.status_code} from re:SearchTX — the session cookie/JWT has "
                "expired; re-copy the Cookie header from a logged-in browser tab.")
        if r.status_code == 429:
            # Hard hourly quota ("200 per Hour"). Do NOT burn through the
            # remaining names as errors — surface it so the run pauses/stops
            # and stays resumable from the checkpoint.
            try:
                ra = int(r.headers.get("Retry-After", "0"))
            except ValueError:
                ra = 0
            raise QuotaError(ra)
        if r.status_code < 500:
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                # An expired session often returns a 200 login/redirect page,
                # not a clean 401 — treat non-JSON as an auth failure so the run
                # stops for a fresh cookie instead of skipping every name.
                raise PermissionError(
                    "re:SearchTX returned a non-JSON response (likely a login "
                    "redirect) — the session cookie/JWT has expired; provide a "
                    "fresh Cookie header.")
            return data.get("result", {}).get("searchResults", {}) or {}
        last = r
        time.sleep(1.5 * (attempt + 1))
    last.raise_for_status()            # persistent 5xx -> HTTPError (caught per-name)
    return {}


# query_string operators re:Search/OpenSearch would choke on inside a name.
_QUERY_UNSAFE = re.compile(r'["\\/:~^?*!(){}\[\]<>|&+]')


def query_for(name: str) -> str:
    """A safe phrase query for an association name. Punctuation that carries
    meaning in the query_string grammar (slashes, quotes, colons, hyphens as
    negation, …) is stripped to spaces, then the bare terms are re-quoted as a
    phrase. Recall stays high because build_site re-matches by name anyway."""
    bare = _QUERY_UNSAFE.sub(" ", clean(name)).replace("-", " ")
    bare = re.sub(r"\s+", " ", bare).strip()
    return f'"{bare}"' if bare else ""


def fetch_for_name(s: requests.Session, name: str, pace: float,
                   size: int = 50, max_pages: int = 20) -> list[dict]:
    """Every TX case whose caption matches `name`, deduped by caseDataID.
    Quoted so multi-word names match as a phrase, not as loose OR terms."""
    query = query_for(name)
    if not query:
        return []
    out: dict[str, dict] = {}
    page = 1
    while page <= max_pages:
        sr = search(s, query, page, size)
        hits = sr.get("hits") or []
        for h in hits:
            rec = to_record(h, name)
            if rec:
                out[rec["case_data_id"] or rec["docket_number"]] = rec
        total = sr.get("paginationTotal") or 0
        if len(hits) < size or page * size >= total:
            break
        page += 1
        time.sleep(pace)
    return list(out.values())


def tx_association_names(path: Path, limit: int | None = None) -> list[str]:
    """Distinct TX association names from the TREC registry rows, longest first
    (a fuller name is a tighter phrase query)."""
    seen: set[str] = set()
    names: list[str] = []
    with path.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("state") != "TX":
                continue
            nm = (d.get("name") or "").strip()
            if nm and nm.upper() not in seen and ASSOCIATION_RE.search(nm):
                seen.add(nm.upper())
                names.append(nm)
    names.sort(key=len, reverse=True)
    return names[:limit] if limit else names


# -- checkpointing (a full run is thousands of requests over hours) -----------

def checkpoint_paths(out_dir: Path) -> tuple[Path, Path]:
    return out_dir / ".tx_research_partial.jsonl", out_dir / ".tx_research_done.txt"


def load_checkpoint(out_dir: Path) -> tuple[list[dict], set[str]]:
    part, done = checkpoint_paths(out_dir)
    records: list[dict] = []
    if part.exists():
        for line in part.open():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue                # torn final line from a hard kill
    names: set[str] = set()
    if done.exists():
        names = {l.strip() for l in done.open() if l.strip()}
    return records, names


def clear_checkpoint(out_dir: Path) -> None:
    for p in checkpoint_paths(out_dir):
        p.unlink(missing_ok=True)


def write_outputs(records: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / OUT_NAME
    # Never clobber an existing dataset with an empty result. An aborted or
    # quota-blocked run that collected nothing must leave prior output intact.
    if not records:
        if out.exists() and out.stat().st_size > 0:
            log.warning("no records this run — keeping existing %s untouched",
                        out.name)
        return out
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    src = out_dir / "sources.json"
    catalog = {}
    if src.exists():
        try:
            catalog = json.loads(src.read_text())
        except json.JSONDecodeError:
            catalog = {}
    catalog["tx_research"] = {
        "name": "re:SearchTX (Texas OCA statewide court records)",
        "url": BASE,
        "access": "registered research subscription; per-association name search",
        "coverage": "participating TX county/district trial courts, statewide",
        "caveat": "partial by construction — name-match recall + ~1000-row "
                  "deep-paging cap; not a complete index",
        "records": len(records),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    src.write_text(json.dumps(catalog, indent=2) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie", help="the full re:SearchTX Cookie header value")
    ap.add_argument("--cookie-file", type=Path,
                    help="file holding the Cookie header (preferred)")
    ap.add_argument("--names", type=Path, help="file of association names, one per line")
    ap.add_argument("--assoc-file", type=Path,
                    default=ROOT / "records" / "associations.jsonl",
                    help="source of TX association names")
    ap.add_argument("--limit", type=int, help="cap the number of names queried")
    ap.add_argument("--pace", type=float, default=0.5,
                    help="seconds between requests")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--tz", type=int, default=0,
                    help="timeZoneOffsetInMinutes (cosmetic; server uses UTC)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any checkpoint and start over")
    ap.add_argument("--wait-on-quota", action="store_true",
                    help="on the 200/hr quota (HTTP 429), sleep Retry-After and "
                         "keep going instead of stopping (a full sweep is many "
                         "hours; the run stays checkpointed either way)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    cookie = args.cookie or os.environ.get("HW_TXRESEARCH_COOKIE", "")
    if args.cookie_file:
        cookie = args.cookie_file.read_text()
    if not cookie.strip():
        print("Need a re:SearchTX cookie: --cookie-file, --cookie, or "
              "HW_TXRESEARCH_COOKIE. See the module docstring.", file=sys.stderr)
        return 2
    s = session(cookie)

    if args.names:
        names = [l.strip() for l in args.names.read_text().splitlines() if l.strip()]
    else:
        if not args.assoc_file.exists():
            print(f"{args.assoc_file} not found — run get_records.py first, or "
                  "pass --names.", file=sys.stderr)
            return 1
        names = tx_association_names(args.assoc_file, args.limit)
    if args.limit:
        names = names[:args.limit]
    if not names:
        print("No TX association names to query.", file=sys.stderr)
        return 1

    if args.fresh:
        clear_checkpoint(args.out)
    records, done = load_checkpoint(args.out)
    part_path, done_path = checkpoint_paths(args.out)
    args.out.mkdir(parents=True, exist_ok=True)

    todo = [n for n in names if n not in done]
    log.info("%d TX association names (%d already done, %d to query)",
             len(names), len(names) - len(todo), len(todo))
    def fetch(name):
        """fetch_for_name, but on a quota hit either sleep it off and retry
        (--wait-on-quota) or re-raise so the run stops resumably."""
        while True:
            try:
                return fetch_for_name(s, name, args.pace)
            except QuotaError as exc:
                if not args.wait_on_quota:
                    raise
                nap = max(exc.retry_after, 60) + 15
                log.warning("quota reached — sleeping %ds (Retry-After) then "
                            "resuming", nap)
                time.sleep(nap)

    completed = True
    stop_code = 0
    try:
        with part_path.open("a") as part_fh, done_path.open("a") as done_fh:
            for i, name in enumerate(todo, 1):
                try:
                    found = fetch(name)
                except PermissionError as exc:
                    print(f"\n{exc}\nProgress is checkpointed — rerun the same "
                          "command with a fresh cookie to resume.", file=sys.stderr)
                    completed, stop_code = False, 3
                    break
                except QuotaError as exc:
                    print(f"\n{exc}\nHourly search quota (200/hr) reached — "
                          "progress is checkpointed. Rerun to resume, or add "
                          "--wait-on-quota to trickle through automatically.",
                          file=sys.stderr)
                    completed, stop_code = False, 4
                    break
                except Exception as exc:
                    # One bad name (server 5xx, odd query) must not end the run.
                    # Leave it out of the done file so a rerun retries it.
                    log.warning("  [%d/%d] %s -> skipped (%s)", i, len(todo),
                                name, type(exc).__name__)
                    continue
                for rec in found:
                    part_fh.write(json.dumps(rec) + "\n")
                    records.append(rec)
                part_fh.flush()
                done_fh.write(name + "\n")
                done_fh.flush()
                if i % 25 == 0 or found:
                    log.info("  [%d/%d] %s -> %d case(s)", i, len(todo), name, len(found))
                time.sleep(args.pace)
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed; rerun to resume.",
              file=sys.stderr)
        return 130

    # Always write what we have so the site can use it immediately; only clear
    # the checkpoint (enabling a fresh restart) when the whole list finished.
    out = write_outputs(records, args.out)
    ncases = len(records)
    nassoc = len({a for r in records for a in r["associations"]})
    ncourts = len({r["court"] for r in records})
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out                    # custom --out outside the repo
    log.info("wrote %s — %d cases across %d associations, %d courts",
             shown, ncases, nassoc, ncourts)
    if completed:
        clear_checkpoint(args.out)      # only a full sweep resets the resume state
    else:
        log.info("run paused before finishing — checkpoint kept; rerun to resume")
    return stop_code


if __name__ == "__main__":
    raise SystemExit(main())
