"""Court cases naming community associations from CourtListener's quarterly
bulk data (Free Law Project): every docket and every opinion whose caption
carries an association, in every court CourtListener holds — federal
district, bankruptcy and appellate, state supreme and appellate, and the
state trial courts it has.

    python -m hoaspy courts-bulk                       # the latest complete snapshot
    python -m hoaspy courts-bulk --snapshot 2026-09-30
    python -m hoaspy courts-bulk --refine-only         # re-run the name rules on the cached extract

The search API (the courts command) answers five phrases and throttles an
anonymous client hard. The bulk files have no limit and no query grammar:
this collector downloads three of them from the public S3 bucket, reads
every caption once and keeps the ones where a party is a homeowners,
condominium or property owners association — including the abbreviated
captions state reporters print ("Homeowners Ass'n", "Condo. Assn.") that a
phrase search never returns. Three steps, each skipped when its result is
already in the cache (default .cache/courtlistener_bulk/, ~7.7 GB):

1. download courts-, opinion-clusters- and dockets-<date>.csv.bz2
   (parallel range requests, resumable);
2. scan the two big files once each, keeping the rows whose caption has
   an association word (hoa_clusters-<date>.jsonl, hoa_dockets-<date>.jsonl
   in the cache) — about an hour, almost all of it bzip2;
3. refine: split each caption into parties, keep the association-shaped
   ones, and write the records.

Output (default courts/): bulk_dockets.jsonl (Case records plus
`docket_id`, `court_id` and `jurisdiction`) and bulk_opinions.jsonl
(Opinion records plus `cluster_id`, `docket_id`, `court_id`,
`jurisdiction`, `docket_number` and `status`), a `courtlistener_bulk`
entry in sources.json and, when writing to the default folder, per-state
counts in coverage.json. Both overlap what the courts command finds (the
same docket_id and url), so a consumer of both de-duplicates on those.

The bulk files carry no party lists: a bankruptcy captioned "In re John
Doe" where the association is only a creditor is found by the courts
command (party: search), not here. Rows CourtListener marks `blocked`
(kept out of search engines, mostly bankruptcies) are skipped.
"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import csv
import html
import io
import logging
import re
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.courts import COURTS_DIR
from hoaspy.collect.courts.court_portals._common import has_business_form, normalize
from hoaspy.lib import coverage, http
from hoaspy.lib.jsonl import iter_rows, merge_json, now_iso, write_rows
from hoaspy.lib.records import Case, Opinion
from hoaspy.lib.states import STATE_NAMES, state_in

DEFAULT_CACHE_DIR = ROOT / ".cache" / "courtlistener_bulk"
COVERAGE_PATH = ROOT / "coverage.json"
BUCKET_URL = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
KEY_PREFIX = "bulk-data/"
SOURCE_PAGE = "https://www.courtlistener.com/help/api/bulk-data/"
SITE_URL = "https://www.courtlistener.com"
DOCKET_SOURCE = "courtlistener-bulk"
OPINION_SOURCE = "courtlistener-bulk-opinions"
TABLES = ("courts", "opinion-clusters", "dockets")
MIN_PART_BYTES = 64 << 20
MAX_CAPTION = 300
MAX_NAME_LENGTH = 120
MAX_NAME_WORDS = 12

log = logging.getLogger(__name__)


# ---- the bucket -------------------------------------------------------------

def parse_listing(xml: str) -> tuple[list[tuple[str, int]], str]:
    """(key, size) pairs of one ListObjectsV2 page and the continuation token ('' on the last page)."""
    items = [(key, int(size)) for key, size in re.findall(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", xml, re.S)]
    match = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
    return items, html.unescape(match.group(1)) if match else ""


def snapshots(items: list[tuple[str, int]]) -> dict[str, dict[str, tuple[str, int]]]:
    """{date: {table: (key, size)}} for every snapshot that has all of TABLES."""
    found: dict[str, dict[str, tuple[str, int]]] = {}
    pattern = re.compile(re.escape(KEY_PREFIX) + r"(" + "|".join(TABLES) + r")-(\d{4}-\d{2}-\d{2})\.csv\.bz2")
    for key, size in items:
        match = pattern.fullmatch(key)
        if match:
            found.setdefault(match.group(2), {})[match.group(1)] = (key, size)
    return {date: tables for date, tables in found.items() if len(tables) == len(TABLES)}


def list_bucket(session: requests.Session) -> list[tuple[str, int]]:
    items, token = [], ""
    while True:
        params = {"list-type": "2", "prefix": KEY_PREFIX, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        response = session.get(BUCKET_URL, params=params, timeout=60)
        response.raise_for_status()
        page, token = parse_listing(response.text)
        items += page
        if not token:
            return items


def plan_parts(size: int, connections: int, min_part: int = MIN_PART_BYTES) -> list[tuple[int, int]]:
    """Inclusive (start, end) byte ranges covering `size`: one per connection, fewer for a small file."""
    count = max(1, min(connections, size // min_part or 1))
    step = -(-size // count)
    return [(i * step, min(size, (i + 1) * step) - 1) for i in range(count) if i * step < size]


def download(key: str, size: int, dest: Path, connections: int = 8) -> None:
    """Fetch one bucket object with parallel range requests. Each part
    resumes from what is already on disk, so a killed run loses nothing."""
    if dest.exists() and dest.stat().st_size == size:
        log.info("have %s", dest.name)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = plan_parts(size, connections)

    def fetch(index: int) -> Path:
        start, end = parts[index]
        path = dest.with_name(f"{dest.name}.{len(parts)}part{index:02d}")
        wanted = end - start + 1
        for attempt in range(10):
            have = path.stat().st_size if path.exists() else 0
            if have >= wanted:
                return path
            try:
                with requests.get(BUCKET_URL + key, stream=True, timeout=120,
                                  headers={"Range": f"bytes={start + have}-{end}",
                                           "User-Agent": http.USER_AGENT}) as response:
                    response.raise_for_status()
                    with path.open("ab") as fh:
                        for block in response.iter_content(1 << 20):
                            fh.write(block)
            except requests.RequestException as exc:
                log.warning("%s part %d: %s — retrying", dest.name, index, exc)
                time.sleep(5 * (attempt + 1))
        if path.stat().st_size < wanted:
            raise OSError(f"{dest.name} part {index} incomplete after retries")
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
        raise OSError(f"{dest.name}: {tmp.stat().st_size} bytes, expected {size}")
    tmp.replace(dest)
    for path in paths:
        path.unlink()


# ---- reading the CSV ----------------------------------------------------------

@contextlib.contextmanager
def open_rows(path: Path):
    """The csv rows of a bulk file (.csv or .csv.bz2). The files are
    PostgreSQL `COPY … WITH (FORMAT csv, ESCAPE '\\')`: quotes inside a
    field are `\\"`, fields may span lines, NULL is an unquoted empty field.
    The bzip2 program is used when installed — several times faster than
    the module."""
    csv.field_size_limit(1 << 30)
    process = None
    if path.suffix == ".bz2":
        executable = shutil.which("bzip2")
        if executable:
            process = subprocess.Popen([executable, "-dc", str(path)], stdout=subprocess.PIPE)
            raw = process.stdout
        else:
            raw = bz2.open(path, "rb")
    else:
        raw = path.open("rb")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    try:
        yield csv.reader(text, escapechar="\\", doublequote=False)
    finally:
        text.close()
        if process is not None and process.wait() != 0:
            raise OSError(f"bzip2 failed on {path.name} (truncated download?)")


def court_table(path: Path) -> dict[str, dict]:
    """{court id: {state, jurisdiction, name}} from the courts file. The
    state is read out of the court's full name; `jurisdiction` is
    CourtListener's code (FD district, FB bankruptcy, F federal appellate,
    S / SA state supreme / appellate, ST state trial …)."""
    out = {}
    with open_rows(path) as rows:
        head = next(rows)
        column = {name: i for i, name in enumerate(head)}
        for row in rows:
            if len(row) != len(head):
                continue
            name = row[column["full_name"]]
            out[row[column["id"]]] = {"state": state_in(name), "jurisdiction": row[column["jurisdiction"]],
                                      "name": name}
    return out


# ---- which captions, which parties ---------------------------------------------

# Step 2's net: wide, cheap, run on 80+ million captions. Step 3 decides.
BROAD_RE = re.compile(
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
STRICT_RE = re.compile(
    r"HOMEOWNERS?\b|HOME OWNERS?\b|CONDOMINIUMS?\b|\bCONDOS?\b|PROPERTY OWNERS?\b|UNIT OWNERS?\b|"
    r"LOT OWNERS?\b|APARTMENT OWNERS\b|\bCO OWNERS\b|TOWNHOMES?\b|TOWNHOUSES?\b|"
    r"\b(?:OWNERS|COMMUNITY|MASTER|CIVIC|IMPROVEMENT|HOMES|RESIDENTS|NEIGHBORHOOD|MAINTENANCE|"
    r"RECREATION|PROPERTY|VILLAS?|ESTATES|VILLAGE|LAKES?|LANDOWNERS) ASSOCIATION\b|"
    r"COUNCIL (?:UNIT |CO )?OWNERS\b|COOPERATIVE APARTMENTS?\b|HOUSING COOPERATIVE\b|"
    r"\bASSOCIATION (?:APARTMENT |UNIT |HOME |PROPERTY |LOT |CO )?OWNERS\b")
ABBREVIATION_RE = re.compile(r"\b(HOA|POA)\b")
# Lenders, insurers, builders, public bodies and trade groups that carry an
# association word: "Home Owners' Loan Corporation", "Bank of America,
# National Association", "Standardbred Owners Association".
NOT_ASSOCIATION_RE = re.compile(
    r"\bLOAN\b|WARRANTY|\bCHOICE\b|MUTUAL|SAVINGS|INSURANCE|ASSURANCE|\bBANK\b|MORTGAGE|"
    r"\bREALTY\b|DEVELOPERS?\b|DEVELOPMENT\b|CONSTRUCTION|\bBUILDERS\b|SCHOOL|COLLEGE|"
    r"UNIVERSITY|HOSPITAL|\bCOUNTY\b|\bCITY\b|BAR ASSOCIATION|EDUCATION ASSOCIATION|\bUNION\b|"
    r"TEACHERS|EMPLOYEES|\bPOLICE\b|FIRE ?FIGHTERS|MEDICAL|\bTITLE\b|MANAGEMENT|"
    r"COMMUNITY ASSOCIATIONS INSTITUTE|UNDERWRITERS|\bTAX\b|\bUNITED STATES\b|STANDARDBRED|"
    r"THOROUGHBRED|HORSE|CATTLE|\bTAXI|\bCAB\b|TRUCK|\bBOAT|VESSEL|AIRCRAFT|PILOTS|LIQUOR|"
    r"THEAT(?:RE|ER)|HOTEL|MOTEL|SERVICE STATION|BUSINESS OWNERS|RESTAURANT|TAVERN|"
    r"NATIONAL ASSOCIATION|\bREPAIR\b|CONSULTING|\bBEHALF\b|\bLOCATED\b")
NOT_A_PARTY_RE = re.compile(
    r"(?:ON BEHALF|SUING|PROCEEDS|ALL OTHER|CERTAIN|UNKNOWN|REAL PROPERTY|INDIVIDUALLY|INDIV|REPRESENTING|"
    r"AS (?:OFFICERS?|MEMBERS?|REPRESENTATIVES?|TRUSTEES?|PRESIDENT|TREASURER|DIRECTORS?|INDIVIDUAL)|"
    r"HUSBAND|ALL INDIVIDUALLY|CIVIL NO|PARCEL|PARENTS)\b")
# A class of owners, not an association: "… and All Other Property Owners
# in the Subdivision", "a Class of Homeowners Residing in Keauhou".
CLASS_RE = re.compile(r"\bOTHER (?:PROPERTY )?OWNERS\b|TAXPAYERS|\bCLASS\b|SIMILARLY SITUATED|\bRESIDING\b|"
                      r"\bOTHER HOMEOWNERS\b|OTHER RESIDENTS")
# "Roseland Townhomes" is as often a rental complex as an association;
# alone, the word counts only on a corporation ("Kendall Walk Townhomes, Inc").
TOWNHOME_ONLY_RE = re.compile(r"TOWNHOMES?\b|TOWNHOUSES?\b")
BEYOND_TOWNHOME_RE = re.compile(r"ASSOCIATION|OWNERS|CONDO|\bHOA\b|\bPOA\b|COUNCIL|COOPERATIVE|HOMEOWNER")
# Words that name no particular association: a party made only of these
# ("Homeowners", "Property Owners", "Condos") is a truncated caption.
GENERIC_WORDS = frozenset("""ASSOCIATION ASSOCIATIONS HOMEOWNERS HOMEOWNER HOME OWNERS OWNER PROPERTY
CONDOMINIUM CONDOMINIUMS CONDO CONDOS COMMUNITY MASTER TOWNHOME TOWNHOMES TOWNHOUSE TOWNHOUSES UNIT
COUNCIL BOARD MANAGERS DIRECTORS IMPROVEMENT CIVIC RESIDENTS MAINTENANCE NO PHASE SECTION HOA POA
ALL OTHER I II III IV V 1 2 3 4 5""".split())
ROLE_RE = re.compile(
    r",?\s*\b(?:et\.? ?al\.?|etc\.?|petitioners?(?:\(s\))?|respondents?(?:\(s\))?|appellants?|appellees?|"
    r"plaintiffs?|defendants?|cross-\w+|intervenors?|individually|a/k/a.*|aka\b.*|f/k/a.*|n/k/a.*|d/b/a.*|c/o\b.*|"
    r"\.\s*appeal of.*|"
    r"an? [a-z -]*(?:corporation|company|association)\b.*)\s*$", re.I)
LEAD_RE = re.compile(
    r"^(?:(?:in re:?|in the matter of|matter of|ex parte|estate of|the arbitration between|appeal of:?|"
    r"(?:plaintiffs?|defendants?|appellants?|appellees?|petitioners?|respondents?|intervenors?)(?:-\w+)? (?:and|&)|"
    r"(?:inc|llc|ltd|corp)\.,?(?= [A-Z])|(?:i{2,3}|iv|jr\.?|sr\.?) (?:and|&)|"
    r"(?:inc|llc|ltd|corp|n\.a|etc)\.?,? (?:and|&)|and|the)\s+)+", re.I)
SUFFIX_RE = re.compile(
    r"^(?:inc|incorporated|llc|l\.l\.c|ltd|lp|l\.p|n\.a|corp|co|etc|et\.? ?al|jr|sr|ii|iii|iv|"
    r"trustee|as trustee.*|an? [a-z -]*(?:corporation|company|association|partnership).*)\.?$", re.I)
TAG_RE = re.compile(r"<[^>]+>")
# California's appellate captions end with the district and division: "… CA1/2".
DIVISION_RE = re.compile(r"\s+CA\d(?:/\d)?$")
# Reporter abbreviations, spelled out so one set of rules reads both
# "Lake Point Tower Condo. Ass'n" and "… Condominium Association".
EXPANSIONS = [(re.compile(pattern, re.I), word) for pattern, word in (
    (r"\bAss'?n\b\.?|\bAssoc?\b\.?|\bAsso\b\.?|\bAssocation\b", "Association"),
    (r"\bCondo\b\.", "Condominium"),
    (r"\bBd\.", "Board"), (r"\bMgrs\.", "Managers"),
    (r"\bProp\.", "Property"), (r"\bCmty\.", "Community"),
    (r"\bHome[- ]?owner'?s?'?(?=\s|,|$)", "Homeowners"),
)]
# Words on either side of an "and" that belong to one name: "Golf and Tennis
# Club", "Beach & Bay Resort", "Town and Country", "Sand and Sea".
PAIR_WORDS = frozenset("""GOLF TENNIS BEACH BAY YACHT RACQUET RACKET COUNTRY TOWN SWIM RESORT SPA MARINA
CLUB LAKE LAKES RIVER OCEAN SEA SAND SURF SUN HUNT BATH PARK GARDEN GARDENS VILLAS ESTATES HILLS DALES
WOODS TOWER TOWERS HARBOR HARBOUR RESIDENCES BUILDING LAND IMPROVEMENT CIVIC EAST WEST NORTH SOUTH
I II III IV V""".split())
OFFICER_LEAD = frozenset({"AS", "INDIVIDUALLY", "INDIV", "ALL", "REPRESENTING", "ON"})


def clean_caption(text: str | None) -> str:
    """A caption as text: entities decoded (the files carry a bare `&39;`),
    markup dropped ("<b><font color=red>Jointly Administered…")."""
    text = html.unescape((text or "").replace("&39;", "'")).replace("’", "'").replace("‘", "'")
    text = " ".join(TAG_RE.sub(" ", text).split())
    return DIVISION_RE.sub("", text)


def expand(text: str) -> str:
    """The reporter abbreviations spelled out, matching the case of what they replace."""
    text = re.sub(r"\bH\.O\.A\.?", "HOA", text)
    for pattern, word in EXPANSIONS:
        text = pattern.sub(lambda match, word=word: word.upper() if match.group(0).isupper() else word, text)
    return text


def cut_off(token: str) -> bool:
    """A caption cut mid-word: "HOMEOWNERS ASS", "… CONDOMINIUM ASSOCIATIO"."""
    return len(token) >= 2 and any(word.startswith(token) for word in ("ASSOCIATION", "CONDOMINIUM", "HOMEOWNERS"))


def is_association(party: str) -> bool:
    """True when one caption party is a community association by its own
    words: an association word, no lender, insurer, builder or public-body
    vocabulary, no LLC or LP form, and more than generic words. "HOA" and
    "POA" count only as the last word of a longer name written in capitals:
    "Sunset Lakes HOA", never the given name in "Hoa Van Doe"."""
    key = normalize(party)
    if not key or NOT_ASSOCIATION_RE.search(key) or NOT_A_PARTY_RE.match(key) or has_business_form(party):
        return False
    words = key.split()
    if all(word in GENERIC_WORDS or cut_off(word) for word in words) or len(words) > MAX_NAME_WORDS \
            or CLASS_RE.search(key):
        return False
    if words.count("ASSOCIATION") > 1 and not key.startswith("ASSOCIATION"):
        return False            # two parties run together in the caption
    if TOWNHOME_ONLY_RE.search(key) and not BEYOND_TOWNHOME_RE.search(key) \
            and not re.search(r"\b(?:inc|incorporated)\b", party, re.I):
        return False
    if STRICT_RE.search(key):
        return True
    match = ABBREVIATION_RE.search(key)
    if not match:
        return False
    cased = re.search(r"\b(?:HOA|POA)\b", party)
    mixed = any(char.islower() for char in party)
    return key.endswith(match.group(1)) and len(key.split()) >= 2 and (bool(cased) or not mixed)


def has_marker(text: str) -> bool:
    key = normalize(text)
    return bool(STRICT_RE.search(key) or ABBREVIATION_RE.search(key))


def split_parties(side: str) -> list[str]:
    """The parties on one side of the "v.": split on ";" and ", ", with
    ", Inc." / ", a Florida corporation" kept on the name before it."""
    out: list[str] = []
    for part in re.split(r"\s*;\s*|,\s+(?:and\s+)?", side):
        part = part.strip()
        if not part:
            continue
        if out and SUFFIX_RE.match(part):
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
    name = re.sub(r"\s*\([^)]*\)?", " ", party).strip(" ,.")
    for _ in range(3):
        name = ROLE_RE.sub("", name).strip(" ,.")
    name = re.sub(r"^.*\ba/s/o\s+", "", name, flags=re.I)           # an insurer suing in the association's shoes
    name = LEAD_RE.sub("", name).strip(" ,.")
    pieces = re.split(r"\s+(?:and|&)\s+", name, flags=re.I)
    while len(pieces) > 1 and not has_marker(pieces[-1]) and has_marker(" ".join(pieces[:-1])):
        name = re.sub(r"\s+(?:and|&)$", "", name[:name.rfind(pieces[-1])].rstrip(), flags=re.I)
        pieces = pieces[:-1]
    return " ".join(drop_leading_coparty(name).split())


def drop_leading_coparty(name: str) -> str:
    """"Jane Doe and Kendall Acres Condo Association" -> the association.
    The cut is made only when what comes before the "and" is at least two
    words with no association word, what follows names an association with
    a distinctive word of its own, and the two sides of the "and" are not
    words that pair up inside one name — so "Sand and Sea Homeowners
    Association", "Hollybrook Golf and Tennis Club Condominium" and
    "Kingspark and Whitehall Civic Improvement Association" stay whole. A
    party that opens "Individually and as President of …" is a person and
    is left alone for the gate to refuse."""
    pieces = re.split(r"\s+(?:and|&)\s+", name, flags=re.I)
    first = next((i for i, piece in enumerate(pieces) if has_marker(piece)), 0)
    if first == 0:
        return name
    left = normalize(" ".join(pieces[:first])).split()
    right = normalize(pieces[first]).split()
    if len(left) < 2 or (left[-1] in PAIR_WORDS and right[0] in PAIR_WORDS) \
            or left[-1].isdigit() or right[0].isdigit() or left[0] in OFFICER_LEAD:
        return name
    if all(word in GENERIC_WORDS for word in right):
        return name
    rest = re.split(r"\s+(?:and|&)\s+", name, maxsplit=first, flags=re.I)[-1]
    return re.sub(r"^the\s+", "", rest, flags=re.I)


def find_associations(case_name: str, case_name_full: str = "") -> dict[str, str]:
    """{association: role} for one case. The full caption is read first —
    Florida's appellate dockets shorten `case_name` to "SIMS CREEK HOMEOWNERS
    v. JOHN DOE" and keep "SIMS CREEK HOMEOWNERS ASSOC., INC." in
    `case_name_full` — and the short one only when the full one names no
    association. The role is "plaintiff" for the side before the first
    "v.", "defendant" after it, "" when there is no "v." ("In re …")."""
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
                if len(name) <= MAX_NAME_LENGTH and is_association(name) \
                        and not any(normalize(name) in normalize(known) for known in found):
                    found[name] = role
        if found:
            return found
    return {}


# ---- step 2: one pass over a big file ---------------------------------------------

DOCKET_COLS = ("id", "source", "court_id", "case_name", "case_name_full", "slug", "docket_number",
               "date_filed", "date_terminated", "date_last_filing", "cause", "nature_of_suit",
               "jurisdiction_type", "appeal_from_str", "appeal_from_id", "pacer_case_id")
CLUSTER_COLS = ("id", "docket_id", "date_filed", "slug", "case_name", "case_name_full",
                "precedential_status", "source", "citation_count")
PROGRESS_EVERY = 5_000_000


def scan(path: Path, cols: tuple[str, ...], also_ids: frozenset[str] = frozenset(), stats: Counter | None = None):
    """Yield the rows of a bulk file worth a second look, cut down to `cols`:
    a caption BROAD_RE matches, or (dockets) an id in `also_ids` — the
    dockets of the opinions already kept, read for their court. Blocked
    rows are never yielded."""
    stats = stats if stats is not None else Counter()
    started = time.time()
    with open_rows(path) as rows:
        head = next(rows)
        column = {name: i for i, name in enumerate(head)}
        keep = [(name, column[name]) for name in cols]
        name_at, full_at, blocked_at = column["case_name"], column["case_name_full"], column["blocked"]
        for row in rows:
            stats["rows"] += 1
            if stats["rows"] % PROGRESS_EVERY == 0:
                log.info("%s: %s rows, %s kept, %.0fs", path.name, f"{stats['rows']:,}", f"{stats['kept']:,}",
                         time.time() - started)
            if len(row) != len(head):
                stats["malformed"] += 1
                continue
            wanted = row[0] in also_ids
            if not wanted and not BROAD_RE.search(row[name_at] + " | " + row[full_at]):
                continue
            if row[blocked_at] == "t":
                stats["blocked"] += 1
                continue
            stats["kept"] += 1
            kept = {name: row[i] for name, i in keep}
            if wanted:
                kept["for_opinion"] = True
            yield kept


def scan_to(path: Path, dest: Path, cols: tuple[str, ...], also_ids: frozenset[str] = frozenset()) -> Counter:
    stats: Counter = Counter()
    write_rows(dest, scan(path, cols, also_ids, stats))
    log.info("%s: %s rows read, %s kept, %s blocked, %s malformed", path.name,
             f"{stats['rows']:,}", f"{stats['kept']:,}", stats["blocked"], stats["malformed"])
    return stats


# ---- step 3: records --------------------------------------------------------------

def court_of(row: dict, courts: dict) -> tuple[dict, str]:
    """(the court of a docket row, the case's state): the court's own, or
    for a federal court of appeals the state of the court appealed from."""
    court = courts.get(row.get("court_id") or "", {"state": "", "jurisdiction": "", "name": ""})
    state = court["state"] or courts.get(row.get("appeal_from_id") or "", {}).get("state", "")
    return court, state


def docket_record(row: dict, courts: dict, snapshot: str, retrieved_at: str) -> dict | None:
    """One scanned docket row as a bulk_dockets.jsonl row, or None when no
    party in its caption is an association."""
    associations = find_associations(row["case_name"], row["case_name_full"])
    if not associations:
        return None
    court, state = court_of(row, courts)
    return Case(
        case_name=clean_caption(row["case_name"] or row["case_name_full"])[:MAX_CAPTION], court=court["name"],
        docket_number=row["docket_number"], date_filed=row["date_filed"], date_terminated=row["date_terminated"],
        nature_of_suit=row["nature_of_suit"], cause=row["cause"], state=state, associations=sorted(associations),
        association_role=sorted({role for role in associations.values() if role}),
        url=f"{SITE_URL}/docket/{row['id']}/{row['slug']}/", source=DOCKET_SOURCE, queries=[f"bulk-data {snapshot}"],
        retrieved_at=retrieved_at,
        extras={"docket_id": int(row["id"]), "court_id": row["court_id"],
                "jurisdiction": court["jurisdiction"]}).as_row()


def opinion_record(row: dict, docket: dict | None, courts: dict, snapshot: str, retrieved_at: str) -> dict | None:
    """One scanned opinion-cluster row as a bulk_opinions.jsonl row. The
    cluster does not say which court decided it — its docket does — so a
    cluster whose docket was not kept (blocked, or missing) is dropped."""
    associations = find_associations(row["case_name"], row["case_name_full"])
    if not associations or docket is None:
        return None
    court, state = court_of(docket, courts)
    return Opinion(
        case_name=clean_caption(row["case_name"] or row["case_name_full"])[:MAX_CAPTION], court=court["name"],
        state=state, date_filed=row["date_filed"], associations=sorted(associations),
        association_role=sorted({role for role in associations.values() if role}),
        url=f"{SITE_URL}/opinion/{row['id']}/{row['slug']}/", source=OPINION_SOURCE,
        queries=[f"bulk-data {snapshot}"], retrieved_at=retrieved_at,
        extras={"cluster_id": int(row["id"]), "docket_id": int(row["docket_id"]), "court_id": docket["court_id"],
                "jurisdiction": court["jurisdiction"], "docket_number": docket["docket_number"],
                "status": row["precedential_status"]}).as_row()


def refine(cluster_rows, docket_rows, courts: dict, snapshot: str, retrieved_at: str) -> tuple[list, list, Counter]:
    """(docket rows, opinion rows, counts) from the two scans."""
    stats: Counter = Counter()
    by_id, dockets = {}, []
    for row in docket_rows:
        by_id[row["id"]] = row
        docket = docket_record(row, courts, snapshot, retrieved_at)
        stats["dockets kept" if docket else "docket captions without an association party"] += 1
        if docket:
            dockets.append(docket)
    opinions = []
    for row in cluster_rows:
        opinion = opinion_record(row, by_id.get(row["docket_id"]), courts, snapshot, retrieved_at)
        if opinion:
            opinions.append(opinion)
            stats["opinions kept"] += 1
        elif row["docket_id"] not in by_id:
            stats["opinions without a docket row"] += 1
        else:
            stats["opinion captions without an association party"] += 1
    return dockets, opinions, stats


def coverage_entries(dockets: list[dict], opinions: list[dict], snapshot: str) -> dict[str, dict]:
    """{state: {coverage key: info}}: what this run found per state, in the
    shape coverage.json's `collected` block holds."""
    out: dict[str, dict] = {}
    for key, label, items in (("courts_bulk_dockets", "dockets", dockets),
                              ("courts_bulk_opinions", "opinions", opinions)):
        for state, count in Counter(row["state"] for row in items if row["state"]).items():
            out.setdefault(state, {})[key] = {
                "source": f"CourtListener bulk data — {label[:-1]} captions", "url": SOURCE_PAGE,
                label: count, "snapshot": snapshot}
    return out


def write_coverage(path: Path, entries: dict[str, dict]) -> None:
    """The per-state counts into coverage.json: one read, one write, only
    this collector's two keys touched."""
    data = coverage.load(path)
    for state, keys in entries.items():
        coverage.state_entry(data, state, STATE_NAMES.get(state, state))["collected"].update(keys)
    coverage.save(data, path)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, default=COURTS_DIR)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_DIR,
                        help="where the downloads and the step-2 extracts are kept")
    parser.add_argument("--snapshot", help="bulk-data date, YYYY-MM-DD (default: the latest complete one)")
    parser.add_argument("--connections", type=int, default=8, help="parallel range requests per file")
    parser.add_argument("--refine-only", action="store_true",
                        help="skip the download and scan; re-run step 3 on the cached extracts")


