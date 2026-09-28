#!/usr/bin/env python3
"""Aggregate California HOAs from the CA Secretary of State bizfile search.

California has no statewide HOA registry, but every common-interest
development (CID) is on file with the Secretary of State — as a
"Nonprofit Corporation - CA - Mutual Benefit - Common Interest Development
Corporation" (FILING_TYPE_ID 69), an "Unincorporated Common Interest
Development" (62, the SI-CID statements TIC/condo boards file), or the rare
"Public Benefit - CID Corporation" (72). Anything filed under one of those
three types *is* an HOA by definition, whatever its name. HOA-shaped names
also turn up under plain Mutual/Public Benefit nonprofits, LLCs and stock
corporations; those are kept only when the name passes the association gate
build_site applies everywhere.

    POST https://bizfileonline.sos.ca.gov/api/Records/businesssearch
    body {"SEARCH_VALUE": <term>, "SEARCH_TYPE_ID": "1",
          "SEARCH_FILTER_TYPE_ID": "0", "FILING_TYPE_ID": <type or "">,
          "STATUS_ID": "", "FILING_DATE": {"start": "M/D/YYYY"|null, ...}}
    -> {"rows": {<id>: {TITLE:["Name (FilingNo)"], ENTITY_TYPE, STATUS,
                        STANDING, FILING_DATE, AGENT, ...}}}

What the live API does (probed 2026-09-01):

* **500-row cap, no paging.** Beaten by bisecting the FILING_DATE window:
  a capped (term, type, window) is split at its midpoint until every leaf
  window is under the cap. A capped single-day window is logged and kept.
* **Search is word-based with limited prefix expansion.** "hoa" also hits
  HOANG/HOARDING, but wildcards and short tokens are truncated by the
  engine ("b*" returns 18 CID corps, "the"/"inc"/"of" almost nothing), and
  an empty or "*" term returns nothing — so enumeration has to go through
  real words. Phase 1 sweeps the three CID types with a broad word list,
  phase 2 sweeps every entity type with the HOA keywords, and phase 3
  mines the names collected so far for the most frequent words not yet
  searched and sweeps those too, logging the yield of each so the tail can
  be judged.
* **Auth.** Requests need the browser's Imperva cookies *and* the Okta
  bearer token the site attaches (`authorization` header; 403 without it).
  The token lives one hour. On 401/403 the run pauses and polls
  `ca_cookie.txt` / `ca_token.txt` for a refresh (copy both from a
  businesssearch request in DevTools → Network → Request Headers), then
  resumes from its checkpoint. Cookies are IP/TLS-bound: run this on the
  machine whose browser produced them.

Output rows match records/state_corps.jsonl; CA rows are replaced wholesale
on a completed run. CID-typed rows carry `is_association: true` so
build_site.add_corps keeps them even when the name lacks an HOA keyword.
Raw API rows are also kept in records/ca_sos_raw.jsonl for re-shaping.

Government source; --no-upload only (public registry names, referenced not
hosted).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

from hoaspy import ROOT
OUT_DEFAULT = ROOT / "records" / "state_corps.jsonl"
RAW_DEFAULT = ROOT / "records" / "ca_sos_raw.jsonl"
CKPT_DONE = ROOT / "records" / ".ca_sos_done.txt"
CKPT_PARTIAL = ROOT / "records" / ".ca_sos_partial.jsonl"
# Which words have been swept against which type. Survives a completed run
# (unlike the window checkpoint) so a later --phase expand only tries new
# (word, type) pairs.
WORDS_FILE = ROOT / "records" / ".ca_sos_words.json"
COOKIE_FILE = ROOT / "ca_cookie.txt"
TOKEN_FILE = ROOT / "ca_token.txt"

SEARCH_URL = "https://bizfileonline.sos.ca.gov/api/Records/businesssearch"
SOURCE = "California Secretary of State — bizfile Business Search"
SOURCE_URL = "https://bizfileonline.sos.ca.gov/search/business"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

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

# Phase 1 — words that appear in CID names, broad on purpose: the goal is to
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

# Phase 2 — HOA-shaped words searched across every entity type. Kept in
# lockstep with records_sunbiz.ASSOCIATION_NAME_RE so each hit also passes
# the name gate (the engine prefix-matches, so CONDO also finds CONDOMINIUM).
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

_TITLE_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<num>[^()]+)\)\s*$")
# Letters only: the engine folds "OWNERS'" into OWNERS, so possessives would
# just re-run a word already swept.
_WORD_RE = re.compile(r"[A-Z]{3,}")

log = logging.getLogger("ca_sos")


# ----------------------------------------------------------------- shaping

def clean_title(title: str) -> tuple[str, str]:
    """Split "1 Buena Vista HOA (B20250040101)" -> (name, filing_number)."""
    title = " ".join(title.split())
    m = _TITLE_RE.match(title)
    if m:
        return m.group("name").strip(), m.group("num").strip()
    return title, ""


def iso_date(mdy: str) -> str:
    """"03/21/2025" -> "2025-03-21"; junk -> ""."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", mdy or "")
    if not m:
        return ""
    mo, da, yr = m.groups()
    return f"{yr}-{int(mo):02d}-{int(da):02d}"


