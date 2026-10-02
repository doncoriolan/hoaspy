#!/usr/bin/env python
"""Collect court cases naming community associations from CourtListener's
quarterly bulk data (Free Law Project): every docket and every opinion whose
caption carries an association, in every court CourtListener holds — federal
district, bankruptcy and appellate, state supreme and appellate, and the
state trial courts it has.

`get_courts` asks the search API five phrases and is throttled hard when
anonymous (5 requests a minute, then a daily cap). The bulk files have no
limit and no query grammar: this collector downloads three of them from the
public S3 bucket, reads every caption once and keeps the ones where a party
is a homeowners / condominium / property owners association — including the
abbreviated captions state reporters print ("Homeowners Ass'n", "Condo.
Assn.") that a phrase search for "homeowners association" never returns.

    ./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk                 # latest snapshot
    ./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk --snapshot 2026-09-30
    ./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk --refine-only   # re-run the name rules only

Three steps, each skipped when its result is already in the cache
(default ./.cache/courtlistener_bulk/, ~7.7 GB of downloads):

1. download `courts-`, `opinion-clusters-` and `dockets-<date>.csv.bz2`
   (parallel range requests, resumable);
2. scan the two big files once each, keeping the rows whose caption has an
   association word (`hoa_clusters-<date>.jsonl`, `hoa_dockets-<date>.jsonl`
   in the cache) — about an hour, almost all of it bzip2;
3. refine: split each caption into parties, keep the association-shaped
   ones, and write the records.

Output (default ./courts/): bulk_dockets.jsonl, bulk_opinions.jsonl, a
`courtlistener_bulk` entry merged into sources.json and, when writing to the
default folder, per-state counts in coverage.json (`courts_bulk_dockets`,
`courts_bulk_opinions`). The docket records
share the shape of dockets.jsonl and the opinion records the shape of
opinions.jsonl; both overlap with what `get_courts` finds (same
`docket_id` / `url`), so a consumer of both dedupes on those.

What the bulk files do not have is party lists: a bankruptcy captioned
"In re John Doe" where the association is only a creditor is found by
`get_courts` (`party:` search), not here. Rows CourtListener marks
`blocked` (kept out of search engines, mostly bankruptcies) are skipped.
"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import csv
import html
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.courts.court_portals._common import has_business_form, normalize
from hoaspy.collect.courts.get_courts import STATE_NAMES, USER_AGENT

DEFAULT_OUT = ROOT / "courts"
DEFAULT_CACHE = ROOT / ".cache" / "courtlistener_bulk"
COVERAGE = ROOT / "coverage.json"

BUCKET = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
PREFIX = "bulk-data/"
SOURCE_PAGE = "https://www.courtlistener.com/help/api/bulk-data/"
BASE_URL = "https://www.courtlistener.com"
TABLES = ("courts", "opinion-clusters", "dockets")

log = logging.getLogger("courts_bulk")

_STATE_BY_LEN = sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[1]))


# ── the bucket ────────────────────────────────────────────────────────────

def parse_listing(xml: str) -> tuple[list[tuple[str, int]], str]:
    """(key, size) pairs of one ListObjectsV2 page and the continuation
    token ("" on the last page)."""
    items = [(k, int(s)) for k, s in
             re.findall(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", xml, re.S)]
    m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
    return items, html.unescape(m.group(1)) if m else ""


def snapshots(items: list[tuple[str, int]]) -> dict[str, dict[str, tuple[str, int]]]:
    """{date: {table: (key, size)}} for every snapshot that has all of TABLES."""
    found: dict[str, dict[str, tuple[str, int]]] = {}
    for key, size in items:
        m = re.fullmatch(re.escape(PREFIX) + r"(" + "|".join(TABLES) + r")-(\d{4}-\d{2}-\d{2})\.csv\.bz2", key)
        if m:
            found.setdefault(m.group(2), {})[m.group(1)] = (key, size)
    return {d: t for d, t in found.items() if len(t) == len(TABLES)}


def list_bucket(session: requests.Session) -> list[tuple[str, int]]:
    items, token = [], ""
    while True:
        params = {"list-type": "2", "prefix": PREFIX, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        resp = session.get(BUCKET, params=params, timeout=60)
        resp.raise_for_status()
        page, token = parse_listing(resp.text)
        items += page
        if not token:
            return items


def plan_parts(size: int, connections: int, min_part: int = 64 << 20) -> list[tuple[int, int]]:
    """Inclusive (start, end) byte ranges covering `size` — one per
    connection, fewer for a small file."""
    n = max(1, min(connections, size // min_part or 1))
    step = -(-size // n)
    return [(i * step, min(size, (i + 1) * step) - 1) for i in range(n) if i * step < size]


def download(key: str, size: int, dest: Path, connections: int = 8) -> None:
    """Fetch one bucket object with parallel range requests. Each part
    resumes from what is already on disk, so a killed run loses nothing."""
    if dest.exists() and dest.stat().st_size == size:
        log.info("have %s", dest.name)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = plan_parts(size, connections)

    def fetch(i: int) -> Path:
        start, end = parts[i]
        path = dest.with_name(f"{dest.name}.{len(parts)}part{i:02d}")
        want = end - start + 1
        for attempt in range(10):
            have = path.stat().st_size if path.exists() else 0
            if have >= want:
                return path
            try:
                with requests.get(BUCKET + key, stream=True, timeout=120,
                                  headers={"Range": f"bytes={start + have}-{end}",
                                           "User-Agent": USER_AGENT}) as resp:
                    resp.raise_for_status()
                    with path.open("ab") as fh:
                        for block in resp.iter_content(1 << 20):
                            fh.write(block)
            except requests.RequestException as exc:
                log.warning("%s part %d: %s — retrying", dest.name, i, exc)
                time.sleep(5 * (attempt + 1))
        if path.stat().st_size < want:
            raise IOError(f"{dest.name} part {i} incomplete after retries")
        return path

    log.info("downloading %s (%.1f GB, %d connections)", dest.name, size / 1e9, len(parts))
    with ThreadPoolExecutor(len(parts)) as pool:
        paths = list(pool.map(fetch, range(len(parts))))
    tmp = dest.with_name(dest.name + ".tmp")
    with tmp.open("wb") as out:
        for path in paths:
            with path.open("rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 22)
    if tmp.stat().st_size != size:
        raise IOError(f"{dest.name}: {tmp.stat().st_size} bytes, expected {size}")
    tmp.replace(dest)
    for path in paths:
        path.unlink()


# ── reading the CSV ───────────────────────────────────────────────────────

@contextlib.contextmanager
def open_rows(path: Path):
    """csv rows of a bulk file (`.csv` or `.csv.bz2`). The files are
    PostgreSQL `COPY … WITH (FORMAT csv, ESCAPE '\\')`: quotes inside a field
    are `\\"`, fields may span lines, NULL is an unquoted empty field. bzip2
    itself is used when installed — several times faster than the module."""
    csv.field_size_limit(1 << 30)
    proc = None
    if path.suffix == ".bz2":
        exe = shutil.which("bzip2")
        if exe:
            proc = subprocess.Popen([exe, "-dc", str(path)], stdout=subprocess.PIPE)
            raw = proc.stdout
        else:
            raw = bz2.open(path, "rb")
    else:
        raw = path.open("rb")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    try:
        yield csv.reader(text, escapechar="\\", doublequote=False)
    finally:
        text.close()
        if proc is not None and proc.wait() != 0:
            raise IOError(f"bzip2 failed on {path.name} (truncated download?)")


def court_table(path: Path) -> dict[str, dict]:
    """{court id: {state, jurisdiction, name}} from the courts file. The
    state is read out of the court's full name, as `get_courts` does;
    `jurisdiction` is CourtListener's code (FD district, FB bankruptcy, F
    federal appellate, S / SA state supreme / appellate, ST state trial …)."""
    out = {}
    with open_rows(path) as rows:
        head = next(rows)
        col = {c: i for i, c in enumerate(head)}
        for row in rows:
            if len(row) != len(head):
                continue
            name = row[col["full_name"]]
            state = next((code for code, full in _STATE_BY_LEN
                          if re.search(r"\b" + re.escape(full) + r"\b", name)), "")
            out[row[col["id"]]] = {"state": state, "jurisdiction": row[col["jurisdiction"]],
                                   "name": name}
    return out


# ── which captions, which parties ─────────────────────────────────────────

# Step 2's net: wide, cheap, run on 80+ million captions. Step 3 decides.
BROAD = re.compile(
    r"home[- ]?owner|condominium|\bcondos?\b|owners'?,? ass|community ass|"
    r"townho(?:me|use)|property owners|\bH\.?O\.?A\b\.?|\bP\.?O\.?A\b\.?|unit owners|"
    r"co-?owners|board of managers|master ass|civic ass|improvement ass|"
    r"homes ass|residents'? ass|cooperative apartment|housing co-?op|"
    r"apartment owners|lot owners|landowners'? ass|property ass|"
    r"maintenance ass|recreation ass|neighborhood ass|villas? ass|"
    r"estates ass|village ass|lakes? ass|council of (?:unit|co)|"
    r"ass(?:ociation|'?n\.?|oc\.?) of (?:\w+ )?owners|cmty\.? ass|bd\.? of mgrs",
    re.I)

# Association words that decide on their own, matched on normalize()d text
# (upper case, punctuation gone, OF / AND / THE / INC dropped).
STRICT = re.compile(
    r"HOMEOWNERS?\b|HOME OWNERS?\b|CONDOMINIUMS?\b|\bCONDOS?\b|PROPERTY OWNERS?\b|UNIT OWNERS?\b|"
    r"LOT OWNERS?\b|APARTMENT OWNERS\b|\bCO OWNERS\b|TOWNHOMES?\b|TOWNHOUSES?\b|"
    r"\b(?:OWNERS|COMMUNITY|MASTER|CIVIC|IMPROVEMENT|HOMES|RESIDENTS|NEIGHBORHOOD|MAINTENANCE|"
    r"RECREATION|PROPERTY|VILLAS?|ESTATES|VILLAGE|LAKES?|LANDOWNERS) ASSOCIATION\b|"
    r"COUNCIL (?:UNIT |CO )?OWNERS\b|COOPERATIVE APARTMENTS?\b|HOUSING COOPERATIVE\b|"
    r"\bASSOCIATION (?:APARTMENT |UNIT |HOME |PROPERTY |LOT |CO )?OWNERS\b")
ABBR = re.compile(r"\b(HOA|POA)\b")
# Lenders, insurers, builders, public bodies and trade groups that carry an
# association word: "Home Owners' Loan Corporation", "Bank of America,
# National Association", "Standardbred Owners Association".
NOT_ASSOC = re.compile(
    r"\bLOAN\b|WARRANTY|\bCHOICE\b|MUTUAL|SAVINGS|INSURANCE|ASSURANCE|\bBANK\b|MORTGAGE|"
    r"\bREALTY\b|DEVELOPERS?\b|DEVELOPMENT\b|CONSTRUCTION|\bBUILDERS\b|SCHOOL|COLLEGE|"
    r"UNIVERSITY|HOSPITAL|\bCOUNTY\b|\bCITY\b|BAR ASSOCIATION|EDUCATION ASSOCIATION|\bUNION\b|"
    r"TEACHERS|EMPLOYEES|\bPOLICE\b|FIRE ?FIGHTERS|MEDICAL|\bTITLE\b|MANAGEMENT|"
    r"COMMUNITY ASSOCIATIONS INSTITUTE|UNDERWRITERS|\bTAX\b|\bUNITED STATES\b|STANDARDBRED|"
    r"THOROUGHBRED|HORSE|CATTLE|\bTAXI|\bCAB\b|TRUCK|\bBOAT|VESSEL|AIRCRAFT|PILOTS|LIQUOR|"
    r"THEAT(?:RE|ER)|HOTEL|MOTEL|SERVICE STATION|BUSINESS OWNERS|RESTAURANT|TAVERN|"
    r"NATIONAL ASSOCIATION|\bREPAIR\b|CONSULTING|\bBEHALF\b|\bLOCATED\b")
_NOT_A_PARTY = re.compile(
    r"(?:ON BEHALF|SUING|PROCEEDS|ALL OTHER|CERTAIN|UNKNOWN|REAL PROPERTY|INDIVIDUALLY|INDIV|REPRESENTING|"
    r"AS (?:OFFICERS?|MEMBERS?|REPRESENTATIVES?|TRUSTEES?|PRESIDENT|TREASURER|DIRECTORS?|INDIVIDUAL)|"
    r"HUSBAND|ALL INDIVIDUALLY|CIVIL NO|PARCEL|PARENTS)\b")
# A class of owners, not an association: "… and All Other Property Owners
# in the Subdivision", "a Class of Homeowners Residing in Keauhou".
_CLASS = re.compile(r"\bOTHER (?:PROPERTY )?OWNERS\b|TAXPAYERS|\bCLASS\b|SIMILARLY SITUATED|\bRESIDING\b|"
                    r"\bOTHER HOMEOWNERS\b|OTHER RESIDENTS")
# "Roseland Townhomes" is as often a rental complex as an association;
# alone, the word counts only on a corporation ("Kendall Walk Townhomes, Inc").
_TOWNHOME_ONLY = re.compile(r"TOWNHOMES?\b|TOWNHOUSES?\b")
_BEYOND_TOWNHOME = re.compile(r"ASSOCIATION|OWNERS|CONDO|\bHOA\b|\bPOA\b|COUNCIL|COOPERATIVE|HOMEOWNER")
MAX_NAME_WORDS = 12
# Words that name no particular association: a party made only of these
# ("Homeowners", "Property Owners", "Condos") is a truncated caption.
GENERIC = frozenset("""ASSOCIATION ASSOCIATIONS HOMEOWNERS HOMEOWNER HOME OWNERS OWNER PROPERTY
CONDOMINIUM CONDOMINIUMS CONDO CONDOS COMMUNITY MASTER TOWNHOME TOWNHOMES TOWNHOUSE TOWNHOUSES UNIT
COUNCIL BOARD MANAGERS DIRECTORS IMPROVEMENT CIVIC RESIDENTS MAINTENANCE NO PHASE SECTION HOA POA
ALL OTHER I II III IV V 1 2 3 4 5""".split())

_ROLE = re.compile(
    r",?\s*\b(?:et\.? ?al\.?|etc\.?|petitioners?(?:\(s\))?|respondents?(?:\(s\))?|appellants?|appellees?|"
    r"plaintiffs?|defendants?|cross-\w+|intervenors?|individually|a/k/a.*|aka\b.*|f/k/a.*|n/k/a.*|d/b/a.*|c/o\b.*|"
    r"\.\s*appeal of.*|"
    r"an? [a-z -]*(?:corporation|company|association)\b.*)\s*$", re.I)
_LEAD = re.compile(
    r"^(?:(?:in re:?|in the matter of|matter of|ex parte|estate of|the arbitration between|appeal of:?|"
    r"(?:plaintiffs?|defendants?|appellants?|appellees?|petitioners?|respondents?|intervenors?)(?:-\w+)? (?:and|&)|"
    r"(?:inc|llc|ltd|corp)\.,?(?= [A-Z])|(?:i{2,3}|iv|jr\.?|sr\.?) (?:and|&)|"
    r"(?:inc|llc|ltd|corp|n\.a|etc)\.?,? (?:and|&)|and|the)\s+)+", re.I)
_SUFFIX = re.compile(
    r"^(?:inc|incorporated|llc|l\.l\.c|ltd|lp|l\.p|n\.a|corp|co|etc|et\.? ?al|jr|sr|ii|iii|iv|"
    r"trustee|as trustee.*|an? [a-z -]*(?:corporation|company|association|partnership).*)\.?$", re.I)
_TAG = re.compile(r"<[^>]+>")
# California's appellate captions end with the district/division: "… CA1/2".
_DIVISION = re.compile(r"\s+CA\d(?:/\d)?$")


def clean_caption(s: str | None) -> str:
    """A caption as text: entities decoded (the files carry a bare `&39;`),
    markup dropped ("<b><font color=red>Jointly Administered…")."""
    s = html.unescape((s or "").replace("&39;", "'")).replace("\u2019", "'").replace("\u2018", "'")
    s = " ".join(_TAG.sub(" ", s).split())
    return _DIVISION.sub("", s)


_EXPANSIONS = [(re.compile(p, re.I), w) for p, w in (
    (r"\bAss'?n\b\.?|\bAssoc?\b\.?|\bAsso\b\.?|\bAssocation\b", "Association"),
    (r"\bCondo\b\.", "Condominium"),
    (r"\bBd\.", "Board"), (r"\bMgrs\.", "Managers"),
    (r"\bProp\.", "Property"), (r"\bCmty\.", "Community"),
    (r"\bHome[- ]?owner'?s?'?(?=\s|,|$)", "Homeowners"),
)]


def expand(s: str) -> str:
    """Reporter abbreviations spelled out, so one set of rules reads both
    "Lake Point Tower Condo. Ass'n" and "… Condominium Association"."""
    s = re.sub(r"\bH\.O\.A\.?", "HOA", s)
    for pattern, word in _EXPANSIONS:
        s = pattern.sub(lambda m, word=word: word.upper() if m.group(0).isupper() else word, s)
    return s


def _cut_off(token: str) -> bool:
    """A caption cut mid-word: "HOMEOWNERS ASS", "… CONDOMINIUM ASSOCIATIO"."""
    return len(token) >= 2 and any(w.startswith(token) for w in ("ASSOCIATION", "CONDOMINIUM", "HOMEOWNERS"))


def is_association(party: str) -> bool:
    """True when one caption party is a community association by its own
    words: an association word, no lender / insurer / builder / public-body
    vocabulary, no LLC or LP form, and more than generic words. "HOA" and
    "POA" count only as the last word of a longer name written in capitals —
    "Sunset Lakes HOA", never the given name in "Hoa Van Doe"."""
    n = normalize(party)
    if not n or NOT_ASSOC.search(n) or _NOT_A_PARTY.match(n) or has_business_form(party):
        return False
    words = n.split()
    if all(t in GENERIC or _cut_off(t) for t in words) or len(words) > MAX_NAME_WORDS or _CLASS.search(n):
        return False
    if words.count("ASSOCIATION") > 1 and not n.startswith("ASSOCIATION"):
        return False            # two parties run together in the caption
    if _TOWNHOME_ONLY.search(n) and not _BEYOND_TOWNHOME.search(n) \
            and not re.search(r"\b(?:inc|incorporated)\b", party, re.I):
        return False
    if STRICT.search(n):
        return True
    m = ABBR.search(n)
    if not m:
        return False
    cased = re.search(r"\b(?:HOA|POA)\b", party)
    mixed = any(c.islower() for c in party)
    return n.endswith(m.group(1)) and len(n.split()) >= 2 and (bool(cased) or not mixed)


def _marked(text: str) -> bool:
    n = normalize(text)
    return bool(STRICT.search(n) or ABBR.search(n))


def split_parties(side: str) -> list[str]:
    """The parties on one side of the "v.": split on ";" and ", ", with
    ", Inc." / ", a Florida corporation" kept on the name before it."""
    out: list[str] = []
    for part in re.split(r"\s*;\s*|,\s+(?:and\s+)?", side):
        part = part.strip()
        if not part:
            continue
        if out and _SUFFIX.match(part):
            out[-1] += ", " + part
        else:
            out.append(part)
    return out


def trim_party(party: str) -> str:
    """One party as a name: parentheticals, role words ("et al.",
    "Appellant", "a Florida corporation"), a leading "In re" and a trailing
    "… and John Doe" removed. The trailing cut is made only when what is
    left still carries the association word, so "Sand and Sea Homeowners"
    stays whole."""
    p = re.sub(r"\s*\([^)]*\)?", " ", party).strip(" ,.")
    for _ in range(3):
        p = _ROLE.sub("", p).strip(" ,.")
    p = re.sub(r"^.*\ba/s/o\s+", "", p, flags=re.I)           # an insurer suing in the association's shoes
    p = _LEAD.sub("", p).strip(" ,.")
    pieces = re.split(r"\s+(?:and|&)\s+", p, flags=re.I)
    while len(pieces) > 1 and not _marked(pieces[-1]) and _marked(" ".join(pieces[:-1])):
        p = re.sub(r"\s+(?:and|&)$", "", p[:p.rfind(pieces[-1])].rstrip(), flags=re.I)
        pieces = pieces[:-1]
    return " ".join(_drop_leading_coparty(p).split())


# Words on either side of an "and" that belong to one name: "Golf and Tennis
# Club", "Beach & Bay Resort", "Town and Country", "Sand and Sea".
_PAIR_WORDS = frozenset("""GOLF TENNIS BEACH BAY YACHT RACQUET RACKET COUNTRY TOWN SWIM RESORT SPA MARINA
CLUB LAKE LAKES RIVER OCEAN SEA SAND SURF SUN HUNT BATH PARK GARDEN GARDENS VILLAS ESTATES HILLS DALES
WOODS TOWER TOWERS HARBOR HARBOUR RESIDENCES BUILDING LAND IMPROVEMENT CIVIC EAST WEST NORTH SOUTH
I II III IV V""".split())


_OFFICER_LEAD = frozenset({"AS", "INDIVIDUALLY", "INDIV", "ALL", "REPRESENTING", "ON"})


def _drop_leading_coparty(p: str) -> str:
    """"Jane Doe and Kendall Acres Condo Association" → the association.
    The cut is made only when what comes before the "and" is at least two
    words with no association word, what follows names an association with
    a distinctive word of its own, and the two sides of the "and" are not
    words that pair up inside one name — so "Sand and Sea Homeowners
    Association", "Hollybrook Golf and Tennis Club Condominium" and
    "Kingspark and Whitehall Civic Improvement Association" stay whole. A
    party that opens "Individually and as President of …" is a person and
    is left alone for the gate to refuse."""
    pieces = re.split(r"\s+(?:and|&)\s+", p, flags=re.I)
    first = next((i for i, piece in enumerate(pieces) if _marked(piece)), 0)
    if first == 0:
        return p
    left = normalize(" ".join(pieces[:first])).split()
    right = normalize(pieces[first]).split()
    if len(left) < 2 or (left[-1] in _PAIR_WORDS and right[0] in _PAIR_WORDS) \
            or left[-1].isdigit() or right[0].isdigit() or left[0] in _OFFICER_LEAD:
        return p
    if all(t in GENERIC for t in right):
        return p
    rest = re.split(r"\s+(?:and|&)\s+", p, maxsplit=first, flags=re.I)[-1]
    return re.sub(r"^the\s+", "", rest, flags=re.I)


def find_associations(case_name: str, case_name_full: str = "") -> dict[str, str]:
    """{association: role} for one case. The full caption is read first —
    Florida's appellate dockets shorten `case_name` to "SIMS CREEK HOMEOWNERS
    v. JOHN DOE" and keep "SIMS CREEK HOMEOWNERS ASSOC., INC." in
    `case_name_full` — and the short one only when the full one names no
    association. Role is "plaintiff" for the side before the first "v.",
    "defendant" after it, "" when there is no "v." ("In re …")."""
    for text in (case_name_full, case_name):
        text = expand(clean_caption(text))
        if not text:
            continue
        found: dict[str, str] = {}
        sides = re.split(r"\s+vs?\.?\s+", text, flags=re.I)
        for i, side in enumerate(sides):
            role = "" if len(sides) == 1 else ("plaintiff" if i == 0 else "defendant")
            for party in split_parties(side):
                name = trim_party(party)
                if len(name) <= 120 and is_association(name) \
                        and not any(normalize(name) in normalize(k) for k in found):
                    found[name] = role
        if found:
            return found
    return {}


# ── step 2: one pass over a big file ──────────────────────────────────────

DOCKET_COLS = ("id", "source", "court_id", "case_name", "case_name_full", "slug", "docket_number",
               "date_filed", "date_terminated", "date_last_filing", "cause", "nature_of_suit",
               "jurisdiction_type", "appeal_from_str", "appeal_from_id", "pacer_case_id")
CLUSTER_COLS = ("id", "docket_id", "date_filed", "slug", "case_name", "case_name_full",
                "precedential_status", "source", "citation_count")


def scan(path: Path, cols: tuple[str, ...], also_ids: frozenset[str] = frozenset(),
         stats: Counter | None = None):
    """Yield the rows of a bulk file worth a second look, cut down to `cols`:
    a caption BROAD matches, or (dockets) an id in `also_ids` — the dockets
    of the opinions already kept, read for their court. Blocked rows are
    never yielded."""
    stats = stats if stats is not None else Counter()
    started = time.time()
    with open_rows(path) as rows:
        head = next(rows)
        col = {c: i for i, c in enumerate(head)}
        keep = [(c, col[c]) for c in cols]
        name, full, blocked = col["case_name"], col["case_name_full"], col["blocked"]
        for row in rows:
            stats["rows"] += 1
            if stats["rows"] % 5_000_000 == 0:
                log.info("%s: %s rows, %s kept, %.0fs", path.name, f"{stats['rows']:,}",
                         f"{stats['kept']:,}", time.time() - started)
            if len(row) != len(head):
                stats["malformed"] += 1
                continue
            wanted = row[0] in also_ids
            if not wanted and not BROAD.search(row[name] + " | " + row[full]):
                continue
            if row[blocked] == "t":
                stats["blocked"] += 1
                continue
            stats["kept"] += 1
            rec = {c: row[i] for c, i in keep}
            if wanted:
                rec["for_opinion"] = True
            yield rec


def scan_to(path: Path, dest: Path, cols: tuple[str, ...],
            also_ids: frozenset[str] = frozenset()) -> Counter:
    stats: Counter = Counter()
    tmp = dest.with_suffix(".tmp")
    with tmp.open("w") as out:
        for rec in scan(path, cols, also_ids, stats):
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(dest)
    log.info("%s: %s rows read, %s kept, %s blocked, %s malformed", path.name,
             f"{stats['rows']:,}", f"{stats['kept']:,}", stats["blocked"], stats["malformed"])
    return stats


# ── step 3: records ───────────────────────────────────────────────────────

def _court(row: dict, courts: dict) -> tuple[dict, str]:
    """The court of a docket row and the case's state: the court's own, or
    for a federal court of appeals the state of the court appealed from."""
    court = courts.get(row.get("court_id") or "", {"state": "", "jurisdiction": "", "name": ""})
    state = court["state"] or courts.get(row.get("appeal_from_id") or "", {}).get("state", "")
    return court, state


def docket_record(row: dict, courts: dict, snapshot: str, now: str) -> dict | None:
    """One scanned docket row as a dockets.jsonl-shaped record, or None when
    no party in its caption is an association."""
    assocs = find_associations(row["case_name"], row["case_name_full"])
    if not assocs:
        return None
    court, state = _court(row, courts)
    return {
        "docket_id": int(row["id"]),
        "case_name": clean_caption(row["case_name"] or row["case_name_full"])[:300],
        "court": court["name"], "court_id": row["court_id"],
        "jurisdiction": court["jurisdiction"],
        "docket_number": row["docket_number"],
        "date_filed": row["date_filed"], "date_terminated": row["date_terminated"],
        "nature_of_suit": row["nature_of_suit"], "cause": row["cause"],
        "state": state, "associations": sorted(assocs), "association_role": sorted({r for r in assocs.values() if r}),
        "url": f"{BASE_URL}/docket/{row['id']}/{row['slug']}/",
        "source": "courtlistener-bulk", "queries": [f"bulk-data {snapshot}"],
        "retrieved_at": now,
    }


def opinion_record(row: dict, docket: dict | None, courts: dict, snapshot: str, now: str) -> dict | None:
    """One scanned opinion-cluster row as an opinions.jsonl-shaped record.
    The cluster does not say which court decided it — its docket does — so
    a cluster whose docket was not kept (blocked, or missing) is dropped."""
    assocs = find_associations(row["case_name"], row["case_name_full"])
    if not assocs or docket is None:
        return None
    court, state = _court(docket, courts)
    return {
        "cluster_id": int(row["id"]), "docket_id": int(row["docket_id"]),
        "case_name": clean_caption(row["case_name"] or row["case_name_full"])[:300],
        "court": court["name"], "court_id": docket["court_id"],
        "jurisdiction": court["jurisdiction"],
        "docket_number": docket["docket_number"],
        "state": state, "date_filed": row["date_filed"],
        "status": row["precedential_status"],
        "associations": sorted(assocs), "association_role": sorted({r for r in assocs.values() if r}),
        "url": f"{BASE_URL}/opinion/{row['id']}/{row['slug']}/",
        "source": "courtlistener-bulk-opinions", "queries": [f"bulk-data {snapshot}"],
        "retrieved_at": now,
    }


def _jsonl(path: Path):
    with path.open() as fh:
        for line in fh:
            yield json.loads(line)


def refine(cluster_rows, docket_rows, courts: dict, snapshot: str, now: str) -> tuple[list, list, Counter]:
    """(docket records, opinion records, counts) from the two scans."""
    stats: Counter = Counter()
    by_id, dockets = {}, []
    for row in docket_rows:
        by_id[row["id"]] = row
        rec = docket_record(row, courts, snapshot, now)
        stats["dockets kept" if rec else "docket captions without an association party"] += 1
        if rec:
            dockets.append(rec)
    opinions = []
    for row in cluster_rows:
        rec = opinion_record(row, by_id.get(row["docket_id"]), courts, snapshot, now)
        if rec:
            opinions.append(rec)
            stats["opinions kept"] += 1
        elif row["docket_id"] not in by_id:
            stats["opinions without a docket row"] += 1
        else:
            stats["opinion captions without an association party"] += 1
    return dockets, opinions, stats


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)


def coverage_entries(dockets: list[dict], opinions: list[dict], snapshot: str) -> dict[str, dict]:
    """{state: {coverage key: info}} — what this run found per state, in
    the shape coverage.json's `collected` block holds."""
    out: dict[str, dict] = {}
    for key, label, items in (("courts_bulk_dockets", "dockets", dockets),
                              ("courts_bulk_opinions", "opinions", opinions)):
        for state, n in Counter(d["state"] for d in items if d["state"]).items():
            out.setdefault(state, {})[key] = {
                "source": f"CourtListener bulk data — {label[:-1]} captions",
                "url": SOURCE_PAGE, label: n, "snapshot": snapshot}
    return out


