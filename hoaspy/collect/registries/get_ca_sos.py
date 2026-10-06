"""California HOAs from the Secretary of State's bizfile business search.

    python -m hoaspy ca-sos                        # the whole sweep (resumable)
    python -m hoaspy ca-sos --phase expand         # only mine collected names for new words
    python -m hoaspy ca-sos --ingest saved_*.jsonl # no network: shape rows saved from a browser

California has no statewide HOA registry, but every common-interest
development (CID) is on file with the Secretary of State: as a "Nonprofit
Corporation - CA - Mutual Benefit - Common Interest Development
Corporation" (FILING_TYPE_ID 69), an "Unincorporated Common Interest
Development" (62, the SI-CID statements TIC and condo boards file), or the
rare "Public Benefit - CID Corporation" (72). Anything filed under one of
those three types *is* an HOA by definition, whatever its name. HOA-shaped
names also turn up under plain nonprofits, LLCs and stock corporations;
those are kept only when the name passes the association gate.

    POST https://bizfileonline.sos.ca.gov/api/Records/businesssearch
    body {"SEARCH_VALUE": <term>, "SEARCH_TYPE_ID": "1",
          "SEARCH_FILTER_TYPE_ID": "0", "FILING_TYPE_ID": <type or "">,
          "STATUS_ID": "", "FILING_DATE": {"start": "M/D/YYYY"|null, ...}}
    -> {"rows": {<id>: {TITLE:["Name (FilingNo)"], ENTITY_TYPE, STATUS,
                        STANDING, FILING_DATE, AGENT, ...}}}

What the API does:

* **A 500-row cap and no paging.** Beaten by bisecting the FILING_DATE
  window: a capped (term, type, window) is split at its midpoint until
  every leaf window is under the cap. A capped single-day window is
  logged and kept.
* **Word search with limited prefix expansion.** "hoa" also hits
  HOANG/HOARDING, but wildcards and short tokens are truncated by the
  engine and an empty term returns nothing, so enumeration has to go
  through real words. Phase 1 sweeps the three CID types with a broad
  word list, phase 2 sweeps every entity type with the HOA keywords, and
  phase 3 mines the names collected so far for the most frequent words
  not yet searched and sweeps those, logging the yield of each.
* **Auth.** Requests need the browser's Imperva cookies *and* the Okta
  bearer token the site attaches (`authorization` header; 403 without
  it). The token lives one hour. On 401/403 the run pauses and polls
  `ca_cookie.txt` / `ca_token.txt` for a refresh (copy both from a
  businesssearch request in DevTools -> Network -> Request Headers), then
  resumes from its checkpoint. Cookies are IP/TLS-bound: run this on the
  machine whose browser produced them.

Output: California's rows in records/state_corps.jsonl (replaced wholesale
on a completed run) in the corporate-roster shape plus `bizfile_id`,
`standing` and `is_association` (true for CID-typed rows, so a consumer
keeps them even when the name lacks an HOA keyword). Raw API rows are
kept in records/ca_sos_raw.jsonl for re-shaping. Checkpoint:
records/.ca_sos_done.txt (finished and split windows),
records/.ca_sos_partial.jsonl (rows so far) and records/.ca_sos_words.json
(which words were swept against which type — kept after a completed run,
so a later expansion tries only new pairs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.registries import CORPORATIONS_PATH
from hoaspy.lib.http import BROWSER_USER_AGENT
from hoaspy.lib.jsonl import append_row, now_iso, read_json, read_rows, replace_rows, write_json
from hoaspy.lib.names import CORPORATION_NAME_RE
from hoaspy.lib.records import Association

DEFAULT_OUTPUT_PATH = CORPORATIONS_PATH
DEFAULT_RAW_PATH = ROOT / "records" / "ca_sos_raw.jsonl"
DONE_PATH = ROOT / "records" / ".ca_sos_done.txt"
PARTIAL_PATH = ROOT / "records" / ".ca_sos_partial.jsonl"
WORDS_PATH = ROOT / "records" / ".ca_sos_words.json"
COOKIE_PATH = ROOT / "ca_cookie.txt"
TOKEN_PATH = ROOT / "ca_token.txt"

SEARCH_URL = "https://bizfileonline.sos.ca.gov/api/Records/businesssearch"
SOURCE = "California Secretary of State — bizfile Business Search"
SOURCE_PAGE = "https://bizfileonline.sos.ca.gov/search/business"
SITE_URL = "https://bizfileonline.sos.ca.gov"
CHROME_VERSION = re.search(r"Chrome/(\d+)", BROWSER_USER_AGENT).group(1)

# FILING_TYPE_ID -> entity type, confirmed by sweeping ids 1-120 against
# the live API (only ids that returned rows for "hoa" are listed).
FILING_TYPES = {
    "3": "Limited Liability Company - CA",
    "4": "Limited Partnership - CA",
    "7": "Name Reservation",
    "16": "Limited Liability Company - Out of State",
    "23": "Stock Corporation - Out of State - Stock",
    "32": "Nonprofit Corporation - Out of State",
    "33": "Limited Partnership - Out of State",
    "38": "Stock Corporation - CA - General",
    "52": "Stock Corporation - CA - Close",
    "53": "Stock Corporation - CA - Professional",
    "55": "Stock Corporation - CA - Benefit",
    "59": "Nonprofit Corporation - CA - Public Benefit",
    "60": "Nonprofit Corporation - CA - Mutual Benefit",
    "61": "Nonprofit Corporation - CA - Religious",
    "62": "Unincorporated Common Interest Development",
    "69": "Nonprofit Corporation - CA - Mutual Benefit - Common Interest Development Corporation",
    "72": "Nonprofit Corporation - CA - Public Benefit - Common Interest Development Corporation",
}
# Entity types that are HOAs by definition: every row is kept.
CID_TYPES = ("62", "69", "72")
CID_LABEL = "Common Interest Development"

# Phase 1: words that appear in CID names, broad on purpose — the goal is to
# touch every CID row at least once, and the date bisection handles the
# volume. Digits work too (the engine prefix-matches numeric tokens), which
# catches the address-named SF/LA condo boards ("1004 W. BALBOA HOA").
CID_KEYWORDS = [
    "ASSOCIATION", "OWNERS", "HOMEOWNERS", "HOA", "CONDOMINIUM", "CONDO",
    "COMMUNITY", "MAINTENANCE", "COUNCIL", "CORPORATION", "PROPERTY",
    "RESIDENTS", "TENANTS", "COOPERATIVE", "MUTUAL", "CLUB",
    "VILLAGE", "ESTATES", "PARK", "HILLS", "GARDENS", "TERRACE", "VILLAS",
    "VILLA", "TOWNHOMES", "TOWNHOUSE", "TOWNHOUSES", "MANOR", "RANCH",
    "RANCHO", "VISTA", "VIEW", "LAKE", "OAKS", "PALM", "PALMS", "HEIGHTS",
    "SQUARE", "PLACE", "COURT", "STREET", "AVENUE", "DRIVE", "ROAD", "LANE",
    "WAY", "COMMONS", "PLAZA", "CREEK", "CANYON", "BAY", "BEACH", "OCEAN",
    "SEA", "HARBOR", "MARINA", "RIDGE", "VALLEY", "MESA", "MOUNTAIN",
    "SPRINGS", "WOODS", "GROVE", "MEADOWS", "POINT", "POINTE", "COVE",
    "SHORES", "ISLAND", "NORTH", "SOUTH", "EAST", "WEST", "GREEN", "GLEN",
    "TRAILS", "CROSSING", "LANDING", "COTTAGES", "LOFTS", "TOWERS", "TOWER",
    "APARTMENTS", "UNITS", "BUILDING", "HOMES", "HOME", "HOUSE", "CASA",
    "CASAS", "SAN", "SANTA", "LOS", "LAS", "DEL", "MISSION", "CAMINO",
    "PASEO", "PACIFIC", "CALIFORNIA", "SUNSET", "SIERRA", "MAR", "VERDE",
    "ALTA", "MONTE", "LOMA", "TIC", "GATE", "GARDEN", "WOOD", "HILL",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
]

# Phase 2: HOA-shaped words searched across every entity type, in step with
# CORPORATION_NAME_RE so each hit also passes the name gate (the engine
# prefix-matches, so CONDO also finds CONDOMINIUM).
NAMED_KEYWORDS = [
    "HOA", "HOMEOWNERS", "HOMEOWNER", "HOME OWNERS", "CONDOMINIUM", "CONDO",
    "PROPERTY OWNERS", "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION",
    "RESIDENTS ASSOCIATION", "TOWNHOMES", "TOWNHOME", "TOWNHOUSE", "VILLAS",
]

# Words the engine treats as stopwords or that carry no name signal — never
# worth a request in the mining phase.
STOPWORDS = {
    "THE", "OF", "AND", "INC", "A", "AN", "AT", "IN", "ON", "FOR", "BY",
    "TO", "DE", "LA", "EL", "NO", "OR", "LLC", "LTD", "CO", "CORP",
}

CAP = 500           # a search returning this many rows is truncated
DATE_MIN = "1850-01-01"
EXHAUSTED_AFTER = 20            # expansion words in a row that found nothing

TITLE_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<num>[^()]+)\)\s*$")
# Letters only: the engine folds "OWNERS'" into OWNERS, so possessives would
# just re-run a word already swept.
WORD_RE = re.compile(r"[A-Z]{3,}")

log = logging.getLogger(__name__)


# ---- shaping -------------------------------------------------------------------

def clean_title(title: str) -> tuple[str, str]:
    """'1 Buena Vista HOA (B20250040101)' -> (name, filing number)."""
    title = " ".join(title.split())
    match = TITLE_RE.match(title)
    if match:
        return match.group("name").strip(), match.group("num").strip()
    return title, ""


def iso_date(mdy: str) -> str:
    """'03/21/2025' -> '2025-03-21'; '' when the text is not a date."""
    match = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", mdy or "")
    if not match:
        return ""
    month, day, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def mdy_date(iso: str) -> str:
    """'2025-03-21' -> '03/21/2025' (what the API's FILING_DATE filter takes)."""
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


def is_cid(row: dict) -> bool:
    return CID_LABEL in (row.get("ENTITY_TYPE") or "")


def to_record(row: dict, retrieved_at: str = "") -> Association | None:
    """One bizfile row as a corporate-roster record, or None when it is not
    an HOA: a CID-typed row is one by definition and is always kept; every
    other entity type must pass the association name gate (so "1058 HOAGIE
    LLC" is dropped and "1014-1016 Diamond St HOA LLC" kept); a name
    reservation is not an entity."""
    name, filing_number = clean_title((row.get("TITLE") or [""])[0])
    if not name or (row.get("ENTITY_TYPE") or "") == "Name Reservation":
        return None
    cid = is_cid(row)
    if not cid and not CORPORATION_NAME_RE.search(name):
        return None
    return Association(
        state="CA", source=SOURCE, source_url=SOURCE_PAGE, record_id=filing_number or str(row.get("ID", "")),
        name=name, corp_status=row.get("STATUS", "") or "", incorporated=iso_date(row.get("FILING_DATE", "")),
        registered_agent=(row.get("AGENT") or "").strip(), entity_type=row.get("ENTITY_TYPE", "") or "",
        retrieved_at=retrieved_at,
        extras={"bizfile_id": str(row.get("ID", "")), "standing": row.get("STANDING", "") or "",
                "is_association": cid})


def parse_response(payload: dict) -> tuple[list[dict], int]:
    """(the kept rows, shaped; the raw row count, which feeds cap detection)."""
    rows = payload.get("rows") or {}
    kept = []
    for row in rows.values():
        record = to_record(row)
        if record:
            kept.append(record.as_row())
    return kept, len(rows)


def search_body(term: str, filing_type: str = "", start: str | None = None,
                end: str | None = None, status: str = "") -> dict:
    """The businesssearch request body for one query."""
    return {
        "SEARCH_VALUE": term, "SEARCH_FILTER_TYPE_ID": "0",
        "SEARCH_TYPE_ID": "1", "FILING_TYPE_ID": filing_type,
        "STATUS_ID": status,
        "FILING_DATE": {"start": mdy_date(start) if start else None,
                        "end": mdy_date(end) if end else None},
        "CORPORATION_BANKRUPTCY_YN": False,
        "CORPORATION_LEGAL_PROCEEDINGS_YN": False,
        "OFFICER_OBJECT": {"FIRST_NAME": "", "MIDDLE_NAME": "", "LAST_NAME": ""},
        "NUMBER_OF_FEMALE_DIRECTORS": "99",
        "NUMBER_OF_UNDERREPRESENTED_DIRECTORS": "99",
        "COMPENSATION_FROM": "", "COMPENSATION_TO": "",
        "SHARES_YN": False, "OPTIONS_YN": False, "BANKRUPTCY_YN": False,
        "FRAUD_YN": False, "LOANS_YN": False, "AUDITOR_NAME": "",
    }


# ---- date windows -------------------------------------------------------------

def split_window(start: str, end: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """An inclusive ISO date window as two adjacent halves."""
    first = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    if first >= last:
        raise ValueError(f"cannot split single-day window {start}")
    middle = first + (last - first) // 2
    return (start, middle.isoformat()), ((middle + dt.timedelta(days=1)).isoformat(), end)


def today_iso() -> str:
    return dt.date.today().isoformat()


# ---- the client ---------------------------------------------------------------

class BlockedError(Exception):
    """401/403 or an Imperva challenge: the cookie and token need refreshing."""


def open_session(cookie: str, token: str = "") -> requests.Session:
    """A session carrying the browser's cookie and bearer token, with the
    header set the site's own frontend sends and nothing more."""
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("empty CA SoS cookie — paste the bizfile Cookie header")
    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_USER_AGENT, "Accept": "*/*", "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": SITE_URL, "Referer": SOURCE_PAGE, "Cookie": cookie,
        "Cache-Control": "no-cache", "Pragma": "no-cache", "Priority": "u=1, i",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": f'"Not=A?Brand";v="99", "Google Chrome";v="{CHROME_VERSION}", "Chromium";v="{CHROME_VERSION}"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
    })
    if token.strip():
        session.headers["authorization"] = token.strip()
    return session


