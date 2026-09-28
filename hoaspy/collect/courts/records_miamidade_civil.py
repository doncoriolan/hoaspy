"""Miami-Dade trial-court cases naming community associations, from the
Clerk's Commercial Data Services **Civil** FTP folder.

    https://www.miamidadeclerk.gov/clerk/commercial-data-services.page

The folder is a court-case feed (not the Official Records lien index — that
is the separate `Records` folder, see docs/NEEDS.md). Two of its file families
carry everything we need; the rest (new-case index, evictions, landlord-
tenant, verdicts) are redundant or association-free and are ignored:

    daily_civil_MMDDYYYY.zip   every case with docket activity that day plus
                               its full history: CASES.EXP, PARTIES.EXP (with
                               party addresses), CASETYPE.EXP lookups, dockets
    Indebtedness_YYYYMMDD.zip  weekly full dump of county contract-and-
                               indebtedness + county foreclosure cases back to
                               1958 (the only backfile in the feed)

Files are caret-delimited, CRLF, no header except Indebtedness. The FTP
keeps 30 days, so the raw files are staged under `.cache/miamidade_civil/raw/`
(git-ignored) and re-read in full on every run — dailies accumulate there.

Per case we keep: caption, case number, court division, dates, case type,
status, the association party names and which side each is on, the property
ZIP (defendant address ZIP when the association is the plaintiff — i.e. the
unit being foreclosed) and a public per-case link. Individual defendants'
names and street addresses are deliberately NOT carried into the output.

Public links: the Clerk's Online Case Search (OCS) gates its *search* forms
behind reCAPTCHA, but `GET /ocs/api/CaseInfo/encrypt/{case number}` is open
and returns the encrypted `qs` that `/ocs/searchResults?qs=` renders straight
to the Case Information page (verified 2026-09-24, no cookie, no login).
Links are cached in `.cache/miamidade_civil/ocs_links.json`.

Output: courts/trial_fl_miamidade.jsonl in the court_portals record shape
(plus `association_role`, `county`, `zip`) and a `trial_courts.fl_miamidade`
entry in courts/sources.json — folded into the site by build_site.add_courts
like every other trial_*.jsonl.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hoaspy.collect.courts.court_portals._common import ASSOCIATION_RE, clean, iso_date, normalize, record

log = logging.getLogger("records.miamidade_civil")

from hoaspy import ROOT
RAW_DIR = ROOT / ".cache" / "miamidade_civil" / "raw"
LINK_CACHE = ROOT / ".cache" / "miamidade_civil" / "ocs_links.json"
OUT_DIR = ROOT / "courts"

STATE = "FL"
KEY = "fl_miamidade"
COUNTY = "Miami-Dade"
OCS = "https://www2.miamidadeclerk.gov/ocs"
SEARCH_URL = OCS + "/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/153.0.0.0 Safari/537.36")
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

# Association-shaped party names beyond the shared ASSOCIATION_RE: master and
# maintenance associations, neighborhood associations, and the Florida
# "recreation association" form. Generic "…ASSOCIATION" names (trade groups,
# clubs, the YMCA) are only kept when they match a name we already hold.
_EXTRA_RE = re.compile(
    r"(MAINTENANCE ASSOCIATION|NEIGHBORHOOD ASSOCIATION|RECREATION ASSOCIATION|"
    r"VILLAGE ASSOCIATION|ESTATES ASSOCIATION|COOPERATIVE APARTMENTS|"
    r"APARTMENTS? ASSOCIATION)", re.I)
_GENERIC_RE = re.compile(r"\b(ASSOCIATION|ASSN|ASSOC)\b", re.I)
# Businesses whose names borrow "homeowners"/"association": the Homeowners
# Choice insurer (captioned a dozen truncated ways), public adjusters and
# contractors selling to homeowners, banks and debt buyers.
_NOT_COMMUNITY_RE = re.compile(
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

ROLE = {"PN": "plaintiff", "PE": "plaintiff", "PLPE": "plaintiff", "PKA": "plaintiff",
        "CP": "plaintiff", "PKC": "plaintiff", "TPP": "plaintiff",
        "DN": "defendant", "DK": "defendant", "DERE": "defendant", "RE": "defendant",
        "CD": "defendant", "DFC": "defendant", "TPD": "defendant"}
# Party types that are people/entities on the caption, not addresses or counsel.
_SKIP_PARTY = {"AT", "LT", "UN", "UK", "GN", "AMBN", "SUBPT", "WITNESS", "TPB", "STR"}

DIVISION = {
    "CA": "Miami-Dade County Circuit Court, Civil Division",
    "CC": "Miami-Dade County Court, Civil Division",
    "SP": "Miami-Dade County Court, Small Claims",
    "CP": "Miami-Dade County Circuit Court, Probate Division",
    "FC": "Miami-Dade County Circuit Court, Family Division",
}


# ---- association detection --------------------------------------------------

def known_association_norms(state: str = STATE) -> set[str]:
    """Normalised names of every association we hold for `state` (registry,
    corporate roster, IRS floor) — the whitelist for generic "…ASSOCIATION"
    captions."""
    out: set[str] = set()
    for fn in ("associations.jsonl", "state_corps.jsonl", "state_registries.jsonl",
               "irs_exempt_orgs.jsonl"):
        p = ROOT / "records" / fn
        if not p.exists():
            continue
        with p.open() as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("state") == state and d.get("name"):
                    out.add(normalize(d["name"]))
    return out


def is_association(name: str, known: set[str] | None = None) -> bool:
    """Community-association-shaped party name. Tier 1: the shared pattern
    plus master/maintenance/neighborhood forms. Tier 2: any other
    "…ASSOCIATION" name that equals one we already hold. Banks, insurers,
    debt buyers and medical groups are never associations here."""
    n = clean(name)
    if not n or _NOT_COMMUNITY_RE.search(n):
        return False
    if ASSOCIATION_RE.search(n) or _EXTRA_RE.search(n):
        return True
    if known and _GENERIC_RE.search(n):
        return normalize(n) in known
    return False


# ---- feed parsing ------------------------------------------------------------

def _split(line: str) -> list[str]:
    return line.rstrip("\r\n").split("^")


def parse_daily_zip(path: Path, casetypes: dict[str, str]) -> dict[str, dict]:
    """One daily_civil zip → {case_number: case} with parties attached.
    CASES.EXP columns: id, case number, file date, style, case type code,
    judge id, section, UCN, -, disposition date, last update.
    PARTIES.EXP: id, case id, case number, name, party type, bar number,
    attorney, -, -, address1, address2, -, -, city, state, zip."""
    cases: dict[str, dict] = {}
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "CASETYPE.EXP" in names:
            for line in io.TextIOWrapper(zf.open("CASETYPE.EXP"), encoding="latin-1"):
                p = _split(line)
                if len(p) >= 3:
                    casetypes[p[1]] = clean(p[2])
        for line in io.TextIOWrapper(zf.open("CASES.EXP"), encoding="latin-1"):
            p = _split(line)
            if len(p) < 11 or not p[1]:
                continue
            cases[p[1]] = {
                "case_number": p[1], "file_date": p[2], "style": clean(p[3]),
                "type_code": p[4], "ucn": p[7], "dispo_date": p[9],
                "updated": p[10], "parties": [],
            }
        for line in io.TextIOWrapper(zf.open("PARTIES.EXP"), encoding="latin-1"):
            p = _split(line)
            if len(p) < 16 or p[2] not in cases:
                continue
            ptype = p[4]
            if ptype in _SKIP_PARTY:
                continue
            cases[p[2]]["parties"].append({
                "name": clean(p[3]), "type": ptype, "zip": clean(p[15])[:5],
                "city": clean(p[13]), "state": clean(p[14]),
            })
    return cases


def parse_indebtedness_zip(path: Path) -> dict[str, dict]:
    """Weekly Indebtedness dump → {case_number: case} in the same shape as
    parse_daily_zip (two parties: plaintiff and defendant)."""
    cases: dict[str, dict] = {}
    with zipfile.ZipFile(path) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        fh = io.TextIOWrapper(zf.open(name), encoding="latin-1")
        header = _split(next(fh))
        col = {h: i for i, h in enumerate(header)}
        for line in fh:
            p = _split(line)
            if len(p) < len(header) - 1 or not p[0] or p[0] == "CASE_NUMBER":
                continue
            g = lambda k: p[col[k]] if k in col and col[k] < len(p) else ""
            cases[p[0]] = {
                "case_number": p[0], "file_date": g("FILE_DATE"), "style": "",
                "type_code": g("CASE_TYPE"), "ucn": "", "dispo_date": g("DISPO_DATE"),
                "status": g("CASE_STATUS"), "dispo_desc": g("DISPO_DESCRIPTION"),
                "updated": "", "parties": [
                    {"name": clean(g("PLAINTIFF_NAME")), "type": "PN", "zip": "",
                     "city": "", "state": ""},
                    {"name": clean(g("DEFENDANT_NAME")), "type": "DN",
                     "zip": clean(g("DN_ZIP"))[:5], "city": clean(g("DN_CITY")),
                     "state": clean(g("DN_STATE"))},
                ],
            }
    return cases


def load_feed(raw_dir: Path) -> tuple[dict[str, dict], dict[str, str], dict]:
    """Every daily zip (newest wins per case) over the newest Indebtedness
    dump (backfile). Returns (cases, casetype lookup, stats)."""
    casetypes: dict[str, str] = {}
    cases: dict[str, dict] = {}
    stats: dict = {"daily_files": 0, "indebtedness_file": "", "daily_cases": 0,
                   "indebtedness_cases": 0}
    ind = sorted(raw_dir.glob("Indebtedness_*.zip"))
    if ind:
        stats["indebtedness_file"] = ind[-1].name
        cases.update(parse_indebtedness_zip(ind[-1]))
        stats["indebtedness_cases"] = len(cases)
        log.info("%s: %d county contract/foreclosure cases (backfile)",
                 ind[-1].name, len(cases))
    for z in sorted(raw_dir.glob("daily_civil_*.zip"),
                    key=lambda p: p.stem[-4:] + p.stem[-8:-4]):   # YYYY + MMDD
        day = parse_daily_zip(z, casetypes)
        stats["daily_files"] += 1
        stats["daily_cases"] += len(day)
        cases.update(day)                     # later day = fuller docket history
        log.debug("%s: %d cases", z.name, len(day))
    log.info("%d daily files, %d distinct cases in the feed", stats["daily_files"], len(cases))
    return cases, casetypes, stats


# ---- public links --------------------------------------------------------------

def ocs_url(qs: str) -> str:
    """Public Case Information page for an encrypted case-number token."""
    return f"{OCS}/searchResults?qs={qs}"


def fetch_qs(case_number: str, timeout: int = 30) -> str:
    """Encrypted `qs` for a case number from the open OCS encrypt endpoint;
    '' when the Clerk does not answer or refuses."""
    req = urllib.request.Request(
        f"{OCS}/api/CaseInfo/encrypt/{urllib.parse.quote(case_number)}",
        headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode())
    except Exception as exc:               # network / 5xx / bad JSON
        log.debug("encrypt %s: %s", case_number, exc)
        return ""
    return j.get("qs", "") if j.get("success") else ""


def load_link_cache() -> dict[str, str]:
    if LINK_CACHE.exists():
        try:
            return json.loads(LINK_CACHE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_link_cache(cache: dict[str, str]) -> None:
    LINK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LINK_CACHE.write_text(json.dumps(cache, sort_keys=True))


def resolve_links(case_numbers: list[str], cache: dict[str, str],
                  workers: int = 4) -> int:
    """Fill `cache` with qs tokens for every case number not yet cached.
    A few polite threads; the endpoint answers in ~0.1-0.4 s."""
    todo = [c for c in case_numbers if c not in cache]
    if not todo:
        return 0
    log.info("fetching %d public links (%d cached)", len(todo), len(cache))
    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for cn, qs in zip(todo, ex.map(fetch_qs, todo)):
            if qs:
                cache[cn] = qs
            done += 1
            if done % 500 == 0:
                save_link_cache(cache)
                log.info("  %d/%d links, %.0fs", done, len(todo), time.time() - t0)
    save_link_cache(cache)
    return done


# ---- records -------------------------------------------------------------------

def caption(case: dict) -> str:
    if case.get("style"):
        return re.sub(r"\s+", " ", case["style"].replace("\n", " ")).strip()
    pn = [p["name"] for p in case["parties"] if p["type"] == "PN"]
    dn = [p["name"] for p in case["parties"] if p["type"] == "DN"]
    return f"{' and '.join(pn)} vs {' and '.join(dn)}".strip()


def to_record(case: dict, casetypes: dict[str, str], known: set[str],
              links: dict[str, str]) -> dict | None:
    """Docket record for a case naming at least one association; None
    otherwise. Carries which side each association is on and the property
    ZIP (defendant's ZIP when the association is plaintiff)."""
    assocs: list[str] = []
    roles: list[str] = []
    for p in case["parties"]:
        if p["name"] and is_association(p["name"], known):
            if p["name"] not in assocs:
                assocs.append(p["name"])
                roles.append(ROLE.get(p["type"], "other"))
    if not assocs:
        return None
    cn = case["case_number"]
    div = cn.split("-")[2] if cn.count("-") >= 2 else ""
    status = clean(case.get("status") or "").title()
    if not status:
        status = "Closed" if case.get("dispo_date") else "Open"
    zc = ""
    if "plaintiff" in roles:
        zips = Counter(p["zip"] for p in case["parties"]
                       if p["type"] in ("DN", "DK") and re.fullmatch(r"\d{5}", p["zip"] or "")
                       and (p["state"] or "FL").upper() == "FL")
        zc = zips.most_common(1)[0][0] if zips else ""
    qs = links.get(cn, "")
    rec = record(key=KEY, state=STATE, case_name=caption(case),
                 court=DIVISION.get(div, "Miami-Dade County Court"),
                 docket_number=cn, date_filed=case.get("file_date", ""),
                 date_terminated=case.get("dispo_date", ""),
                 nature_of_suit=casetypes.get(case.get("type_code", ""), case.get("type_code", "")),
                 status=status, associations=assocs,
                 url=ocs_url(qs) if qs else SEARCH_URL,
                 case_id=case.get("ucn") or cn, queried="civil-feed")
    rec["association_role"] = roles
    rec["county"] = COUNTY
    rec["zip"] = zc
    rec["has_public_link"] = bool(qs)
    return rec


def build_records(cases: dict[str, dict], casetypes: dict[str, str],
                  known: set[str], links: dict[str, str]) -> list[dict]:
    out = []
    for cn in sorted(cases):
        r = to_record(cases[cn], casetypes, known, links)
        if r:
            out.append(r)
    return out


def write_outputs(records: list[dict], out_dir: Path, stats: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"trial_{KEY}.jsonl"
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    src = out_dir / "sources.json"
    catalog = {}
    if src.exists():
        try:
            catalog = json.loads(src.read_text())
        except json.JSONDecodeError:
            catalog = {}
    entry = dict(INFO)
    roles = Counter(role for r in records for role in r["association_role"])
    entry.update({
        "state": STATE, "records": len(records),
        "associations": len({a for r in records for a in r["associations"]}),
        "courts": len({r["court"] for r in records}),
        "with_public_link": sum(1 for r in records if r["has_public_link"]),
        "with_property_zip": sum(1 for r in records if r["zip"]),
        "association_plaintiff": roles["plaintiff"],
        "association_defendant": roles["defendant"],
        "feed": stats,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    })
    catalog.setdefault("trial_courts", {})[KEY] = entry
    src.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=RAW_DIR,
                    help=f"directory of daily_civil_*.zip / Indebtedness_*.zip (default {RAW_DIR})")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--no-links", action="store_true",
                    help="use cached public links only; contact nothing")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-upload", action="store_true", help="accepted for parity; never uploads")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    if not args.src.is_dir():
        print(f"{args.src} not found — stage the FTP files there first")
        return 1
    cases, casetypes, stats = load_feed(args.src)
    known = known_association_norms()
    log.info("%d known FL association names for the generic-name whitelist", len(known))

    # First pass without links to learn which cases matter, then link those.
    wanted = [cn for cn in sorted(cases) if to_record(cases[cn], casetypes, known, {})]
    log.info("%d of %d cases name a community association", len(wanted), len(cases))
    links = load_link_cache()
    if not args.no_links:
        resolve_links(wanted, links, workers=args.workers)
    records = build_records({cn: cases[cn] for cn in wanted}, casetypes, known, links)
    out = write_outputs(records, args.out_dir, stats)
    roles = Counter(role for r in records for role in r["association_role"])
    log.info("wrote %s — %d cases, %d associations, %d with public links, "
             "%d with property ZIP; association plaintiff %d / defendant %d",
             out.relative_to(ROOT) if out.is_relative_to(ROOT) else out, len(records),
             len({a for r in records for a in r["associations"]}),
             sum(1 for r in records if r["has_public_link"]),
             sum(1 for r in records if r["zip"]), roles["plaintiff"], roles["defendant"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
