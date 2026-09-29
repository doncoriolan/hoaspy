#!/usr/bin/env python
"""Arizona associations from ADRE subdivision Public Reports.

    ./venv/bin/python -m hoaspy.collect.registries.get_az_adre                 # full run (resumable)
    ./venv/bin/python -m hoaspy.collect.registries.get_az_adre --since 2015    # newer reports only
    ./venv/bin/python -m hoaspy.collect.registries.get_az_adre --limit 20      # smoke test
    ./venv/bin/python -m hoaspy.collect.registries.get_az_adre --finalize-only # rebuild rows from the checkpoint

Arizona has no HOA registry (docs/NEEDS.md 3e). What the Department of Real
Estate does publish is every subdivision *Public Report* (A.R.S. 32-2183):
the disclosure a developer must hand each buyer, and its "PROPERTY OWNERS'
ASSOCIATIONS" section names the association the purchaser will belong to,
with its regular assessment. That section is the only free, government
source naming Arizona HOAs, so this collector reads it:

    List/DownloadList/4                   bulk CSV of registrations (44.9k rows)
    Development/ViewDevelopment/<id>      detail card: registration number, legal /
                                          marketing name, dates, type, status, county,
                                          developer; and the Public Report form
    Development/DownloadPublicReport      POST {token, id} -> the report PDF

`<id>` is the number after the dash in a `DMyy-0<id>` registration number
(the format used since 2002). Older numeric registration numbers have no id
mapping and their reports predate the structured association section, so
they are not fetched. Reports issued before `--since` (default 2005) are
skipped for the same reason.

Text is extracted with `pdftotext` (poppler). The association name, its
assessment, the subdivision's town, lot count and the town's ZIP (the most
common "<Town>, Arizona 85xxx" in the report, i.e. the local-services
addresses — a town-level placement, not the subdivision's own ZIP) are
parsed out; nothing else from the PDF is kept beyond a gzipped text cache.
Broker and developer contact details are never stored.

Output: records/state_registries.jsonl (AZ rows replaced), one row per
association (a phased subdivision names the same HOA several times; the
row lists every registration), plus coverage.json collected["hoa_registry"]
for AZ with the caveat that this is a developer-disclosure roster.
Checkpoint: .cache/az_adre/details.jsonl (one line per id, appended);
text cache .cache/az_adre/text/<id>.txt.gz.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import html
import io
import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.registries.get_states import REGS, update_coverage, write_merged

CACHE = ROOT / ".cache" / "az_adre"
CKPT = CACHE / "details.jsonl"
TEXT_DIR = CACHE / "text"
BASE = "https://services.azre.gov"
CSV_URL = BASE + "/PdbWeb/List/DownloadList/4"
DETAIL_URL = BASE + "/PdbWeb/Development/ViewDevelopment/{id}"
REPORT_URL = BASE + "/PdbWeb/Development/DownloadPublicReport"
PAGE_URL = BASE + "/PdbWeb/Development/SearchDevelopments"
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
SOURCE = "Arizona Department of Real Estate — subdivision Public Reports"

log = logging.getLogger("az")

DL_RE = re.compile(r"<dt[^>]*>\s*(.*?)\s*</dt>\s*<dd[^>]*>(.*?)</dd>", re.S)
GENERIC = {"association", "homeowners association", "homeowners' association",
           "homeowner's association", "master association", "community association",
           "property owners association", "property owners' association",
           "condominium association", "owners association", "the association",
           "homeowners associations", "property owners associations",
           "an association", "a homeowners association"}


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _iso(us: str) -> str:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", us or "")
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


# ------------------------------------------------------------- HTTP client

class Client:
    """One requests session per thread: the Public Report form's anti-forgery
    token is bound to the session cookie that served the detail page."""

    def __init__(self, pace: float = 0.3):
        self.pace = pace
        self._local = threading.local()

    @property
    def s(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT})
            self._local.s = s
            self._local.last = 0.0
        return self._local.s

    def _wait(self):
        self.s                                   # ensure the thread's session exists
        w = self._local.last + self.pace - time.monotonic()
        if w > 0:
            time.sleep(w)
        self._local.last = time.monotonic()

    def get(self, url: str, **kw) -> requests.Response:
        for attempt in range(4):
            try:
                self._wait()
                r = self.s.get(url, timeout=120, **kw)
                r.raise_for_status()
                return r
            except requests.RequestException as exc:
                log.warning("GET %s: %s (attempt %d)", url, exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"gave up on {url}")

    def detail_page(self, dev_id: int) -> str | None:
        body = self.get(DETAIL_URL.format(id=dev_id)).text
        return None if "Record Unavailable" in body or "Development Details" not in body else body

    def report_pdf(self, page: str) -> bytes | None:
        form = re.search(r'<form action="/PdbWeb/Development/DownloadPublicReport".*?</form>', page, re.S)
        if not form:
            return None
        fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', form.group(0)))
        b = "----hoaspy" + uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        body += f"--{b}--\r\n".encode()
        for attempt in range(3):
            try:
                self._wait()
                r = self.s.post(REPORT_URL, data=body, timeout=180,
                                headers={"Content-Type": f"multipart/form-data; boundary={b}"})
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    return r.content
                log.warning("report POST: %s %s", r.status_code, r.headers.get("Content-Type"))
                return None
            except requests.RequestException as exc:
                log.warning("report POST: %s (attempt %d)", exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        return None


# ---------------------------------------------------------------- parsing

def parse_detail(page: str) -> dict:
    """Development + developer fields off the ViewDevelopment card."""
    d: dict = {}
    dev_part = page.split("Developer Details", 1)
    keys = {"Registration Number": "registration_no", "Legal Name": "legal_name",
            "Marketing Name": "marketing_name", "Date Filed": "date_filed",
            "Date Issued": "date_issued", "Application Type": "application_type",
            "Application Status": "application_status", "Lot Status": "lot_status",
            "County": "county"}
    for k, v in DL_RE.findall(dev_part[0]):
        k = clean(k)
        if k in keys:
            d[keys[k]] = clean(v)
    if len(dev_part) == 2:
        for k, v in DL_RE.findall(dev_part[1]):
            if clean(k) == "Legal Name":
                d["developer"] = clean(v)
                break
    for k in ("marketing_name", "developer"):
        if d.get(k, "").upper() in ("NONE", "N/A", ""):
            d[k] = ""
    d["county"] = county_name(d.get("county", ""))
    return d


def pdf_text(pdf: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as fh:
        fh.write(pdf)
        fh.flush()
        r = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, timeout=300)
    return r.stdout.decode("utf-8", "replace")


ASSOC_WORD = r"(?:Association|ASSOCIATION|Assn\.?)"
BELONG_RE = re.compile(
    r"(?:Purchasers?|Buyers?|Owners?|Members?|You)\s+(?:will|shall|must|are required to|will be required to)\s+"
    r"(?:automatically\s+)?(?:be(?:come)?\s+(?:a\s+)?members?\s+(?:of|in)|belong\s+to|join)\s+(?:the\s+)?"
    r"([A-Z][A-Za-z0-9'’&\- ]{3,120}?" + ASSOC_WORD + r"(?:,?\s*Inc\.?)?)", re.S)
PROPER_ASSOC_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z0-9'’&.\-]*\s+){1,8}(?:Community|Homeowners'?|Homeowner'?s'?|Property Owners'?|"
    r"Condominium|Master|Owners'?|Residential|Townhomes?|Villas?|Estates|Neighborhood)?\s?Association)\b")
CAPS_ASSOC_RE = re.compile(
    r"\b((?:[A-Z][A-Z0-9'’&.\-]*\s+){1,8}(?:COMMUNITY|HOMEOWNERS'?|HOMEOWNER'?S'?|PROPERTY OWNERS'?|"
    r"CONDOMINIUM|MASTER|OWNERS'?|RESIDENTIAL|TOWNHOMES?|VILLAS?|ESTATES|NEIGHBORHOOD)?\s?ASSOCIATION)\b")
ASSESS_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d{2})?)\s*(?:per|/|a|each|every)\s*(month|mo\.?|year|yr\.?|annum|quarter|"
    r"semi-?annual(?:ly)?|annual(?:ly)?)", re.I)
UNITS_RE = re.compile(
    r"(?:consists?\s+of|contains?|comprised\s+of|composed\s+of|(?:a\s+)?total\s+of)\s+(\d[\d,]*)\s+"
    r"(?:residential\s+|single[- ]family\s+|attached\s+|detached\s+)?(?:lots?|units?|homes?|"
    r"condominium\s+units?|townho\w+|parcels?)\b", re.I)
LOCATION_RE = re.compile(r"SUBDIVISION\s+LOCATION[:\s]*(.{0,600}?)(?:\n\s*\n|\n[A-Z][A-Z /&]{5,}:)", re.S)
TOWN_RE = re.compile(r"\b(?:Town|City)\s+of\s+((?:(?:St|Ft|Mt)\.\s)?[A-Z][a-z'’-]+(?:\s(?:[A-Z][a-z'’-]+|de|del|la)){0,2}?)"
                     r"(?=,|\.|;|:|\s+(?:in|within|and|Arizona|AZ|will|shall|is|has|for|or|at|on|to|which|where|"
                     r"Water|Fire|Police|Planning|Public|Building|Engineering|Utilities|Parks|Sanitation|Solid|Sewer|"
                     r"Streets|Transportation|Environmental|Development|Community|Department|Services|Division|Located|"
                     r"Phone|Provider|Municipal|Code|Zoning|Ordinance|General|Plan|Aviation|Easement)\b|"
                     r"\s+(?!(?:de|del|la)\b)[a-z]|\n|$)")
NOT_A_TOWN = {"Arizona", "Phoenix Fire", "Maricopa County", "Pima County", "New York", "Los Angeles"}
# "…, Tucson, Pima County, Arizona": the legal-location phrasing when no "Town/City of" is named.
LOC_TOWN_RE = re.compile(r"(?<![A-Za-z])([A-Z][a-z'’-]+(?:\s(?:[A-Z][a-z'’-]+|de|del|la)){0,2}),"
                         r"\s+((?:[A-Z][a-z]+\s+){1,2})County,\s+(?:State\s+of\s+)?(?:Arizona|AZ)\b")
LOC_PREFIX_RE = re.compile(r"^(?:Unincorporated|Incorporated|City|Town|Village|In|At|Near|The|Downtown)(?:\s+of)?(?:\s+|$)")
STREET_HEAD_RE = re.compile(r"^(?:Avenida|Camino|Calle|Via|Paseo|Placita|Vereda|Circulo|Corte)\b")
# "Hayward Avenue Phoenix" (the comma between street and town is missing): the town is what follows the street type
STREET_MID_RE = re.compile(r"^.*\b(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Parkway|Pkwy|Highway|Hwy|"
                           r"Trail|Loop|Circle|Cir|Place|Pl|Court|Ct)\.?\s+(?=[A-Z])")
GENERIC_WORDS = {"City", "Town", "County", "Village", "Streets", "Street", "Community", "Arizona"}
AZ_COUNTIES = ("Apache", "Cochise", "Coconino", "Gila", "Graham", "Greenlee", "La Paz", "Maricopa", "Mohave", "Navajo",
               "Pima", "Pinal", "Santa Cruz", "Yavapai", "Yuma")
STREET_TAIL_RE = re.compile(r"\b(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Parkway|Pkwy|Highway|Hwy|"
                            r"Trail|Loop|Circle|Cir|Place|Pl|Court|Ct|Route|Rte|Freeway|Fwy|Interstate|Corner|Intersection|"
                            r"Section|Township|Range|Miles?|North|South|East|West|Meridian|Baseline|Roads|Streets|Avenues|"
                            r"Drives|Lanes)\.?$")
LOC_ZIP_RE = re.compile(r"(?:Arizona|AZ),?\s+(8[56]\d{3})\b")
# words the "Town/City of X" capture runs into that are not part of the name
TOWN_TAIL_RE = re.compile(r"\s+(?:[A-Z][a-z]+\s+County|Boundary|Limits|Landfill|Gas|Utility|Standard|Detail|Corporate|"
                          r"Hall|Council|Manager|Office|Airport|Library|Booster|Station|Plant|Facility|Reservoir|Wells?)$")
JUNK_WORDS = re.compile(
    r"\b(?:PARTICIPATE|CONTROL|RESPONSIBILIT\w*|OBLIGATIONS?|DEMANDS?|OWNED|DEEDED?|CONSISTENT|REQUIREMENTS?|"
    r"PURCHASERS?|ADJACENT|WASHES|DECLARATION|PLAT|WITHIN|NOTE|PAYMENTS?|RECORDED|RESTRICTIONS|CURRENT|"
    r"ASSESSMENTS?|MANAGEMENT|PHONE|DUES|FEES?|MAINTAIN\w*|SAID|SUCH|THIS|YOUR|ITS|SHALL|WILL|MUST|ARE|IS|BE|"
    r"CC&RS?|BYLAWS|MEMBERSHIP|MEMBERS?|SUBDIVIDER|DEVELOPER|LOTS?|TRACTS?|CONVEYED|DEEDS?|THAT|ARTICLES|"
    r"INCORPORATION|COMMON|AREAS?|TITLE|EASEMENTS?|DEDICATES?|GRANTS?|HEREBY)\b", re.I)
TYPE_HINT = re.compile(r"homeowner|home owner|community|property owner|condominium|condo|master|owners|residential|"
                       r"neighborhood|villa|estates|townho|\bHOA\b|\bPOA\b", re.I)
CUT_AT = re.compile(r"\s+(?:NOTE:|PAYMENTS TO|Management Company|Current (?:Dues |Regular )?Assessments?|PHONE|"
                    r"Purchasers?\b|Type:|which\b|who\b|that\b|is\b|are\b|will\b|shall\b|has\b|may\b)")
CONNECTIVES = {"at", "of", "the", "and", "de", "del", "la", "on", "in", "by", "for", "y", "e", "&"}
FORM_NAME_RE = re.compile(r"Name of the HOA:\s*(.*)$", re.M)
FORM_ASSESS_RE = re.compile(r"Current (?:Dues |Regular |Annual |Monthly )?Assessments?:")
DIVIDED_RE = re.compile(r"divided\s+into\s+(\d[\d,]*)\s+(?:residential\s+|single[- ]family\s+)?(?:lots?|units?|homes?|condominium\s+units?)\b", re.I)
UNINC_RE = re.compile(r"unincorporated\s+(?:area\s+of\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+County", re.I)
LEADING_JUNK = ("the ", "The ", "THE ")


def _norm_assoc(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ,.;:")
    for j in LEADING_JUNK:
        if name.startswith(j):
            name = name[len(j):]
    return name


TYPE_WORDS = {"the", "a", "an", "association", "associations", "assn", "inc", "homeowners", "homeowner",
              "home", "owners", "owner", "property", "community", "master", "residential", "neighborhood",
              "condominium", "condo", "townhome", "townhomes", "villas", "villa", "sub", "hoa", "poa", "of",
              "said", "this", "such", "your", "its"}


def clean_names(raw: str) -> list[str]:
    """Split 'X and Y' / 'X or Y', cut trailing boilerplate, and reject
    sentence fragments: anything with boilerplate words, prices, colons,
    lower-case non-connective words, or more than ten words."""
    raw = re.sub(r"\s+", " ", raw or "").strip(" .,;:")
    out = []
    for part in re.split(r",?\s+(?:and|or|AND|OR)\s+(?=[A-Z])", raw):
        part = CUT_AT.split(part, 1)[0].strip(" .,;:")
        part = _norm_assoc(part)
        words = part.split()
        if not (2 <= len(words) <= 10) or len(part) > 80:
            continue
        if "$" in part or ":" in part or JUNK_WORDS.search(part) or re.search(r"\.\s+[A-Z]", part):
            continue
        if any(w[:1].islower() and w.lower() not in CONNECTIVES for w in words):
            continue
        if words[0].lower() in CONNECTIVES | {"to", "or", "by", "with"} or _generic(part):
            continue
        # 'the' mid-name only after a preposition ("Ascent at the Phoenician")
        lw = [w.lower() for w in words]
        if any(w == "the" and (k == 0 or lw[k - 1] not in ("at", "of", "on", "in")) for k, w in enumerate(lw)):
            continue
        out.append(part)
    return out


def _generic(name: str) -> bool:
    """True for 'the Association', 'Homeowners Association', 'Residential
    Association' … — a name with no word that is not a type word."""
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return len(name) < 8 or not [w for w in words if w not in TYPE_WORDS]