def mdy_date(iso: str) -> str:
    """"2025-03-21" -> "03/21/2025" (what the API's FILING_DATE filter takes)."""
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def is_cid(row: dict) -> bool:
    return CID_LABEL in (row.get("ENTITY_TYPE") or "")


def to_record(row: dict) -> dict | None:
    """Shape one bizfile row into a records/state_corps.jsonl row.

    CID-typed rows are HOAs by definition and are always kept; every other
    entity type must pass the association name gate build_site applies
    (so "1058 HOAGIE LLC" is dropped and "1014-1016 Diamond St HOA LLC" kept).
    Name reservations are not entities and are dropped outright."""
    title = (row.get("TITLE") or [""])[0]
    name, filing_number = clean_title(title)
    if not name or (row.get("ENTITY_TYPE") or "") == "Name Reservation":
        return None
    cid = is_cid(row)
    if not cid:
        from hoaspy.collect.registries import records_sunbiz
        if not records_sunbiz.ASSOCIATION_NAME_RE.search(name):
            return None
    return {
        "state": "CA",
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "record_id": filing_number or str(row.get("ID", "")),
        "bizfile_id": str(row.get("ID", "")),
        "name": name,
        "corp_status": row.get("STATUS", "") or "",
        "standing": row.get("STANDING", "") or "",
        "address": "", "city": "", "county": "", "zip": "",
        "incorporated": iso_date(row.get("FILING_DATE", "")),
        "registered_agent": (row.get("AGENT") or "").strip(),
        "entity_type": row.get("ENTITY_TYPE", "") or "",
        "is_association": cid,
    }


def parse_response(payload: dict) -> tuple[list[dict], int]:
    """Return (records, raw_row_count). raw_row_count feeds cap detection;
    records are the kept subset shaped for state_corps.jsonl."""
    rows = payload.get("rows") or {}
    out = []
    for row in rows.values():
        rec = to_record(row)
        if rec:
            out.append(rec)
    return out, len(rows)


def make_body(term: str, filing_type: str = "", start: str | None = None,
              end: str | None = None, status: str = "") -> dict:
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


# ------------------------------------------------------------ date windows