def search_once(session: requests.Session, term: str, filing_type: str = "",
                start: str | None = None, end: str | None = None,
                timeout: int = 60, url: str = SEARCH_URL, body_fn=search_body,
                parse_fn=None, **extra) -> tuple[list[dict], int, dict]:
    """One API call -> (kept rows, raw row count, raw rows). `url`,
    `body_fn` and `parse_fn` let the same session, auth and retry logic
    drive bizfile's other search endpoints (the ca-ucc command uses the
    UCC / judgment-lien one)."""
    response = session.post(url, json=body_fn(term, filing_type, start, end, **extra), timeout=timeout)
    if response.status_code in (401, 403) or "Request unsuccessful" in response.text[:400] \
            or "_Incapsula_" in response.text[:2000]:
        raise BlockedError(f"HTTP {response.status_code} on {term!r}/{filing_type}")
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise BlockedError(f"non-JSON on {term!r}/{filing_type} (login/challenge)") from exc
    kept, raw_count = (parse_fn or parse_response)(payload)
    rows = payload.get("rows") or {}
    if isinstance(payload.get("edge"), dict):
        # Paged endpoints (uccsearch) report offset/limit/total; pass it on.
        rows = dict(rows)
        rows["__edge__"] = payload["edge"]
    return kept, raw_count, rows