def form_block(text: str) -> tuple[str, str]:
    """(HOA name, assessment text) from the two-column "Name of the HOA: …
    Current Assessments: …" form used by newer reports; ('', '') if absent.
    Each column wraps onto the following lines until a blank line, so the
    columns are split at the "Current Assessments:" x-position."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = FORM_NAME_RE.search(line)
        if not m:
            continue
        am = FORM_ASSESS_RE.search(line)
        split = am.start() if am else len(line)
        left = [line[m.start(1):split]]
        right = [line[am.end():] if am else ""]
        started = bool(left[0].strip())
        for nxt in lines[i + 1:i + 8]:
            if started and not am:
                break                   # single-column form line: no wrapped name
            if not nxt.strip():
                if started:
                    break
                continue                # name sits below an empty form line
            if re.match(r"\s*(?:Payment frequency|Membership|Who will)", nxt):
                break
            started = True
            gap = re.search(r"\s{3,}", nxt[:split].rstrip() + "   ")     # column boundary
            left.append(nxt[:min(split, gap.start() if gap else split)])
            right.append(nxt[split:])
        name = re.sub(r"\s+", " ", " ".join(left)).strip(" .,;")
        name = re.sub(r"^(?:Type:\s*)", "", name)
        name = re.split(r",?\s+an?\s+Arizona|\s*\(|,\s+an?\s+non-?profit|\s+amount of\b|\s+Type:", name)[0]
        name = name.strip(" .,;")
        if re.fullmatch(r"(?i)(?:none|n/?a|not applicable|-|tbd)?", name):
            name = ""
        return name, re.sub(r"\s+", " ", " ".join(right))
    return "", ""


def association_names(text: str) -> list[str]:
    """Association names the report says the purchaser will belong to, in
    order of confidence: explicit "will belong to X" sentences first, then
    proper-cased names repeated in the text, then all-caps ones."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str):
        for n in clean_names(raw):
            key = re.sub(r"[^a-z0-9]", "", n.lower())
            if key not in seen:
                seen.add(key)
                found.append(n)

    form_name, _ = form_block(text)
    if form_name:
        add(form_name)
        if found:                       # the form is authoritative when present
            return found
    flat = re.sub(r"[ \t]+", " ", text)
    for m in BELONG_RE.finditer(flat):
        add(m.group(1))
    if found:
        return found
    counts: collections.Counter = collections.Counter()
    for rx in (PROPER_ASSOC_RE, CAPS_ASSOC_RE):
        for m in rx.finditer(flat):
            for n in clean_names(m.group(1)):
                if TYPE_HINT.search(n):          # a bare "… Association" is not an HOA name
                    counts[n] += 1
    for n, c in counts.most_common():
        if c >= 2:
            add(n)
        if len(found) >= 2:
            break
    return found