def write_coverage(path: Path, entries: dict[str, dict]) -> None:
    """Record the per-state counts in coverage.json: one read, one write,
    only this collector's two keys touched."""
    cov = json.loads(path.read_text()) if path.exists() else {"states": {}}
    for state, keys in entries.items():
        entry = cov["states"].setdefault(state, {"name": STATE_NAMES.get(state, state), "collected": {}})
        entry.setdefault("collected", {}).update(keys)
        if entry.get("status") in (None, "pending", "researching"):
            entry["status"] = "collected"
    cov["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(cov, indent=2))


def merge_sources(path: Path, entry: dict) -> None:
    """Add this collector's entry to courts/sources.json without touching
    what the other court collectors wrote there."""
    data = json.loads(path.read_text()) if path.exists() else {}
    data["courtlistener_bulk"] = entry
    path.write_text(json.dumps(data, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help="where the downloads and the step-2 extracts are kept")
    ap.add_argument("--snapshot", help="bulk-data date, YYYY-MM-DD (default: the latest complete one)")
    ap.add_argument("--connections", type=int, default=8, help="parallel range requests per file")
    ap.add_argument("--refine-only", action="store_true",
                    help="skip download and scan; re-run step 3 on the cached extracts")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    started = time.time()
    snapshot = args.snapshot
    if args.refine_only and not snapshot:
        done = sorted(p.name[len("hoa_dockets-"):-len(".jsonl")]
                      for p in args.cache.glob("hoa_dockets-*.jsonl"))
        if not done:
            log.error("no cached extract in %s — run without --refine-only first", args.cache)
            return 1
        snapshot = done[-1]

    def bulk(table: str) -> Path:
        return args.cache / f"{table}-{snapshot}.csv.bz2"

    if not args.refine_only:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        available = snapshots(list_bucket(session))
        if not available:
            log.error("no complete snapshot under %s%s", BUCKET, PREFIX)
            return 1
        snapshot = snapshot or max(available)
        if snapshot not in available:
            log.error("snapshot %s not in the bucket; have %s", snapshot, ", ".join(sorted(available)))
            return 1
        log.info("snapshot %s", snapshot)
        for table in TABLES:
            key, size = available[snapshot][table]
            download(key, size, bulk(table), args.connections)

    clusters_x = args.cache / f"hoa_clusters-{snapshot}.jsonl"
    dockets_x = args.cache / f"hoa_dockets-{snapshot}.jsonl"
    if not args.refine_only:
        # Opinions first: their docket ids tell the docket pass which extra
        # rows to keep for the court.
        if not clusters_x.exists():
            scan_to(bulk("opinion-clusters"), clusters_x, CLUSTER_COLS)
        if not dockets_x.exists():
            ids = frozenset(r["docket_id"] for r in _jsonl(clusters_x))
            scan_to(bulk("dockets"), dockets_x, DOCKET_COLS, ids)

    now = datetime.now(timezone.utc).isoformat()
    courts = court_table(bulk("courts"))
    dockets, opinions, stats = refine(_jsonl(clusters_x), _jsonl(dockets_x), courts, snapshot, now)
    write_jsonl(args.out / "bulk_dockets.jsonl", dockets)
    write_jsonl(args.out / "bulk_opinions.jsonl", opinions)
    merge_sources(args.out / "sources.json", {
        "name": "CourtListener bulk data (Free Law Project)",
        "url": SOURCE_PAGE,
        "bucket": BUCKET + PREFIX,
        "snapshot": snapshot,
        "access": "anonymous download of the quarterly bulk CSV files; no API, no rate limit",
        "coverage": "every docket and opinion caption CourtListener holds: federal district, "
                    "bankruptcy and appellate courts, state supreme and appellate courts, "
                    "and the state trial courts it has",
        "caveat": ("captions only — the bulk files carry no party lists, so a case where the "
                   "association is not in the caption is missed; association names are read out "
                   "of the caption by rule and can be truncated; overlaps dockets.jsonl and "
                   "opinions.jsonl (same docket_id / url); CourtListener-blocked rows are skipped"),
        "dockets": len(dockets), "opinions": len(opinions),
        "retrieved_at": now, "duration_seconds": round(time.time() - started, 1),
    })
    if args.out.resolve() == DEFAULT_OUT.resolve():
        write_coverage(COVERAGE, coverage_entries(dockets, opinions, snapshot))
    for key, n in sorted(stats.items()):
        log.info("%-48s %s", key, f"{n:,}")
    for label, items in (("bulk dockets", dockets), ("bulk opinions", opinions)):
        by_state = Counter(d["state"] for d in items if d["state"])
        top = ", ".join(f"{s} {n:,}" for s, n in by_state.most_common(8))
        print(f"{len(items):,} {label} — top states: {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