class Client:
    """A paced, retrying search client that pauses for a credential refresh
    on 401/403 instead of dying: it polls the cookie and token files for a
    newer mtime for up to `wait_minutes`, then resumes."""

    def __init__(self, cookie_path: Path, token_path: Path, pace: float = 1.0,
                 wait_minutes: float = 90, max_requests: int = 0,
                 url: str = SEARCH_URL, body_fn=search_body, parse_fn=None):
        self.cookie_path, self.token_path = cookie_path, token_path
        self.pace, self.wait_minutes = pace, wait_minutes
        self.max_requests = max_requests
        self.url, self.body_fn, self.parse_fn = url, body_fn, parse_fn
        self.requests = 0
        self.session: requests.Session | None = None
        self._stamp = (0.0, 0.0)
        self.reload()

    def _mtimes(self) -> tuple[float, float]:
        return (self.cookie_path.stat().st_mtime if self.cookie_path.exists() else 0.0,
                self.token_path.stat().st_mtime if self.token_path.exists() else 0.0)

    def reload(self) -> None:
        cookie = self.cookie_path.read_text() if self.cookie_path.exists() else ""
        token = self.token_path.read_text() if self.token_path.exists() else ""
        self.session = open_session(cookie, token)
        self._stamp = self._mtimes()

    def wait_for_refresh(self) -> bool:
        """Block until the cookie or token file changes; False on timeout."""
        log.error("bizfile rejected the credentials (the token expires hourly).")
        log.error("Refresh: DevTools -> Network -> 'businesssearch' -> Request Headers; "
                  "paste 'authorization' into %s and 'cookie' into %s. "
                  "Waiting up to %.0f min; the checkpoint is safe.",
                  self.token_path.name, self.cookie_path.name, self.wait_minutes)
        deadline = time.time() + self.wait_minutes * 60
        while time.time() < deadline:
            time.sleep(15)
            if self._mtimes() != self._stamp:
                time.sleep(2)  # let a paste finish
                try:
                    self.reload()
                except ValueError:
                    continue
                log.info("credentials refreshed — resuming")
                return True
        return False

    def search(self, term: str, filing_type: str, start: str | None,
               end: str | None, **extra) -> tuple[list[dict], int, dict]:
        if self.max_requests and self.requests >= self.max_requests:
            raise RuntimeError(f"--max-requests {self.max_requests} reached")
        blocked = 0
        backoff = 20.0
        while True:
            self.requests += 1
            try:
                found = search_once(self.session, term, filing_type, start, end,
                                    url=self.url, body_fn=self.body_fn, parse_fn=self.parse_fn, **extra)
                time.sleep(self.pace * random.uniform(0.7, 1.4))
                return found
            except BlockedError as exc:
                blocked += 1
                log.warning("%s", exc)
                if blocked == 1:
                    time.sleep(5)  # a single transient 403 happens; retry once
                    continue
                if not self.wait_for_refresh():
                    raise
                blocked = 0
            except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
                log.warning("transient error %s — backing off %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


# ---- the checkpoint -----------------------------------------------------------

class Checkpoint:
    """An append-only log of finished ("DONE key") and bisected ("SPLIT key")
    windows plus the shaped rows collected so far. path=None keeps it in
    memory (tests)."""

    def __init__(self, done_path: Path | None, partial_path: Path | None, raw_path: Path | None = None):
        self.done_path, self.partial_path, self.raw_path = done_path, partial_path, raw_path
        self.done: set[str] = set()
        self.split: set[str] = set()
        self.by_id: dict[str, dict] = {}
        self.raw_ids: set[str] = set()
        self.load()

    def load(self) -> None:
        if self.done_path and self.done_path.exists():
            for line in self.done_path.read_text().splitlines():
                tag, _, key = line.partition(" ")
                if tag == "DONE":
                    self.done.add(key)
                elif tag == "SPLIT":
                    self.split.add(key)
        if self.partial_path:
            for row in read_rows(self.partial_path, skip_bad=True):
                self.by_id[row["bizfile_id"]] = row
        if self.raw_path:
            for row in read_rows(self.raw_path, skip_bad=True):
                if isinstance(row, dict):
                    self.raw_ids.add(str(row.get("ID", "")))

    def _append(self, path: Path | None, line: str) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _append_row(self, path: Path | None, row: dict) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                append_row(fh, row)

    def mark_done(self, key: str) -> None:
        self.done.add(key)
        self._append(self.done_path, "DONE " + key)

    def mark_split(self, key: str) -> None:
        self.split.add(key)
        self._append(self.done_path, "SPLIT " + key)

    def add(self, rows: list[dict], raw_rows: dict) -> int:
        """Record the shaped rows and the raw rows of one answer; returns how many rows were new."""
        new = 0
        for row in rows:
            if row["bizfile_id"] in self.by_id:
                continue
            self.by_id[row["bizfile_id"]] = row
            self._append_row(self.partial_path, row)
            new += 1
        for row_id, row in raw_rows.items():
            row_id = str(row_id)
            if row_id in self.raw_ids:
                continue
            self.raw_ids.add(row_id)
            self._append_row(self.raw_path, row)
        return new

    def seed(self, rows: list[dict]) -> int:
        """Load previously written rows into memory (not re-appended to the
        partial log) so an expansion-only run can mine their names."""
        count = 0
        for row in rows:
            bizfile_id = row.get("bizfile_id")
            if bizfile_id and bizfile_id not in self.by_id:
                self.by_id[bizfile_id] = row
                count += 1
        return count

    def clear(self) -> None:
        for path in (self.done_path, self.partial_path):
            if path:
                path.unlink(missing_ok=True)


# ---- the sweep ----------------------------------------------------------------

class Sweeper:
    """Runs (term, type) searches over a date window, bisecting the window
    wherever the 500-row cap is hit. `search_fn(term, type, start, end)`
    returns (rows, raw_count, raw_rows): the Client, or a fake in tests."""

    def __init__(self, search_fn, checkpoint: Checkpoint, date_min: str = DATE_MIN,
                 date_max: str | None = None, words_path: Path | None = None, cap: int = CAP):
        self.search_fn, self.checkpoint = search_fn, checkpoint
        self.cap = cap
        self.date_min = date_min
        self.date_max = date_max or today_iso()
        self.capped_days: list[str] = []
        self.queries = 0
        self.term_yield: Counter = Counter()   # new rows credited per term
        # type -> words swept against it (a word is only "used" for the
        # types it was actually searched under).
        self.words_used: dict[str, set[str]] = defaultdict(set)
        self.words_path = words_path
        if words_path and words_path.exists():
            used = read_json(words_path, {})
            if isinstance(used, dict):
                for filing_type, words in used.items():
                    self.words_used[filing_type] = set(words)

    @staticmethod
    def key(term: str, filing_type: str, start: str, end: str) -> str:
        return f"{term}|{filing_type}|{start}|{end}"

    def save_words(self) -> None:
        if self.words_path:
            write_json(self.words_path, {filing_type: sorted(words) for filing_type, words in self.words_used.items()})

    def run_term(self, term: str, filing_type: str) -> int:
        """Sweep one (term, type) over the whole date range; returns the new rows."""
        before = len(self.checkpoint.by_id)
        self._window(term, filing_type, self.date_min, self.date_max)
        new = len(self.checkpoint.by_id) - before
        self.term_yield[f"{term}|{filing_type}"] += new
        self.words_used[filing_type].add(term.upper())
        self.save_words()
        return new

    def _window(self, term: str, filing_type: str, start: str, end: str) -> None:
        key = self.key(term, filing_type, start, end)
        if key in self.checkpoint.done:
            return
        if key not in self.checkpoint.split:
            rows, raw_count, raw_rows = self.search_fn(term, filing_type, start, end)
            self.queries += 1
            raw_rows = {row_id: row for row_id, row in raw_rows.items() if row_id != "__edge__"}
            new = self.checkpoint.add(rows, raw_rows)
            capped = raw_count >= self.cap
            log.info("%-22s type=%-2s %s..%s -> %3d rows, %3d kept, %3d new%s",
                     term[:22], filing_type or "*", start, end, raw_count, len(rows), new,
                     "  CAPPED->split" if capped and start != end else ("  CAPPED-day" if capped else ""))
            if not capped or start == end:
                if capped:
                    self.capped_days.append(key)
                self.checkpoint.mark_done(key)
                return
            self.checkpoint.mark_split(key)
        left, right = split_window(start, end)
        self._window(term, filing_type, *left)
        self._window(term, filing_type, *right)
        self.checkpoint.mark_done(key)


def mine_words(names: list[str], used: set[str], limit: int) -> list[str]:
    """The most frequent name words (3+ letters) not yet searched, by
    document frequency, minus the engine's stopwords. Feeds the expansion phase."""
    frequency: Counter = Counter()
    for name in names:
        frequency.update(set(WORD_RE.findall(name.upper())))
    out = []
    # Ties break alphabetically so the plan (and its checkpoint) is stable.
    for word, _ in sorted(frequency.items(), key=lambda item: (-item[1], item[0])):
        if word in used or word in STOPWORDS:
            continue
        out.append(word)
        if len(out) >= limit:
            break
    return out


def load_dropped(paths: list[Path]) -> dict[str, dict]:
    """Raw API rows from files saved out of a browser (one row per line) or
    a whole captured response ({"rows": {...}}), keyed by bizfile ID."""
    rows: dict[str, dict] = {}
    for path in paths:
        text = path.read_text().strip()
        if not text:
            continue
        if text.startswith("{") and '"rows"' in text[:200]:
            payload = read_json(path, None)
            if isinstance(payload, dict):
                for row_id, row in (payload.get("rows") or {}).items():
                    rows[str(row_id)] = row
                continue
        for row in read_rows(path, skip_bad=True):
            if isinstance(row, dict) and row.get("ID") is not None:
                rows[str(row["ID"])] = row
    return rows


# ---- output -------------------------------------------------------------------

def write_merged(rows: list[dict], out: Path) -> int:
    """Replace the CA rows of state_corps.jsonl with `rows`, keeping the
    other states; an empty result never clobbers existing data."""
    if not rows:
        if out.exists() and out.stat().st_size > 0:
            log.warning("no CA records collected — keeping existing %s untouched", out)
            return 0
    replace_rows(out, rows, {"CA"})
    return len(rows)


def ca_rows(path: Path) -> list[dict]:
    return [row for row in read_rows(path, skip_bad=True) if row.get("state") == "CA"]


def summarize(checkpoint: Checkpoint) -> str:
    by_type = Counter(row["entity_type"] for row in checkpoint.by_id.values())
    lines = [f"{count:6d}  {entity_type}" for entity_type, count in by_type.most_common()]
    return f"{len(checkpoint.by_id)} CA rows kept:\n" + "\n".join(lines)


# ---- the command --------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cookie-file", type=Path, default=COOKIE_PATH,
                        help="file holding the bizfile Cookie header (default ca_cookie.txt)")
    parser.add_argument("--token-file", type=Path, default=TOKEN_PATH,
                        help="file holding the Okta bearer token (default ca_token.txt)")
    parser.add_argument("--phase", choices=["cid", "named", "expand", "all"], default="all",
                        help="cid: CID types x broad words; named: all types x HOA keywords; "
                             "expand: mine collected names for new words")
    parser.add_argument("--keywords", help="comma-separated override of the word list")
    parser.add_argument("--filing-types", help="comma-separated override of FILING_TYPE_IDs ('' = all types)")
    parser.add_argument("--expand", type=int, default=150,
                        help="how many mined words to sweep per CID type (default 150)")
    parser.add_argument("--pace", type=float, default=1.0,
                        help="base seconds between requests (jittered; default 1.0)")
    parser.add_argument("--wait-minutes", type=float, default=90,
                        help="how long to wait for a credential refresh on 401/403")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="stop cleanly after this many API calls (0 = no limit)")
    parser.add_argument("--date-min", default=DATE_MIN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW_PATH)
    parser.add_argument("--ingest", nargs="+", type=Path, metavar="FILE",
                        help="no network: shape rows saved out of a browser (JSONL) or captured responses "
                             "and write the output")
    parser.add_argument("--fresh", action="store_true", help="ignore the checkpoint and re-query everything")