def parse_report(text: str, county: str = "") -> dict:
    """Association, assessment, town, ZIP and lot count from the report text."""
    flat = re.sub(r"[ \t]+", " ", text)
    names = association_names(text)
    out: dict = {"associations": names, "assessment": "", "city": "", "zip": "", "units": None}
    _, form_assess = form_block(text)
    if names:
        i = flat.find(names[0])
        window = flat[i:i + 2500] if i >= 0 else ""
        m = (ASSESS_RE.search(form_assess) or ASSESS_RE.search(window)
             or ASSESS_RE.search(flat[flat.find("ASSOCIATION"):][:6000]))
        if m:
            per = m.group(2).lower().rstrip(".")
            per = {"mo": "month", "yr": "year", "annum": "year", "annually": "year",
                   "annual": "year"}.get(per, per)
            out["assessment"] = f"${m.group(1)}/{per}"
    m = DIVIDED_RE.search(flat) or UNITS_RE.search(flat)
    if m:
        try:
            out["units"] = int(m.group(1).replace(",", "")) or None
        except ValueError:
            pass
    loc = LOCATION_RE.search(flat)
    town, loc_zip = "", ""
    cands = collections.Counter(re.sub(r"\s+", " ", t.group(1)) for t in TOWN_RE.finditer(flat))
    if loc:
        section = loc.group(1)
        tm = TOWN_RE.search(section)
        if tm and re.sub(r"\s+", " ", tm.group(1)) not in NOT_A_TOWN:
            town = unwrap_town(tm.group(1), section[tm.end(1):], flat)
        else:
            lm = LOC_TOWN_RE.search(section)
            um = UNINC_RE.search(section)
            named = LOC_PREFIX_RE.sub("", re.sub(r"\s+", " ", lm.group(1))) if lm else ""
            rest = STREET_MID_RE.sub("", named)
            if rest != named and rest not in GENERIC_WORDS:
                named = rest                               # "Hayward Avenue Phoenix" -> Phoenix (Circle City stays)
            # "…, St. Johns, Apache County" / ", Ft. Mohave": the abbreviation is part of the name when it follows a
            # comma or a preposition; after "1st" or "Main" it is a street
            ab = re.search(r"(?:^|[,;:]|\b(?:of|in|at|near|to|the))\s*(St|Ft|Mt)\.\s*$", section[:lm.start(1)]) if lm else None
            if ab and named:
                full = f"{ab.group(1)}. {named}"
                if town_zips(full, flat) or not town_zips(named, flat):
                    named = full
            if named and named not in NOT_A_TOWN and named not in GENERIC_WORDS and not named.endswith("County") \
                    and not STREET_TAIL_RE.search(named) and not STREET_HEAD_RE.search(named):
                town = named                              # "…, Tucson, Pima County, Arizona"
            elif um:
                town = f"unincorporated {um.group(1)} County"
            elif lm and (not named or named.endswith("County")):
                town = f"unincorporated {lm.group(2).strip()} County"   # "…, Unincorporated, Apache County, Arizona"
        zm = LOC_ZIP_RE.search(section)
        if zm:
            loc_zip = zm.group(1)
    if not town or not _plausible(town):
        town = next((c for c, _n in cands.most_common() if _plausible(c)), "")
    town, zc = repair_town(town, cands, flat)
    if not _plausible(town):                    # "Ma" from a letter-spaced "Town of Ma r a n a": try the rest
        town = next((c for c, _n in cands.most_common() if _plausible(c) and c != town), "")
        town, zc = repair_town(town, cands, flat)
        if not _plausible(town):
            town, zc = "", ""
    out["city"] = town
    out["zip"] = loc_zip or zc
    return out


