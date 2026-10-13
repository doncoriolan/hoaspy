"""Miami-Dade trial-court cases naming community associations, from the
Clerk's Commercial Data Services **Civil** FTP folder.

    python -m hoaspy miamidade-civil
    python -m hoaspy miamidade-civil --no-links -v

    https://www.miamidadeclerk.gov/clerk/commercial-data-services.page

The folder is a court-case feed (not the Official Records lien index —
that is the separate Records folder). Two of its file families carry what
is needed; the rest (the new-case index, evictions, landlord-tenant,
verdicts) are redundant or association-free and are ignored:

    daily_civil_MMDDYYYY.zip   every case with docket activity that day plus
                               its full history: CASES.EXP, PARTIES.EXP (with
                               party addresses), CASETYPE.EXP lookups, dockets
    Indebtedness_YYYYMMDD.zip  a weekly full dump of county contract-and-
                               indebtedness and county foreclosure cases back
                               to 1958 (the only backfile in the feed)

The files are caret-delimited, CRLF, no header except Indebtedness. The
FTP keeps 30 days, so the raw files are staged under
.cache/miamidade_civil/raw/ (git-ignored) and re-read in full on every
run; the dailies accumulate there.

Per case: the caption, case number, court division, dates, case type,
status, the association party names and which side each is on, the
property ZIP (the defendant's ZIP when the association is the plaintiff,
i.e. the unit being foreclosed) and a public per-case link. Individual
defendants' names and street addresses are never carried into the output.

Public links: the Clerk's Online Case Search (OCS) gates its *search*
forms behind reCAPTCHA, but GET /ocs/api/CaseInfo/encrypt/{case number} is
open and returns the encrypted `qs` that /ocs/searchResults?qs= renders
straight to the Case Information page (no cookie, no login). The links
are cached in .cache/miamidade_civil/ocs_links.json.

Output: courts/trial_fl_miamidade.jsonl in the shared docket shape plus
`zip` and `has_public_link`, and a `trial_courts.fl_miamidade` entry in
courts/sources.json.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import io
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

from hoaspy import ROOT
from hoaspy.collect.courts import COURTS_DIR
from hoaspy.collect.courts.court_portals._common import ASSOCIATION_RE, clean, normalize, record
from hoaspy.lib.http import BROWSER_USER_AGENT
from hoaspy.lib.jsonl import merge_json, now_iso, read_json, read_rows, write_json, write_rows

RAW_DIR = ROOT / ".cache" / "miamidade_civil" / "raw"
LINK_CACHE_PATH = ROOT / ".cache" / "miamidade_civil" / "ocs_links.json"
NAME_FILES = ("associations.jsonl", "state_corps.jsonl", "state_registries.jsonl", "irs_exempt_orgs.jsonl")

STATE = "FL"
KEY = "fl_miamidade"
COUNTY = "Miami-Dade"
OCS_URL = "https://www2.miamidadeclerk.gov/ocs"
SEARCH_URL = OCS_URL + "/"
INFO = {
    "name": "Miami-Dade Clerk — Commercial Data Services Civil feed (daily civil + weekly indebtedness)",
    "url": "https://www.miamidadeclerk.gov/clerk/commercial-data-services.page",
    "access": "subscriber FTP feed (Florida Supreme Court electronic-access standards); "
              "per-case public links via the Clerk's Online Case Search",
    "coverage": "Miami-Dade County / 11th Circuit civil, county civil and small claims",
    "caveat": "complete for cases with docket activity since the feed was first pulled "
              "(2026-08-25) plus the county contract/indebtedness and county foreclosure "
              "backfile; circuit cases idle since then are missing until they move",
}

# Association-shaped party names beyond the shared vocabulary: master and
# maintenance associations, neighborhood associations, and the Florida
# "recreation association" form. A generic "…ASSOCIATION" name (a trade
# group, a club, the YMCA) is kept only when it matches a name already held.
EXTRA_FORMS_RE = re.compile(
    r"(MAINTENANCE ASSOCIATION|NEIGHBORHOOD ASSOCIATION|RECREATION ASSOCIATION|"
    r"VILLAGE ASSOCIATION|ESTATES ASSOCIATION|COOPERATIVE APARTMENTS|"
    r"APARTMENTS? ASSOCIATION)", re.I)
GENERIC_RE = re.compile(r"\b(ASSOCIATION|ASSN|ASSOC)\b", re.I)
# Businesses whose names borrow "homeowners" or "association": the
# Homeowners Choice insurer (captioned a dozen truncated ways), public
# adjusters and contractors selling to homeowners, banks and debt buyers.
NOT_COMMUNITY_RE = re.compile(
    r"(NATIONAL ASSOC|\bBANK\b|N\.A\.|SAVINGS|CREDIT UNION|FEDERAL|MORTGAGE|"
    r"RECOVERY ASSOC|FINANCIAL|INSURANCE|\bINS\b|CASUALTY|CHOICE PROPERTY|"
    r"MEDICAL|DENTAL|PHYSICIAN|HEALTH|CHIROPRACTIC|ORTHOP|SURG|RADIOLOG|ANESTH|"
    r"PORTFOLIO|FUNDING|ACCEPTANCE|CLAIM|ADJUST|CONSULTANT|EXPERT|ROOFING|"
    r"CONTRACT(?:OR|ING)|REALTY|TITLE|WARRANTY|LENDING|LOAN|"
    # associations are not-for-profit corporations, never LLCs, developers,
    # managers or funds — those are the other side of the caption
    r"\bL\.?L\.?C\b|\(LLC\)|\bMGMT\b|MANAGEMENT|SERVICES|\bASSET|ASSISTANCE|"
    r"\bGROUP\b|\bFUND\b|DEVELOP|INVESTMENT|INVESTOR|\bVENTURES?\b|\bPARTNERS\b|"
    r"\bL\.?P\.?\b|\bLTD\b)", re.I)
# Party type codes -> which side of the caption.
ROLES = {"PN": "plaintiff", "PE": "plaintiff", "PLPE": "plaintiff", "PKA": "plaintiff",
         "CP": "plaintiff", "PKC": "plaintiff", "TPP": "plaintiff",
         "DN": "defendant", "DK": "defendant", "DERE": "defendant", "RE": "defendant",
         "CD": "defendant", "DFC": "defendant", "TPD": "defendant"}
DEFENDANT_TYPES = ("DN", "DK")
# Party types that are addresses, counsel or witnesses, not parties.
NOT_A_PARTY = {"AT", "LT", "UN", "UK", "GN", "AMBN", "SUBPT", "WITNESS", "TPB", "STR"}
DIVISIONS = {
    "CA": "Miami-Dade County Circuit Court, Civil Division",
    "CC": "Miami-Dade County Court, Civil Division",
    "SP": "Miami-Dade County Court, Small Claims",
    "CP": "Miami-Dade County Circuit Court, Probate Division",
    "FC": "Miami-Dade County Circuit Court, Family Division",
}
DEFAULT_COURT = "Miami-Dade County Court"
# The columns read off the Indebtedness dump's header.
INDEBTEDNESS_COLUMNS = ("FILE_DATE", "CASE_TYPE", "DISPO_DATE", "CASE_STATUS", "DISPO_DESCRIPTION", "PLAINTIFF_NAME",
                        "DEFENDANT_NAME", "DN_ZIP", "DN_CITY", "DN_STATE")
LINK_WORKERS = 4
LINK_SAVE_EVERY = 500

log = logging.getLogger(__name__)


# ---- association detection ---------------------------------------------------------

def known_association_norms(state: str = STATE) -> set[str]:
    """The normalized names of every association held for `state`
    (registry, corporate roster, IRS floor): the whitelist for generic
    "…ASSOCIATION" captions."""
    out: set[str] = set()
    for filename in NAME_FILES:
        for row in read_rows(ROOT / "records" / filename, skip_bad=True):
            if row.get("state") == state and row.get("name"):
                out.add(normalize(row["name"]))
    return out


def is_association(name: str, known: set[str] | None = None) -> bool:
    """A community-association-shaped party name. Tier 1: the shared
    vocabulary plus the master, maintenance and neighborhood forms. Tier
    2: any other "…ASSOCIATION" name that equals one already held. Banks,
    insurers, debt buyers and medical groups are never associations here."""
    text = clean(name)
    if not text or NOT_COMMUNITY_RE.search(text):
        return False
    if ASSOCIATION_RE.search(text) or EXTRA_FORMS_RE.search(text):
        return True
    if known and GENERIC_RE.search(text):
        return normalize(text) in known
    return False


# ---- feed parsing ------------------------------------------------------------------

def split_fields(line: str) -> list[str]:
    return line.rstrip("\r\n").split("^")


def parse_daily_zip(path: Path, casetypes: dict[str, str]) -> dict[str, dict]:
    """One daily_civil zip -> {case number: case} with the parties attached.
    CASES.EXP columns: id, case number, file date, style, case type code,
    judge id, section, UCN, -, disposition date, last update. PARTIES.EXP:
    id, case id, case number, name, party type, bar number, attorney, -, -,
    address1, address2, -, -, city, state, zip."""
    cases: dict[str, dict] = {}
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        if "CASETYPE.EXP" in members:
            for line in io.TextIOWrapper(archive.open("CASETYPE.EXP"), encoding="latin-1"):
                fields = split_fields(line)
                if len(fields) >= 3:
                    casetypes[fields[1]] = clean(fields[2])
        for line in io.TextIOWrapper(archive.open("CASES.EXP"), encoding="latin-1"):
            fields = split_fields(line)
            if len(fields) < 11 or not fields[1]:
                continue
            cases[fields[1]] = {
                "case_number": fields[1], "file_date": fields[2], "style": clean(fields[3]),
                "type_code": fields[4], "ucn": fields[7], "dispo_date": fields[9],
                "updated": fields[10], "parties": [],
            }
        for line in io.TextIOWrapper(archive.open("PARTIES.EXP"), encoding="latin-1"):
            fields = split_fields(line)
            if len(fields) < 16 or fields[2] not in cases:
                continue
            party_type = fields[4]
            if party_type in NOT_A_PARTY:
                continue
            cases[fields[2]]["parties"].append({
                "name": clean(fields[3]), "type": party_type, "zip": clean(fields[15])[:5],
                "city": clean(fields[13]), "state": clean(fields[14]),
            })
    return cases


def parse_indebtedness_zip(path: Path) -> dict[str, dict]:
    """The weekly Indebtedness dump -> {case number: case} in the shape of
    parse_daily_zip (two parties: plaintiff and defendant)."""
    cases: dict[str, dict] = {}
    with zipfile.ZipFile(path) as archive:
        name = next(member for member in archive.namelist() if member.lower().endswith(".txt"))
        fh = io.TextIOWrapper(archive.open(name), encoding="latin-1")
        header = split_fields(next(fh))
        columns = {heading: i for i, heading in enumerate(header)}
        for line in fh:
            fields = split_fields(line)
            if len(fields) < len(header) - 1 or not fields[0] or fields[0] == "CASE_NUMBER":
                continue
            value = {key: fields[columns[key]] if key in columns and columns[key] < len(fields) else ""
                     for key in INDEBTEDNESS_COLUMNS}
            cases[fields[0]] = {
                "case_number": fields[0], "file_date": value["FILE_DATE"], "style": "",
                "type_code": value["CASE_TYPE"], "ucn": "", "dispo_date": value["DISPO_DATE"],
                "status": value["CASE_STATUS"], "dispo_desc": value["DISPO_DESCRIPTION"],
                "updated": "", "parties": [
                    {"name": clean(value["PLAINTIFF_NAME"]), "type": "PN", "zip": "", "city": "", "state": ""},
                    {"name": clean(value["DEFENDANT_NAME"]), "type": "DN", "zip": clean(value["DN_ZIP"])[:5],
                     "city": clean(value["DN_CITY"]), "state": clean(value["DN_STATE"])},
                ],
            }
    return cases


def load_feed(raw_dir: Path) -> tuple[dict[str, dict], dict[str, str], dict]:
    """Every daily zip (the newest wins per case) over the newest
    Indebtedness dump (the backfile): (cases, case-type lookup, stats)."""
    casetypes: dict[str, str] = {}
    cases: dict[str, dict] = {}
    stats: dict = {"daily_files": 0, "indebtedness_file": "", "daily_cases": 0, "indebtedness_cases": 0}
    dumps = sorted(raw_dir.glob("Indebtedness_*.zip"))
    if dumps:
        stats["indebtedness_file"] = dumps[-1].name
        cases.update(parse_indebtedness_zip(dumps[-1]))
        stats["indebtedness_cases"] = len(cases)
        log.info("%s: %d county contract/foreclosure cases (backfile)", dumps[-1].name, len(cases))
    for path in sorted(raw_dir.glob("daily_civil_*.zip"), key=lambda p: p.stem[-4:] + p.stem[-8:-4]):   # YYYY + MMDD
        day = parse_daily_zip(path, casetypes)
        stats["daily_files"] += 1
        stats["daily_cases"] += len(day)
        cases.update(day)                     # a later day = a fuller docket history
        log.debug("%s: %d cases", path.name, len(day))
    log.info("%d daily files, %d distinct cases in the feed", stats["daily_files"], len(cases))
    return cases, casetypes, stats


# ---- public links -------------------------------------------------------------------

def ocs_url(token: str) -> str:
    """The public Case Information page for an encrypted case-number token."""
    return f"{OCS_URL}/searchResults?qs={token}"


def fetch_qs(case_number: str, timeout: int = 30) -> str:
    """The encrypted `qs` for a case number from the open OCS encrypt
    endpoint; '' when the Clerk does not answer or refuses."""
    request = urllib.request.Request(f"{OCS_URL}/api/CaseInfo/encrypt/{urllib.parse.quote(case_number)}",
                                     headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode())
    except Exception as exc:               # network, 5xx, bad JSON
        log.debug("encrypt %s: %s", case_number, exc)
        return ""
    return answer.get("qs", "") if answer.get("success") else ""


def load_link_cache() -> dict[str, str]:
    cache = read_json(LINK_CACHE_PATH, {})
    return cache if isinstance(cache, dict) else {}


def save_link_cache(cache: dict[str, str]) -> None:
    write_json(LINK_CACHE_PATH, dict(sorted(cache.items())))


def resolve_links(case_numbers: list[str], cache: dict[str, str], workers: int = LINK_WORKERS) -> int:
    """Fill `cache` with tokens for every case number not yet cached, a few
    polite threads at a time; returns how many were asked for."""
    todo = [number for number in case_numbers if number not in cache]
    if not todo:
        return 0
    log.info("fetching %d public links (%d cached)", len(todo), len(cache))
    done = 0
    started = time.time()
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, token in zip(todo, pool.map(fetch_qs, todo), strict=True):
            if token:
                cache[number] = token
            done += 1
            if done % LINK_SAVE_EVERY == 0:
                save_link_cache(cache)
                log.info("  %d/%d links, %.0fs", done, len(todo), time.time() - started)
    save_link_cache(cache)
    return done


# ---- records ------------------------------------------------------------------------

def caption(case: dict) -> str:
    if case.get("style"):
        return re.sub(r"\s+", " ", case["style"].replace("\n", " ")).strip()
    plaintiffs = [party["name"] for party in case["parties"] if party["type"] == "PN"]
    defendants = [party["name"] for party in case["parties"] if party["type"] == "DN"]
    return f"{' and '.join(plaintiffs)} vs {' and '.join(defendants)}".strip()


def to_record(case: dict, casetypes: dict[str, str], known: set[str], links: dict[str, str]) -> dict | None:
    """A docket record for a case naming at least one association; None
    otherwise. Carries which side each association is on and the property
    ZIP (the defendant's ZIP when the association is the plaintiff)."""
    associations: list[str] = []
    roles: list[str] = []
    for party in case["parties"]:
        if party["name"] and is_association(party["name"], known) and party["name"] not in associations:
            associations.append(party["name"])
            roles.append(ROLES.get(party["type"], "other"))
    if not associations:
        return None
    case_number = case["case_number"]
    division = case_number.split("-")[2] if case_number.count("-") >= 2 else ""
    status = clean(case.get("status") or "").title() or ("Closed" if case.get("dispo_date") else "Open")
    property_zip = ""
    if "plaintiff" in roles:
        zips = Counter(party["zip"] for party in case["parties"]
                       if party["type"] in DEFENDANT_TYPES and re.fullmatch(r"\d{5}", party["zip"] or "")
                       and (party["state"] or "FL").upper() == "FL")
        property_zip = zips.most_common(1)[0][0] if zips else ""
    token = links.get(case_number, "")
    return record(
        key=KEY, state=STATE, case_name=caption(case), court=DIVISIONS.get(division, DEFAULT_COURT),
        docket_number=case_number, date_filed=case.get("file_date", ""), date_terminated=case.get("dispo_date", ""),
        nature_of_suit=casetypes.get(case.get("type_code", ""), case.get("type_code", "")), status=status,
        associations=associations, url=ocs_url(token) if token else SEARCH_URL,
        case_id=case.get("ucn") or case_number, queried="civil-feed",
        association_role=roles, county=COUNTY, zip=property_zip, has_public_link=bool(token)).as_row()


def build_records(cases: dict[str, dict], casetypes: dict[str, str], known: set[str],
                  links: dict[str, str]) -> list[dict]:
    out = []
    for case_number in sorted(cases):
        row = to_record(cases[case_number], casetypes, known, links)
        if row:
            out.append(row)
    return out


def write_outputs(rows: list[dict], out_dir: Path, stats: dict) -> Path:
    out = out_dir / f"trial_{KEY}.jsonl"
    write_rows(out, rows)
    roles = Counter(role for row in rows for role in row["association_role"])
    entry = dict(INFO)
    entry.update({
        "state": STATE, "records": len(rows),
        "associations": len({name for row in rows for name in row["associations"]}),
        "courts": len({row["court"] for row in rows}),
        "with_public_link": sum(1 for row in rows if row["has_public_link"]),
        "with_property_zip": sum(1 for row in rows if row["zip"]),
        "association_plaintiff": roles["plaintiff"],
        "association_defendant": roles["defendant"],
        "feed": stats,
        "retrieved_at": now_iso(),
    })
    catalog = read_json(out_dir / "sources.json", {})
    catalog.setdefault("trial_courts", {})[KEY] = entry
    merge_json(out_dir / "sources.json", "trial_courts", catalog["trial_courts"])
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--src", type=Path, default=RAW_DIR,
                        help=f"directory of daily_civil_*.zip / Indebtedness_*.zip (default {RAW_DIR})")
    parser.add_argument("--out-dir", type=Path, default=COURTS_DIR)
    parser.add_argument("--no-links", action="store_true", help="use cached public links only; contact nothing")
    parser.add_argument("--workers", type=int, default=LINK_WORKERS)