def ingest(args: argparse.Namespace) -> int:
    """Shape saved raw rows and merge them with whatever a run already holds."""
    raw = load_dropped(args.ingest)
    checkpoint = Checkpoint(None, None, args.raw)   # the raw file de-duplicates by ID
    rows, _ = parse_response({"rows": raw})
    checkpoint.add(rows, raw)
    for key, row in Checkpoint(DONE_PATH, PARTIAL_PATH).by_id.items():
        checkpoint.by_id.setdefault(key, row)
    for row in ca_rows(args.out):
        if row.get("bizfile_id"):
            checkpoint.by_id.setdefault(row["bizfile_id"], row)
    count = write_merged(list(checkpoint.by_id.values()), args.out)
    log.info("ingested %d raw rows from %d file(s) -> %d CA rows in %s",
             len(raw), len(args.ingest), count, args.out.name)
    log.info("%s", summarize(checkpoint))
    return 0


def expand(sweeper: Sweeper, checkpoint: Checkpoint, client: Client, filing_types, keywords, limit: int) -> None:
    """Phase 3: for each CID type, sweep the most frequent not-yet-searched
    words of the names collected so far, until twenty in a row find nothing."""
    for filing_type in filing_types:
        names = [row["name"] for row in checkpoint.by_id.values() if row.get("is_association")]
        used = {word.upper() for word in CID_KEYWORDS} | sweeper.words_used[filing_type]
        words = keywords or mine_words(names, used, limit)
        log.info("expansion for type %s: %d mined words: %s ...", filing_type, len(words), ", ".join(words[:15]))
        tail: list[int] = []
        for i, word in enumerate(words, 1):
            new = sweeper.run_term(word, filing_type)
            tail.append(new)
            log.info("== [expand %s %d/%d] %r: +%d new (total %d, %d requests)",
                     filing_type, i, len(words), word, new, len(checkpoint.by_id), client.requests)
            if len(tail) >= EXHAUSTED_AFTER and sum(tail[-EXHAUSTED_AFTER:]) == 0:
                log.info("expansion for type %s exhausted (%d words, 0 new)", filing_type, EXHAUSTED_AFTER)
                break