def _plausible(town: str) -> bool:
    """Three letters or more (Ajo and Why are real; "Ma" and "St" are not)
    and not a known non-town."""
    return len(re.sub(r"[^A-Za-z]", "", town)) >= 3 and town not in NOT_A_TOWN and town not in GENERIC_WORDS


def unwrap_town(town: str, after: str, flat: str) -> str:
    """"City of Casa" at a line end whose next line starts "Grande, Pinal
    County" is one name split by the wrap: join them when the joined name is
    used as a place elsewhere in the report ("Casa Grande," or "Casa Grande,
    Arizona") — i.e. it occurs at least twice."""
    nm = re.match(r"\n([A-Z][a-z'’-]+(?:\s[A-Z][a-z'’-]+)?)(?=,|\.|\s)", after)
    if not nm:
        return town
    joined = re.sub(r"\s+", " ", f"{town} {nm.group(1)}")
    pat = r"\s+".join(re.escape(w) for w in joined.split()) + r"(?:,|\s+(?:Arizona|AZ)\b)"
    return joined if len(re.findall(pat, flat)) >= 2 else town


def town_zips(town: str, flat: str) -> collections.Counter:
    """ZIPs from '<town>, Arizona 85xxx' addresses in the text (the words may
    wrap across a line, so they are joined with \\s+ rather than a space)."""
    pat = r"\s+".join(re.escape(w) for w in town.split()) + r",?\s+(?:Arizona|AZ),?\s+(8[56]\d{3})\b"
    return collections.Counter(re.findall(pat, flat, re.I))