def split_window(start: str, end: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Bisect an inclusive ISO date window into two adjacent halves."""
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    if a >= b:
        raise ValueError(f"cannot split single-day window {start}")
    mid = a + (b - a) // 2
    return (start, mid.isoformat()), ((mid + dt.timedelta(days=1)).isoformat(), end)


def today_iso() -> str:
    return dt.date.today().isoformat()


# ------------------------------------------------------------------ client

class BlockedError(Exception):
    """401/403 or Imperva challenge — cookie/token need refreshing."""


def session(cookie: str, token: str = "") -> requests.Session:
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("empty CA SoS cookie — paste the bizfile Cookie header")
    s = requests.Session()
    # The exact header set the site's own frontend sends (captured from a
    # browser businesssearch request) — nothing beyond what Chrome sends.
    s.headers.update({
        "User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://bizfileonline.sos.ca.gov",
        "Referer": SOURCE_URL, "Cookie": cookie,
        "Cache-Control": "no-cache", "Pragma": "no-cache", "Priority": "u=1, i",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
    })
    if token.strip():
        s.headers["authorization"] = token.strip()
    return s


def search(s: requests.Session, term: str, filing_type: str = "",
           start: str | None = None, end: str | None = None,
           timeout: int = 60, url: str = SEARCH_URL, body_fn=make_body,
           parse_fn=None, **extra) -> tuple[list[dict], int, dict]:
    """One API call -> (records, raw_row_count, raw_rows). `url`/`body_fn`/
    `parse_fn` let the same session, auth and retry logic drive bizfile's
    other search endpoints (get_ca_ucc.py uses the UCC/judgment-lien one)."""
    r = s.post(url, json=body_fn(term, filing_type, start, end, **extra),
               timeout=timeout)
    if r.status_code in (401, 403) or "Request unsuccessful" in r.text[:400] \
            or "_Incapsula_" in r.text[:2000]:
        raise BlockedError(f"HTTP {r.status_code} on {term!r}/{filing_type}")
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError:
        raise BlockedError(f"non-JSON on {term!r}/{filing_type} (login/challenge)")
    recs, raw = (parse_fn or parse_response)(payload)
    rows = payload.get("rows") or {}
    if isinstance(payload.get("edge"), dict):
        # Paged endpoints (uccsearch) report offset/limit/total; pass it on.
        rows = dict(rows)
        rows["__edge__"] = payload["edge"]
    return recs, raw, rows


class Client:
    """Paced, retrying search client that pauses for a credential refresh
    on 401/403 instead of dying: it polls the cookie/token files for a
    newer mtime for up to `wait_minutes`, then resumes."""

    def __init__(self, cookie_file: Path, token_file: Path, pace: float = 1.0,
                 wait_minutes: float = 90, max_requests: int = 0,
                 url: str = SEARCH_URL, body_fn=make_body, parse_fn=None):
        self.cookie_file, self.token_file = cookie_file, token_file
        self.pace, self.wait_minutes = pace, wait_minutes
        self.max_requests = max_requests
        self.url, self.body_fn, self.parse_fn = url, body_fn, parse_fn
        self.requests = 0
        self.session: requests.Session | None = None
        self._stamp = (0.0, 0.0)
        self.reload()

    def _mtimes(self) -> tuple[float, float]:
        return (self.cookie_file.stat().st_mtime if self.cookie_file.exists() else 0.0,
                self.token_file.stat().st_mtime if self.token_file.exists() else 0.0)

    def reload(self) -> None:
        cookie = self.cookie_file.read_text() if self.cookie_file.exists() else ""
        token = self.token_file.read_text() if self.token_file.exists() else ""
        self.session = session(cookie, token)
        self._stamp = self._mtimes()

    def wait_for_refresh(self) -> bool:
        """Block until ca_cookie.txt or ca_token.txt changes. False on timeout."""
        log.error("bizfile rejected the credentials (token expires hourly).")
        log.error("Refresh: DevTools → Network → 'businesssearch' → Request Headers; "
                  "paste 'authorization' into %s and 'cookie' into %s. "
                  "Waiting up to %.0f min; checkpoint is safe.",
                  self.token_file.name, self.cookie_file.name, self.wait_minutes)
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
                out = search(self.session, term, filing_type, start, end,
                             url=self.url, body_fn=self.body_fn,
                             parse_fn=self.parse_fn, **extra)
                time.sleep(self.pace * random.uniform(0.7, 1.4))
                return out
            except BlockedError as e:
                blocked += 1
                log.warning("%s", e)
                if blocked == 1:
                    time.sleep(5)  # a single transient 403 happens; retry once
                    continue
                if not self.wait_for_refresh():
                    raise
                blocked = 0
            except (requests.HTTPError, requests.ConnectionError,
                    requests.Timeout) as e:
                log.warning("transient error %s — backing off %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


# -------------------------------------------------------------- checkpoint

class Checkpoint:
    """Append-only log of finished ("DONE key") and bisected ("SPLIT key")
    windows plus the shaped rows collected so far. path=None keeps it in
    memory (tests)."""

    def __init__(self, done_path: Path | None, partial_path: Path | None,
                 raw_path: Path | None = None):
        self.done_path, self.partial_path, self.raw_path = done_path, partial_path, raw_path
        self.done: set[str] = set()
        self.split: set[str] = set()
        self.by_id: dict[str, dict] = {}
        self.raw_ids: set[str] = set()
        self.load()

    def load(self) -> None:
        if self.done_path and self.done_path.exists():
            for ln in self.done_path.read_text().splitlines():
                tag, _, key = ln.partition(" ")
                if tag == "DONE":
                    self.done.add(key)
                elif tag == "SPLIT":
                    self.split.add(key)
        if self.partial_path and self.partial_path.exists():
            for ln in self.partial_path.read_text().splitlines():
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue  # torn final line from a hard kill
                self.by_id[rec["bizfile_id"]] = rec
        if self.raw_path and self.raw_path.exists():
            for ln in self.raw_path.read_text().splitlines():
                try:
                    self.raw_ids.add(str(json.loads(ln).get("ID", "")))
                except (json.JSONDecodeError, AttributeError):
                    continue

    def _append(self, path: Path | None, line: str) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(line + "\n")

    def mark_done(self, key: str) -> None:
        self.done.add(key)
        self._append(self.done_path, "DONE " + key)

    def mark_split(self, key: str) -> None:
        self.split.add(key)
        self._append(self.done_path, "SPLIT " + key)

    def add(self, records: list[dict], raw_rows: dict) -> int:
        new = 0
        for rec in records:
            if rec["bizfile_id"] in self.by_id:
                continue
            self.by_id[rec["bizfile_id"]] = rec
            self._append(self.partial_path, json.dumps(rec, ensure_ascii=False))
            new += 1
        for rid, row in raw_rows.items():
            rid = str(rid)
            if rid in self.raw_ids:
                continue
            self.raw_ids.add(rid)
            self._append(self.raw_path, json.dumps(row, ensure_ascii=False))
        return new

    def seed(self, records: list[dict]) -> int:
        """Load previously written rows into memory (not re-appended to the
        partial log) so an expansion-only run can mine their names."""
        n = 0
        for rec in records:
            bid = rec.get("bizfile_id")
            if bid and bid not in self.by_id:
                self.by_id[bid] = rec
                n += 1
        return n

    def clear(self) -> None:
        for p in (self.done_path, self.partial_path):
            if p:
                p.unlink(missing_ok=True)


# ------------------------------------------------------------------- sweep

class Sweeper:
    """Runs (term, type) searches over a date window, bisecting the window
    wherever the 500-row cap is hit. `search_fn(term, type, start, end)`
    returns (records, raw_count, raw_rows) — the Client, or a fake in tests."""

    def __init__(self, search_fn, ckpt: Checkpoint, date_min: str = DATE_MIN,
                 date_max: str | None = None, words_path: Path | None = None,
                 cap: int = CAP):
        self.search_fn, self.ckpt = search_fn, ckpt
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
            try:
                for t, ws in json.loads(words_path.read_text()).items():
                    self.words_used[t] = set(ws)
            except (json.JSONDecodeError, AttributeError):
                pass

    @staticmethod
    def key(term: str, ftype: str, start: str, end: str) -> str:
        return f"{term}|{ftype}|{start}|{end}"

    def save_words(self) -> None:
        if self.words_path:
            self.words_path.parent.mkdir(parents=True, exist_ok=True)
            self.words_path.write_text(json.dumps(
                {t: sorted(ws) for t, ws in self.words_used.items()}, indent=0))

    def run_term(self, term: str, ftype: str) -> int:
        """Sweep one (term, type) over the whole date range; return new rows."""
        before = len(self.ckpt.by_id)
        self._window(term, ftype, self.date_min, self.date_max)
        new = len(self.ckpt.by_id) - before
        self.term_yield[f"{term}|{ftype}"] += new
        self.words_used[ftype].add(term.upper())
        self.save_words()
        return new

    def _window(self, term: str, ftype: str, start: str, end: str) -> None:
        key = self.key(term, ftype, start, end)
        if key in self.ckpt.done:
            return
        if key not in self.ckpt.split:
            recs, raw, raw_rows = self.search_fn(term, ftype, start, end)
            self.queries += 1
            raw_rows = {k: v for k, v in raw_rows.items() if k != "__edge__"}
            new = self.ckpt.add(recs, raw_rows)
            capped = raw >= self.cap
            log.info("%-22s type=%-2s %s..%s -> %3d rows, %3d kept, %3d new%s",
                     term[:22], ftype or "*", start, end, raw, len(recs), new,
                     "  CAPPED→split" if capped and start != end else
                     ("  CAPPED-day" if capped else ""))
            if not capped or start == end:
                if capped:
                    self.capped_days.append(key)
                self.ckpt.mark_done(key)
                return
            self.ckpt.mark_split(key)
        left, right = split_window(start, end)
        self._window(term, ftype, *left)
        self._window(term, ftype, *right)
        self.ckpt.mark_done(key)


def mine_words(names: list[str], used: set[str], limit: int) -> list[str]:
    """Most frequent name words (>=3 letters) not yet searched, by document
    frequency, minus engine stopwords. Feeds the expansion phase."""
    df: Counter = Counter()
    for n in names:
        df.update(set(_WORD_RE.findall(n.upper())))
    out = []
    # Ties break alphabetically so the plan (and its checkpoint) is stable.
    for w, _ in sorted(df.items(), key=lambda kv: (-kv[1], kv[0])):
        if w in used or w in STOPWORDS:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def load_dropped(paths: list[Path]) -> dict[str, dict]:
    """Raw API rows from files saved by ca_sos_browser.js (one row per line)
    or a whole captured response ({"rows": {...}}), keyed by bizfile ID."""
    rows: dict[str, dict] = {}
    for p in paths:
        text = p.read_text().strip()
        if not text:
            continue
        if text.startswith("{") and '"rows"' in text[:200]:
            try:
                payload = json.loads(text)
                for rid, row in (payload.get("rows") or {}).items():
                    rows[str(rid)] = row
                continue
            except json.JSONDecodeError:
                pass  # fall through: maybe JSONL whose first row mentions rows
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("ID") is not None:
                rows[str(row["ID"])] = row
    return rows


# ------------------------------------------------------------------ output

def write_merged(records: list[dict], out: Path) -> int:
    """Replace CA rows in state_corps.jsonl with `records`, keep other states.
    Empty-guard: never clobber existing data with nothing."""
    if not records:
        if out.exists() and out.stat().st_size > 0:
            log.warning("no CA records collected — keeping existing %s untouched", out)
            return 0
    existing = []
    if out.exists():
        for ln in out.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("state") != "CA":
                existing.append(r)
    merged = existing + records
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for r in merged:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(out)
    return len(records)


def summarize(ckpt: Checkpoint) -> str:
    by_type = Counter(r["entity_type"] for r in ckpt.by_id.values())
    lines = [f"{n:6d}  {t}" for t, n in by_type.most_common()]
    return f"{len(ckpt.by_id)} CA rows kept:\n" + "\n".join(lines)


# -------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", type=Path, default=COOKIE_FILE,
                    help="file holding the bizfile Cookie header (default ca_cookie.txt)")
    ap.add_argument("--token-file", type=Path, default=TOKEN_FILE,
                    help="file holding the Okta bearer token (default ca_token.txt)")
    ap.add_argument("--phase", choices=["cid", "named", "expand", "all"], default="all",
                    help="cid: CID types x broad words; named: all types x HOA "
                         "keywords; expand: mine collected names for new words")
    ap.add_argument("--keywords", help="comma-separated override of the word list")
    ap.add_argument("--filing-types", help="comma-separated override of FILING_TYPE_IDs "
                                           "('' = all types)")
    ap.add_argument("--expand", type=int, default=150,
                    help="how many mined words to sweep per CID type (default 150)")
    ap.add_argument("--pace", type=float, default=1.0,
                    help="base seconds between requests (jittered; default 1.0)")
    ap.add_argument("--wait-minutes", type=float, default=90,
                    help="how long to wait for a credential refresh on 401/403")
    ap.add_argument("--max-requests", type=int, default=0,
                    help="stop cleanly after this many API calls (0 = no limit)")
    ap.add_argument("--date-min", default=DATE_MIN)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--raw", type=Path, default=RAW_DEFAULT)
    ap.add_argument("--ingest", nargs="+", type=Path, metavar="FILE",
                    help="no network: shape rows saved by ca_sos_browser.js "
                         "(JSONL) or captured responses and write the output")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore checkpoint and re-query everything")
    ap.add_argument("--no-upload", action="store_true",
                    help="explicit no-op; this collector never uploads")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s\t%(message)s", datefmt="%H:%M:%S")

    if args.ingest:
        raw = load_dropped(args.ingest)
        ckpt = Checkpoint(None, None, args.raw)   # raw file dedupes by ID
        recs, _ = parse_response({"rows": raw})
        ckpt.add(recs, raw)
        # Merge with whatever an API run already checkpointed or wrote.
        prior = Checkpoint(CKPT_DONE, CKPT_PARTIAL).by_id
        for k, v in prior.items():
            ckpt.by_id.setdefault(k, v)
        if args.out.exists():
            for ln in args.out.read_text().splitlines():
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if r.get("state") == "CA" and r.get("bizfile_id"):
                    ckpt.by_id.setdefault(r["bizfile_id"], r)
        n = write_merged(list(ckpt.by_id.values()), args.out)
        log.info("ingested %d raw rows from %d file(s) -> %d CA rows in %s",
                 len(raw), len(args.ingest), n, args.out.name)
        log.info("%s", summarize(ckpt))
        return 0

    try:
        client = Client(args.cookie_file, args.token_file, pace=args.pace,
                        wait_minutes=args.wait_minutes,
                        max_requests=args.max_requests)
    except ValueError as e:
        log.error("%s", e)
        return 2

    if args.fresh:
        Checkpoint(CKPT_DONE, CKPT_PARTIAL).clear()
        WORDS_FILE.unlink(missing_ok=True)
    ckpt = Checkpoint(CKPT_DONE, CKPT_PARTIAL, args.raw)
    sweeper = Sweeper(client.search, ckpt, date_min=args.date_min,
                      words_path=WORDS_FILE)
    if not ckpt.by_id and args.out.exists():
        # A completed run cleared its checkpoint; start from what it wrote so
        # an expansion-only pass has names to mine and nothing is lost.
        prior = []
        for ln in args.out.read_text().splitlines():
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("state") == "CA":
                prior.append(r)
        log.info("seeded %d CA rows from %s", ckpt.seed(prior), args.out.name)
    log.info("checkpoint: %d windows done, %d rows so far, %d words swept",
             len(ckpt.done), len(ckpt.by_id),
             sum(len(w) for w in sweeper.words_used.values()))

    # Build the plan: list of (phase, term, type).
    plan: list[tuple[str, str, str]] = []
    types_override = ([t.strip() for t in args.filing_types.split(",")]
                      if args.filing_types is not None else None)
    kw_override = ([k.strip() for k in args.keywords.split(",") if k.strip()]
                   if args.keywords else None)
    if args.phase in ("cid", "all"):
        for t in (types_override or CID_TYPES):
            for w in (kw_override or CID_KEYWORDS):
                plan.append(("cid", w, t))
    if args.phase in ("named", "all"):
        for t in (types_override or [""]):
            for w in (kw_override or NAMED_KEYWORDS):
                plan.append(("named", w, t))
    log.info("plan: %d (term,type) sweeps%s", len(plan),
             " + expansion" if args.phase in ("expand", "all") else "")

    completed = True
    try:
        for i, (phase, term, ftype) in enumerate(plan, 1):
            new = sweeper.run_term(term, ftype)
            log.info("== [%s %d/%d] %r type=%s: +%d new (total %d, %d requests)",
                     phase, i, len(plan), term, ftype or "*", new,
                     len(ckpt.by_id), client.requests)

        if args.phase in ("expand", "all"):
            for t in (types_override or CID_TYPES):
                names = [r["name"] for r in ckpt.by_id.values()
                         if r.get("is_association")]
                used = {w.upper() for w in CID_KEYWORDS} | sweeper.words_used[t]
                words = kw_override or mine_words(names, used, args.expand)
                log.info("expansion for type %s: %d mined words: %s ...",
                         t, len(words), ", ".join(words[:15]))
                tail: list[int] = []
                for j, w in enumerate(words, 1):
                    new = sweeper.run_term(w, t)
                    tail.append(new)
                    log.info("== [expand %s %d/%d] %r: +%d new (total %d, %d requests)",
                             t, j, len(words), w, new, len(ckpt.by_id), client.requests)
                    if len(tail) >= 20 and sum(tail[-20:]) == 0:
                        log.info("expansion for type %s exhausted (20 words, 0 new)", t)
                        break
    except KeyboardInterrupt:
        log.warning("interrupted — checkpoint kept; rerun to resume")
        completed = False
    except (BlockedError, RuntimeError) as e:
        log.error("stopped: %s — checkpoint kept; rerun to resume", e)
        completed = False

    records = list(ckpt.by_id.values())
    n = write_merged(records, args.out)
    try:
        rel = args.out.relative_to(ROOT)
    except ValueError:
        rel = args.out
    log.info("wrote %s — %d CA rows (%d API calls this run)", rel, n, client.requests)
    log.info("%s", summarize(ckpt))
    if sweeper.capped_days:
        log.warning("%d single-day window(s) still capped (undercount): %s",
                    len(sweeper.capped_days), "; ".join(sweeper.capped_days[:10]))
    if completed:
        ckpt.clear()
        log.info("sweep complete — checkpoint cleared (raw rows kept in %s)",
                 args.raw.name)
    else:
        log.info("run paused before finishing — checkpoint kept; rerun to resume")
    log.info("upload skipped (public registry names; referenced, not hosted).")
    return 0 if completed else 3


if __name__ == "__main__":
    sys.exit(main())