def run(args: argparse.Namespace) -> int:
    started = time.time()
    snapshot = args.snapshot
    if args.refine_only and not snapshot:
        done = sorted(path.name[len("hoa_dockets-"):-len(".jsonl")] for path in args.cache.glob("hoa_dockets-*.jsonl"))
        if not done:
            log.error("no cached extract in %s — run without --refine-only first", args.cache)
            return 1
        snapshot = done[-1]

    def bulk(table: str) -> Path:
        return args.cache / f"{table}-{snapshot}.csv.bz2"

    if not args.refine_only:
        available = snapshots(list_bucket(http.session()))
        if not available:
            log.error("no complete snapshot under %s%s", BUCKET_URL, KEY_PREFIX)
            return 1
        snapshot = snapshot or max(available)
        if snapshot not in available:
            log.error("snapshot %s not in the bucket; have %s", snapshot, ", ".join(sorted(available)))
            return 1
        log.info("snapshot %s", snapshot)
        for table in TABLES:
            key, size = available[snapshot][table]
            download(key, size, bulk(table), args.connections)

    clusters_extract = args.cache / f"hoa_clusters-{snapshot}.jsonl"
    dockets_extract = args.cache / f"hoa_dockets-{snapshot}.jsonl"
    if not args.refine_only:
        # Opinions first: their docket ids tell the docket pass which extra rows to keep for the court.
        if not clusters_extract.exists():
            scan_to(bulk("opinion-clusters"), clusters_extract, CLUSTER_COLS)
        if not dockets_extract.exists():
            ids = frozenset(row["docket_id"] for row in iter_rows(clusters_extract))
            scan_to(bulk("dockets"), dockets_extract, DOCKET_COLS, ids)

    retrieved_at = now_iso()
    courts = court_table(bulk("courts"))
    dockets, opinions, stats = refine(iter_rows(clusters_extract), iter_rows(dockets_extract), courts, snapshot,
                                      retrieved_at)
    write_rows(args.out / "bulk_dockets.jsonl", dockets)
    write_rows(args.out / "bulk_opinions.jsonl", opinions)
    merge_json(args.out / "sources.json", "courtlistener_bulk", {
        "name": "CourtListener bulk data (Free Law Project)",
        "url": SOURCE_PAGE,
        "bucket": BUCKET_URL + KEY_PREFIX,
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
        "retrieved_at": retrieved_at, "duration_seconds": round(time.time() - started, 1),
    })
    if args.out.resolve() == COURTS_DIR.resolve():
        write_coverage(COVERAGE_PATH, coverage_entries(dockets, opinions, snapshot))
    for key, count in sorted(stats.items()):
        log.info("%-48s %s", key, f"{count:,}")
    for label, items in (("bulk dockets", dockets), ("bulk opinions", opinions)):
        by_state = Counter(row["state"] for row in items if row["state"])
        top = ", ".join(f"{state} {count:,}" for state, count in by_state.most_common(8))
        print(f"{len(items):,} {label} — top states: {top}")
    return 0