def repair_town(town: str, cands: collections.Counter, flat: str) -> tuple[str, str]:
    """(town, zip) after repairing the "Town/City of X" capture from the
    report itself. A line wrap cuts a name short ("City of Casa / Grande" ->
    "Casa") and running text runs it long ("Town of Payson Gila County,",
    "City of Tucson Standard Detail"), so: extend to a longer, more frequent
    "Town of …" candidate that starts with it; trim trailing words that are
    not a name; and take the ZIP from the report's own "<Town>, Arizona 85xxx"
    addresses. A name is never shortened just to find a ZIP (a Prescott Valley
    subdivision whose services sit in Prescott stays in Prescott Valley)."""
    town = re.sub(r"\s+", " ", town).strip().rstrip("-'’ ")
    if not town or town.startswith("unincorporated"):
        return town, ""
    longer = [c for c in cands if c.startswith(town + " ") and cands[c] > cands.get(town, 0)]
    if longer:
        town = max(longer, key=lambda c: cands[c])
    elif not town_zips(town, flat):
        # "City of Lake Havasu" (informal, 3x) vs "City of Lake Havasu City" (2x): the report's own
        # addresses say which one is the place name
        addressed = [c for c in cands if c.startswith(town + " ") and town_zips(c, flat)]
        if addressed:
            town = max(addressed, key=lambda c: (sum(town_zips(c, flat).values()), cands[c]))
    while True:
        trimmed = TOWN_TAIL_RE.sub("", town)
        if trimmed == town or not trimmed:
            break
        town = trimmed
    z = town_zips(town, flat)
    return town, (z.most_common(1)[0][0] if z else "")