def run(args: argparse.Namespace) -> int:
    if not args.src.is_dir():
        print(f"{args.src} not found — stage the FTP files there first")
        return 1
    cases, casetypes, stats = load_feed(args.src)
    known = known_association_norms()
    log.info("%d known FL association names for the generic-name whitelist", len(known))

    # A first pass without links says which cases matter; then those get links.
    wanted = [number for number in sorted(cases) if to_record(cases[number], casetypes, known, {})]
    log.info("%d of %d cases name a community association", len(wanted), len(cases))
    links = load_link_cache()
    if not args.no_links:
        resolve_links(wanted, links, workers=args.workers)
    rows = build_records({number: cases[number] for number in wanted}, casetypes, known, links)
    out = write_outputs(rows, args.out_dir, stats)
    roles = Counter(role for row in rows for role in row["association_role"])
    log.info("wrote %s — %d cases, %d associations, %d with public links, %d with property ZIP; "
             "association plaintiff %d / defendant %d",
             out.relative_to(ROOT) if out.is_relative_to(ROOT) else out, len(rows),
             len({name for row in rows for name in row["associations"]}),
             sum(1 for row in rows if row["has_public_link"]), sum(1 for row in rows if row["zip"]),
             roles["plaintiff"], roles["defendant"])
    return 0