def run(args: argparse.Namespace) -> int:
    if args.ingest:
        return ingest(args)
    try:
        client = Client(args.cookie_file, args.token_file, pace=args.pace,
                        wait_minutes=args.wait_minutes, max_requests=args.max_requests)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if args.fresh:
        Checkpoint(DONE_PATH, PARTIAL_PATH).clear()
        WORDS_PATH.unlink(missing_ok=True)
    checkpoint = Checkpoint(DONE_PATH, PARTIAL_PATH, args.raw)
    sweeper = Sweeper(client.search, checkpoint, date_min=args.date_min, words_path=WORDS_PATH)
    if not checkpoint.by_id and args.out.exists():
        # A completed run cleared its checkpoint; start from what it wrote so
        # an expansion-only pass has names to mine and nothing is lost.
        log.info("seeded %d CA rows from %s", checkpoint.seed(ca_rows(args.out)), args.out.name)
    log.info("checkpoint: %d windows done, %d rows so far, %d words swept",
             len(checkpoint.done), len(checkpoint.by_id), sum(len(words) for words in sweeper.words_used.values()))

    types_override = ([t.strip() for t in args.filing_types.split(",")] if args.filing_types is not None else None)
    keywords = ([k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else None)
    plan: list[tuple[str, str, str]] = []
    if args.phase in ("cid", "all"):
        plan += [("cid", word, filing_type) for filing_type in (types_override or CID_TYPES)
                 for word in (keywords or CID_KEYWORDS)]
    if args.phase in ("named", "all"):
        plan += [("named", word, filing_type) for filing_type in (types_override or [""])
                 for word in (keywords or NAMED_KEYWORDS)]
    log.info("plan: %d (term,type) sweeps%s", len(plan), " + expansion" if args.phase in ("expand", "all") else "")

    completed = True
    try:
        for i, (phase, term, filing_type) in enumerate(plan, 1):
            new = sweeper.run_term(term, filing_type)
            log.info("== [%s %d/%d] %r type=%s: +%d new (total %d, %d requests)",
                     phase, i, len(plan), term, filing_type or "*", new, len(checkpoint.by_id), client.requests)
        if args.phase in ("expand", "all"):
            expand(sweeper, checkpoint, client, types_override or CID_TYPES, keywords, args.expand)
    except KeyboardInterrupt:
        log.warning("interrupted — checkpoint kept; rerun to resume")
        completed = False
    except (BlockedError, RuntimeError) as exc:
        log.error("stopped: %s — checkpoint kept; rerun to resume", exc)
        completed = False

    retrieved_at = now_iso()
    rows = [dict(row, retrieved_at=row.get("retrieved_at") or retrieved_at) for row in checkpoint.by_id.values()]
    count = write_merged(rows, args.out)
    shown = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    log.info("wrote %s — %d CA rows (%d API calls this run)", shown, count, client.requests)
    log.info("%s", summarize(checkpoint))
    if sweeper.capped_days:
        log.warning("%d single-day window(s) still capped (undercount): %s",
                    len(sweeper.capped_days), "; ".join(sweeper.capped_days[:10]))
    if completed:
        checkpoint.clear()
        log.info("sweep complete — checkpoint cleared (raw rows kept in %s)", args.raw.name)
    else:
        log.info("run paused before finishing — checkpoint kept; rerun to resume")
    return 0 if completed else 3