# ------------------------------------------------------------- pipeline

def load_ckpt() -> dict[int, dict]:
    out: dict[int, dict] = {}
    if CKPT.exists():
        for line in CKPT.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                out[int(d["id"])] = d
    return out


def csv_targets(client: Client, since: int) -> list[tuple[int, str, int]]:
    """(dev_id, registration_no, year_issued) for DM-format registrations
    issued in `since` or later, newest first."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "registrations.csv"
    if not p.exists() or time.time() - p.stat().st_mtime > 7 * 86400:
        p.write_bytes(client.get(CSV_URL).content)
    out = []
    for r in csv.DictReader(io.StringIO(p.read_text(encoding="utf-8-sig"))):
        m = re.match(r"DM\d{2}-0*(\d+)$", r.get("RegistrationNumber", "").strip())
        y = re.search(r"/(\d{4}) ", r.get("DateIssued") or "")
        if not m or not y or int(y.group(1)) < since:
            continue
        if r.get("ApplicationStatus", "") != "Issued":
            continue
        out.append((int(m.group(1)), r["RegistrationNumber"].strip(), int(y.group(1))))
    out.sort(key=lambda t: -t[0])
    return out


def fetch_one(client: Client, dev_id: int, want_pdf: bool) -> dict:
    page = client.detail_page(dev_id)
    if page is None:
        return {"id": dev_id, "missing": True}
    d = {"id": dev_id, **parse_detail(page), "pdf": False}
    if want_pdf:
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        tp = TEXT_DIR / f"{dev_id}.txt.gz"
        text = ""
        if tp.exists():
            text = gzip.decompress(tp.read_bytes()).decode("utf-8", "replace")
        else:
            pdf = client.report_pdf(page)
            if pdf:
                text = pdf_text(pdf)
                tp.write_bytes(gzip.compress(text.encode("utf-8")))
        if text:
            d["pdf"] = True
            d.update(parse_report(text, d.get("county", "")))
    return d


def cached_text(dev_id: int) -> str:
    tp = TEXT_DIR / f"{dev_id}.txt.gz"
    return gzip.decompress(tp.read_bytes()).decode("utf-8", "replace") if tp.exists() else ""


def reparse(details: dict[int, dict]) -> None:
    """Re-run parse_report over the cached text so parser improvements apply
    to every report already fetched (the checkpoint keeps the first parse)."""
    for dev_id, d in details.items():
        if d.get("missing") or not d.get("pdf"):
            continue
        text = cached_text(dev_id)
        if text:
            d.update(parse_report(text, d.get("county", "")))


def county_name(s: str) -> str:
    """'MARICOPA' / 'Maricopa' -> 'Maricopa' (the card mixes spellings)."""
    return " ".join(w.capitalize() for w in (s or "").split())


def az_county(s: str) -> str | None:
    """The Arizona county named on the detail card ('MARICOPA', 'Maricopa County',
    'Town Of Queen Creek, Maricopa County,' -> 'Maricopa'); '' when the card
    names none; None when the land is somewhere else — ADRE also registers
    out-of-state subdivisions sold to Arizonans ('Out Of State', 'Idaho',
    'Lahaina, Maui, Hawaii'), which are not Arizona associations."""
    c = county_name(s)
    for k in AZ_COUNTIES:
        if re.search(rf"\b{k}\b", c, re.I):
            return k
    return "" if not c else None


def to_records(details: dict[int, dict]) -> list[dict]:
    """One registry row per association named in any report."""
    by_name: dict[str, dict] = {}
    for dev_id in sorted(details, reverse=True):
        d = details[dev_id]
        if d.get("missing") or not d.get("pdf"):
            continue
        county = az_county(d.get("county", ""))
        if county is None:
            continue                                        # out-of-state land registered for sale in Arizona
        d["county"] = county
        issued = _iso(d.get("date_issued", ""))
        for i, name in enumerate(d.get("associations") or []):
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            row = by_name.get(key)
            sub = {"registration_no": d.get("registration_no", ""),
                   "subdivision": d.get("marketing_name") or d.get("legal_name", ""),
                   "issued": issued, "id": dev_id}
            if row is None:
                row = by_name[key] = {
                    "state": "AZ", "source": SOURCE,
                    "source_url": DETAIL_URL.format(id=dev_id),
                    "record_id": d.get("registration_no", "") or str(dev_id),
                    "name": name, "dba": "",
                    "status": "named in subdivision Public Report",
                    "status_detail": "", "recorded_date": issued,
                    "address": "", "city": d.get("city", ""),
                    "county": d.get("county", ""), "zip": d.get("zip", ""),
                    "units": None, "manager_name": "", "officers": [],
                    "registration_type": "subdivision public report",
                    "developer": d.get("developer", ""),
                    "assessment": d.get("assessment", "") if i == 0 else "",
                    "subdivisions": [],
                }
            row["subdivisions"].append(sub)
            if issued and (not row["recorded_date"] or issued < row["recorded_date"]):
                row["recorded_date"] = issued
            for f in ("city", "zip", "county"):
                if not row[f] and d.get(f):
                    row[f] = d[f]
            if i == 0 and d.get("units"):
                row["units"] = (row["units"] or 0) + d["units"]
            if i == 0 and not row["assessment"] and d.get("assessment"):
                row["assessment"] = d["assessment"]
    rows = []
    for row in by_name.values():
        n = len(row["subdivisions"])
        latest = max((s["issued"] for s in row["subdivisions"] if s["issued"]), default="")
        bits = []
        if latest:
            bits.append(f"latest issued {latest}")
        if row["assessment"]:
            bits.append(f"regular assessment {row['assessment']}")
        row["status_detail"] = "; ".join(bits)
        row["status"] = f"named in {n} subdivision Public Report{'s' if n != 1 else ''}"
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    return rows


def finalize(details: dict[int, dict], since: int, started: float) -> int:
    reparse(details)
    rows = to_records(details)
    total = write_merged(REGS, rows, {"AZ"})
    n_pdf = sum(1 for d in details.values() if d.get("pdf"))
    n_named = sum(1 for d in details.values() if d.get("associations"))
    update_coverage("AZ", "hoa_registry", {
        "source": SOURCE,
        "url": BASE + "/PdbWeb/Development/SearchDevelopments",
        "records": len(rows),
        "reports": n_pdf,
        "reports_naming_an_association": n_named,
        "years": f"{since}-{datetime.now().year}",
        "coverage": "partial",
        "note": ("Associations named in developer subdivision Public Reports (A.R.S. 32-2183), "
                 "not a registry: only subdivisions registered since the cut-off year, and only "
                 "where the report names the HOA. City/ZIP are the subdivision's town, from the "
                 "report's location and local-services addresses (town-level placement)."),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 1),
    })
    log.info("state_registries.jsonl: %d rows total, %d AZ associations from %d reports "
             "(%d named an association)", total, len(rows), n_pdf, n_named)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=int, default=2005, help="fetch reports issued this year or later")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pace", type=float, default=0.3, help="seconds between requests per worker")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many new ids (smoke test)")
    ap.add_argument("--fresh", action="store_true", help="ignore the checkpoint")
    ap.add_argument("--finalize-only", action="store_true", help="rebuild rows from the checkpoint")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    started = time.time()
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.fresh and CKPT.exists():
        CKPT.unlink()
    details = load_ckpt()
    if args.finalize_only:
        finalize(details, args.since, started)
        return 0

    client = Client(args.pace)
    targets = csv_targets(client, args.since)
    todo = [t for t in targets if t[0] not in details]
    n_have = len(targets) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    log.info("%d registrations since %d; %d already fetched; %d to fetch with %d workers",
             len(targets), args.since, n_have, len(todo), args.workers)

    lock = threading.Lock()
    done = 0
    with CKPT.open("a") as ck, ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(fetch_one, client, dev_id, True): (dev_id, reg) for dev_id, reg, _ in todo}
        for fut in as_completed(futs):
            dev_id, reg = futs[fut]
            try:
                d = fut.result()
            except Exception as exc:              # noqa: BLE001 — keep the sweep going
                log.warning("%s (%d): %s", reg, dev_id, exc)
                continue
            with lock:
                ck.write(json.dumps(d, ensure_ascii=False) + "\n")
                ck.flush()
                details[dev_id] = d
                done += 1
                if done % 100 == 0:
                    log.info("%d/%d fetched (%d named an association so far)", done, len(todo),
                             sum(1 for x in details.values() if x.get("associations")))
    finalize(details, args.since, started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
