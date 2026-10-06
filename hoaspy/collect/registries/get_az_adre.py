"""Arizona associations from ADRE subdivision Public Reports.

    python -m hoaspy az-adre                  # full run (resumable)
    python -m hoaspy az-adre --since 2015     # newer reports only
    python -m hoaspy az-adre --limit 20       # smoke test
    python -m hoaspy az-adre --finalize-only  # rebuild the rows from the checkpoint

Arizona has no HOA registry. What the Department of Real Estate publishes
is every subdivision *Public Report* (A.R.S. 32-2183): the disclosure a
developer must hand each buyer, whose "PROPERTY OWNERS' ASSOCIATIONS"
section names the association the purchaser will belong to, with its
regular assessment. That section is the only free, government source
naming Arizona HOAs, so this collector reads it:

    List/DownloadList/4                   bulk CSV of registrations (44.9k rows)
    Development/ViewDevelopment/<id>      detail card: registration number, legal /
                                          marketing name, dates, type, status, county,
                                          developer; and the Public Report form
    Development/DownloadPublicReport      POST {token, id} -> the report PDF

`<id>` is the number after the dash in a `DMyy-0<id>` registration number,
the format used since 2002. Older numeric registration numbers have no id
and their reports predate the structured association section, so they are
not fetched; reports issued before `--since` (default 2005) are skipped
for the same reason.

Text is extracted with `pdftotext` (poppler). The association name, its
assessment, the subdivision's town, lot count and the town's ZIP (the most
common "<Town>, Arizona 85xxx" in the report — the local-services
addresses, so a town-level placement, not the subdivision's own ZIP) are
parsed out; nothing else from the PDF is kept beyond a gzipped text cache.
Broker and developer contact details are never stored.

Output: Arizona's rows in records/state_registries.jsonl (replaced on
each run), one row per association (a phased subdivision names the same
HOA several times; the row lists every registration) in the registry
shape plus `developer`, `assessment` and `subdivisions`; coverage.json
collected["hoa_registry"] for AZ, noting that this is a developer
disclosure roster. Checkpoint: .cache/az_adre/details.jsonl (one line per
id, appended); text cache .cache/az_adre/text/<id>.txt.gz.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import html
import io
import logging
import re
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from hoaspy import ROOT
from hoaspy.collect.registries import REGISTRIES_PATH
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import append_row, now_iso, read_rows, replace_rows
from hoaspy.lib.records import Association

CACHE_DIR = ROOT / ".cache" / "az_adre"
CHECKPOINT_PATH = CACHE_DIR / "details.jsonl"
REGISTRATIONS_PATH = CACHE_DIR / "registrations.csv"
TEXT_DIR = CACHE_DIR / "text"
CACHE_DAYS = 7
ADRE_URL = "https://services.azre.gov"
CSV_URL = ADRE_URL + "/PdbWeb/List/DownloadList/4"
DETAIL_URL = ADRE_URL + "/PdbWeb/Development/ViewDevelopment/{id}"
REPORT_URL = ADRE_URL + "/PdbWeb/Development/DownloadPublicReport"
SOURCE_PAGE = ADRE_URL + "/PdbWeb/Development/SearchDevelopments"
SOURCE = "Arizona Department of Real Estate — subdivision Public Reports"
DEFAULT_SINCE = 2005

log = logging.getLogger(__name__)

DEFINITION_RE = re.compile(r"<dt[^>]*>\s*(.*?)\s*</dt>\s*<dd[^>]*>(.*?)</dd>", re.S)
CARD_FIELDS = {"Registration Number": "registration_no", "Legal Name": "legal_name",
               "Marketing Name": "marketing_name", "Date Filed": "date_filed",
               "Date Issued": "date_issued", "Application Type": "application_type",
               "Application Status": "application_status", "Lot Status": "lot_status",
               "County": "county"}
NOT_A_NAME = ("NONE", "N/A", "")
GENERIC_NAMES = {"association", "homeowners association", "homeowners' association",
                 "homeowner's association", "master association", "community association",
                 "property owners association", "property owners' association",
                 "condominium association", "owners association", "the association",
                 "homeowners associations", "property owners associations",
                 "an association", "a homeowners association"}


def html_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def iso_date(text: str) -> str:
    """'1/22/2003' -> '2003-01-22'; '' when the text is not a date."""
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}" if match else ""


# ---- the site ------------------------------------------------------------------

class Client:
    """One requests session per thread: the Public Report form's anti-forgery
    token is bound to the session cookie that served the detail page."""

    def __init__(self, pace: float = 0.3):
        self.pace = pace
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = http.session()
            self._local.last = 0.0
        return self._local.session

    def _wait(self) -> None:
        if not hasattr(self._local, "last"):
            self._local.last = 0.0
        wait = self._local.last + self.pace - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._local.last = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        for attempt in range(4):
            try:
                self._wait()
                response = self.session.get(url, timeout=120, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                log.warning("GET %s: %s (attempt %d)", url, exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"gave up on {url}")

    def detail_page(self, dev_id: int) -> str | None:
        body = self.get(DETAIL_URL.format(id=dev_id)).text
        return None if "Record Unavailable" in body or "Development Details" not in body else body

    def report_pdf(self, page: str) -> bytes | None:
        """The Public Report PDF the detail page's form downloads, or None."""
        form = re.search(r'<form action="/PdbWeb/Development/DownloadPublicReport".*?</form>', page, re.S)
        if not form:
            return None
        fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', form.group(0)))
        boundary = "----hoaspy" + uuid.uuid4().hex
        body = b""
        for key, value in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        body += f"--{boundary}--\r\n".encode()
        for attempt in range(3):
            try:
                self._wait()
                response = self.session.post(REPORT_URL, data=body, timeout=180,
                                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                if response.status_code == 200 and response.content[:4] == b"%PDF":
                    return response.content
                log.warning("report POST: %s %s", response.status_code, response.headers.get("Content-Type"))
                return None
            except requests.RequestException as exc:
                log.warning("report POST: %s (attempt %d)", exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        return None


# ---- the detail card ----------------------------------------------------------

def parse_detail(page: str) -> dict:
    """The development and developer fields of a ViewDevelopment card."""
    card: dict = {}
    development, _, developer = page.partition("Developer Details")
    for label, value in DEFINITION_RE.findall(development):
        label = html_text(label)
        if label in CARD_FIELDS:
            card[CARD_FIELDS[label]] = html_text(value)
    for label, value in DEFINITION_RE.findall(developer):
        if html_text(label) == "Legal Name":
            card["developer"] = html_text(value)
            break
    for key in ("marketing_name", "developer"):
        if card.get(key, "").upper() in NOT_A_NAME:
            card[key] = ""
    card["county"] = county_name(card.get("county", ""))
    return card


def pdf_text(pdf: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as fh:
        fh.write(pdf)
        fh.flush()
        result = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, timeout=300, check=False)
    return result.stdout.decode("utf-8", "replace")


# ---- the report text ----------------------------------------------------------

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
ASSESSMENT_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d{2})?)\s*(?:per|/|a|each|every)\s*(month|mo\.?|year|yr\.?|annum|quarter|"
    r"semi-?annual(?:ly)?|annual(?:ly)?)", re.I)
PERIOD_WORDS = {"mo": "month", "yr": "year", "annum": "year", "annually": "year", "annual": "year"}
UNITS_RE = re.compile(
    r"(?:consists?\s+of|contains?|comprised\s+of|composed\s+of|(?:a\s+)?total\s+of)\s+(\d[\d,]*)\s+"
    r"(?:residential\s+|single[- ]family\s+|attached\s+|detached\s+)?(?:lots?|units?|homes?|"
    r"condominium\s+units?|townho\w+|parcels?)\b", re.I)
DIVIDED_RE = re.compile(
    r"divided\s+into\s+(\d[\d,]*)\s+(?:residential\s+|single[- ]family\s+)?"
    r"(?:lots?|units?|homes?|condominium\s+units?)\b", re.I)
LOCATION_RE = re.compile(r"SUBDIVISION\s+LOCATION[:\s]*(.{0,600}?)(?:\n\s*\n|\n[A-Z][A-Z /&]{5,}:)",
                         re.S)
TOWN_RE = re.compile(r"\b(?:Town|City)\s+of\s+"
                     r"((?:(?:St|Ft|Mt)\.\s)?[A-Z][a-z'’-]+(?:\s(?:[A-Z][a-z'’-]+|de|del|la)){0,2}?)"
                     r"(?=,|\.|;|:|\s+(?:in|within|and|Arizona|AZ|will|shall|is|has|for|or|at|on|to|which|where|"
                     r"Water|Fire|Police|Planning|Public|Building|Engineering|Utilities|Parks|Sanitation|Solid|Sewer|"
                     r"Streets|Transportation|Environmental|Development|Community|Department|Services|Division|"
                     r"Located|"
                     r"Phone|Provider|Municipal|Code|Zoning|Ordinance|General|Plan|Aviation|Easement)\b|"
                     r"\s+(?!(?:de|del|la)\b)[a-z]|\n|$)")
NOT_A_TOWN = {"Arizona", "Phoenix Fire", "Maricopa County", "Pima County", "New York", "Los Angeles"}
# "…, Tucson, Pima County, Arizona": the legal-location phrasing when no "Town/City of" is named.
LOCATION_TOWN_RE = re.compile(r"(?<![A-Za-z])([A-Z][a-z'’-]+(?:\s(?:[A-Z][a-z'’-]+|de|del|la)){0,2}),"
                              r"\s+((?:[A-Z][a-z]+\s+){1,2})County,\s+(?:State\s+of\s+)?(?:Arizona|AZ)\b")
LOCATION_PREFIX_RE = re.compile(
    r"^(?:Unincorporated|Incorporated|City|Town|Village|In|At|Near|The|Downtown)(?:\s+of)?(?:\s+|$)")
STREET_HEAD_RE = re.compile(r"^(?:Avenida|Camino|Calle|Via|Paseo|Placita|Vereda|Circulo|Corte)\b")
# "Hayward Avenue Phoenix" (the comma between street and town is missing):
# the town is what follows the street type.
STREET_MID_RE = re.compile(r"^.*\b(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Parkway|Pkwy|"
                           r"Highway|Hwy|Trail|Loop|Circle|Cir|Place|Pl|Court|Ct)\.?\s+(?=[A-Z])")
GENERIC_WORDS = {"City", "Town", "County", "Village", "Streets", "Street", "Community", "Arizona"}
AZ_COUNTIES = ("Apache", "Cochise", "Coconino", "Gila", "Graham", "Greenlee", "La Paz", "Maricopa", "Mohave", "Navajo",
               "Pima", "Pinal", "Santa Cruz", "Yavapai", "Yuma")
STREET_TAIL_RE = re.compile(r"\b(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Parkway|Pkwy|"
                            r"Highway|Hwy|Trail|Loop|Circle|Cir|Place|Pl|Court|Ct|Route|Rte|Freeway|Fwy|Interstate|"
                            r"Corner|Intersection|Section|Township|Range|Miles?|North|South|East|West|Meridian|"
                            r"Baseline|Roads|Streets|Avenues|Drives|Lanes)\.?$")
LOCATION_ZIP_RE = re.compile(r"(?:Arizona|AZ),?\s+(8[56]\d{3})\b")
# Words the "Town/City of X" capture runs into that are not part of the name.
TOWN_TAIL_RE = re.compile(r"\s+(?:[A-Z][a-z]+\s+County|Boundary|Limits|Landfill|Gas|Utility|Standard|Detail|"
                          r"Corporate|Hall|Council|Manager|Office|Airport|Library|Booster|Station|Plant|Facility|"
                          r"Reservoir|Wells?)$")
JUNK_WORDS_RE = re.compile(
    r"\b(?:PARTICIPATE|CONTROL|RESPONSIBILIT\w*|OBLIGATIONS?|DEMANDS?|OWNED|DEEDED?|CONSISTENT|REQUIREMENTS?|"
    r"PURCHASERS?|ADJACENT|WASHES|DECLARATION|PLAT|WITHIN|NOTE|PAYMENTS?|RECORDED|RESTRICTIONS|CURRENT|"
    r"ASSESSMENTS?|MANAGEMENT|PHONE|DUES|FEES?|MAINTAIN\w*|SAID|SUCH|THIS|YOUR|ITS|SHALL|WILL|MUST|ARE|IS|BE|"
    r"CC&RS?|BYLAWS|MEMBERSHIP|MEMBERS?|SUBDIVIDER|DEVELOPER|LOTS?|TRACTS?|CONVEYED|DEEDS?|THAT|ARTICLES|"
    r"INCORPORATION|COMMON|AREAS?|TITLE|EASEMENTS?|DEDICATES?|GRANTS?|HEREBY)\b", re.I)
TYPE_HINT_RE = re.compile(r"homeowner|home owner|community|property owner|condominium|condo|master|owners|residential|"
                          r"neighborhood|villa|estates|townho|\bHOA\b|\bPOA\b", re.I)
CUT_AT_RE = re.compile(r"\s+(?:NOTE:|PAYMENTS TO|Management Company|Current (?:Dues |Regular )?Assessments?|PHONE|"
                       r"Purchasers?\b|Type:|which\b|who\b|that\b|is\b|are\b|will\b|shall\b|has\b|may\b)")
CONNECTIVES = {"at", "of", "the", "and", "de", "del", "la", "on", "in", "by", "for", "y", "e", "&"}
FORM_NAME_RE = re.compile(r"Name of the HOA:\s*(.*)$", re.M)
FORM_ASSESSMENT_RE = re.compile(r"Current (?:Dues |Regular |Annual |Monthly )?Assessments?:")
UNINCORPORATED_RE = re.compile(r"unincorporated\s+(?:area\s+of\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+County", re.I)
LEADING_ARTICLES = ("the ", "The ", "THE ")
TYPE_WORDS = {"the", "a", "an", "association", "associations", "assn", "inc", "homeowners", "homeowner",
              "home", "owners", "owner", "property", "community", "master", "residential", "neighborhood",
              "condominium", "condo", "townhome", "townhomes", "villas", "villa", "sub", "hoa", "poa", "of",
              "said", "this", "such", "your", "its"}


def trim_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ,.;:")
    for article in LEADING_ARTICLES:
        if name.startswith(article):
            name = name[len(article):]
    return name


def is_generic_name(name: str) -> bool:
    """True for 'the Association', 'Homeowners Association', 'Residential
    Association' …: a name with no word that is not a type word."""
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return len(name) < 8 or not [word for word in words if word not in TYPE_WORDS]


def clean_names(raw: str) -> list[str]:
    """Split 'X and Y' / 'X or Y', cut trailing boilerplate, and reject
    sentence fragments: anything with boilerplate words, prices, colons,
    lower-case non-connective words, or more than ten words."""
    raw = re.sub(r"\s+", " ", raw or "").strip(" .,;:")
    out = []
    for part in re.split(r",?\s+(?:and|or|AND|OR)\s+(?=[A-Z])", raw):
        part = trim_name(CUT_AT_RE.split(part, 1)[0].strip(" .,;:"))
        words = part.split()
        if not (2 <= len(words) <= 10) or len(part) > 80:
            continue
        if "$" in part or ":" in part or JUNK_WORDS_RE.search(part) or re.search(r"\.\s+[A-Z]", part):
            continue
        if any(word[:1].islower() and word.lower() not in CONNECTIVES for word in words):
            continue
        if words[0].lower() in CONNECTIVES | {"to", "or", "by", "with"} or is_generic_name(part):
            continue
        # 'the' mid-name only after a preposition ("Ascent at the Phoenician")
        lowered = [word.lower() for word in words]
        if any(word == "the" and (i == 0 or lowered[i - 1] not in ("at", "of", "on", "in"))
               for i, word in enumerate(lowered)):
            continue
        out.append(part)
    return out


def form_block(text: str) -> tuple[str, str]:
    """(HOA name, assessment text) from the two-column "Name of the HOA: …
    Current Assessments: …" form of newer reports; ('', '') when absent.
    Each column wraps onto the following lines until a blank line, so the
    columns are split at the "Current Assessments:" x-position."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = FORM_NAME_RE.search(line)
        if not match:
            continue
        assessment = FORM_ASSESSMENT_RE.search(line)
        split = assessment.start() if assessment else len(line)
        left = [line[match.start(1):split]]
        right = [line[assessment.end():] if assessment else ""]
        started = bool(left[0].strip())
        for following in lines[i + 1:i + 8]:
            if started and not assessment:
                break                   # a single-column form line: no wrapped name
            if not following.strip():
                if started:
                    break
                continue                # the name sits below an empty form line
            if re.match(r"\s*(?:Payment frequency|Membership|Who will)", following):
                break
            started = True
            gap = re.search(r"\s{3,}", following[:split].rstrip() + "   ")     # the column boundary
            left.append(following[:min(split, gap.start() if gap else split)])
            right.append(following[split:])
        name = re.sub(r"\s+", " ", " ".join(left)).strip(" .,;")
        name = re.sub(r"^(?:Type:\s*)", "", name)
        name = re.split(r",?\s+an?\s+Arizona|\s*\(|,\s+an?\s+non-?profit|\s+amount of\b|\s+Type:", name)[0]
        name = name.strip(" .,;")
        if re.fullmatch(r"(?i)(?:none|n/?a|not applicable|-|tbd)?", name):
            name = ""
        return name, re.sub(r"\s+", " ", " ".join(right))
    return "", ""


def association_names(text: str) -> list[str]:
    """The association names the report says the purchaser will belong to,
    in order of confidence: the form's own field, then explicit "will belong
    to X" sentences, then proper-cased names repeated in the text, then
    all-caps ones."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        for name in clean_names(raw):
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if key not in seen:
                seen.add(key)
                found.append(name)

    form_name, _ = form_block(text)
    if form_name:
        add(form_name)
        if found:                       # the form is authoritative when present
            return found
    flat = re.sub(r"[ \t]+", " ", text)
    for match in BELONG_RE.finditer(flat):
        add(match.group(1))
    if found:
        return found
    counts: collections.Counter = collections.Counter()
    for pattern in (PROPER_ASSOC_RE, CAPS_ASSOC_RE):
        for match in pattern.finditer(flat):
            for name in clean_names(match.group(1)):
                if TYPE_HINT_RE.search(name):          # a bare "… Association" is not an HOA name
                    counts[name] += 1
    for name, count in counts.most_common():
        if count >= 2:
            add(name)
        if len(found) >= 2:
            break
    return found


def parse_report(text: str, county: str = "") -> dict:
    """Association, assessment, town, ZIP and lot count from the report text."""
    flat = re.sub(r"[ \t]+", " ", text)
    names = association_names(text)
    out: dict = {"associations": names, "assessment": "", "city": "", "zip": "", "units": None}
    _, form_assessment = form_block(text)
    if names:
        at = flat.find(names[0])
        window = flat[at:at + 2500] if at >= 0 else ""
        match = (ASSESSMENT_RE.search(form_assessment) or ASSESSMENT_RE.search(window)
                 or ASSESSMENT_RE.search(flat[flat.find("ASSOCIATION"):][:6000]))
        if match:
            period = match.group(2).lower().rstrip(".")
            out["assessment"] = f"${match.group(1)}/{PERIOD_WORDS.get(period, period)}"
    match = DIVIDED_RE.search(flat) or UNITS_RE.search(flat)
    if match:
        try:
            out["units"] = int(match.group(1).replace(",", "")) or None
        except ValueError:
            pass
    location = LOCATION_RE.search(flat)
    town, location_zip = "", ""
    candidates = collections.Counter(re.sub(r"\s+", " ", found.group(1)) for found in TOWN_RE.finditer(flat))
    if location:
        section = location.group(1)
        town_match = TOWN_RE.search(section)
        if town_match and re.sub(r"\s+", " ", town_match.group(1)) not in NOT_A_TOWN:
            town = unwrap_town(town_match.group(1), section[town_match.end(1):], flat)
        else:
            legal = LOCATION_TOWN_RE.search(section)
            unincorporated = UNINCORPORATED_RE.search(section)
            named = LOCATION_PREFIX_RE.sub("", re.sub(r"\s+", " ", legal.group(1))) if legal else ""
            rest = STREET_MID_RE.sub("", named)
            if rest != named and rest not in GENERIC_WORDS:
                named = rest                               # "Hayward Avenue Phoenix" -> Phoenix (Circle City stays)
            # "…, St. Johns, Apache County" / ", Ft. Mohave": the abbreviation is part of the name when it
            # follows a comma or a preposition; after "1st" or "Main" it is a street
            abbreviation = re.search(r"(?:^|[,;:]|\b(?:of|in|at|near|to|the))\s*(St|Ft|Mt)\.\s*$",
                                     section[:legal.start(1)]) if legal else None
            if abbreviation and named:
                full = f"{abbreviation.group(1)}. {named}"
                if town_zips(full, flat) or not town_zips(named, flat):
                    named = full
            if named and named not in NOT_A_TOWN and named not in GENERIC_WORDS and not named.endswith("County") \
                    and not STREET_TAIL_RE.search(named) and not STREET_HEAD_RE.search(named):
                town = named                              # "…, Tucson, Pima County, Arizona"
            elif unincorporated:
                town = f"unincorporated {unincorporated.group(1)} County"
            elif legal and (not named or named.endswith("County")):
                town = f"unincorporated {legal.group(2).strip()} County"   # "…, Unincorporated, Apache County, Arizona"
        zip_match = LOCATION_ZIP_RE.search(section)
        if zip_match:
            location_zip = zip_match.group(1)
    if not town or not plausible_town(town):
        town = next((candidate for candidate, _ in candidates.most_common() if plausible_town(candidate)), "")
    town, zip_code = repair_town(town, candidates, flat)
    if not plausible_town(town):                 # "Ma" from a letter-spaced "Town of Ma r a n a": try the rest
        town = next((candidate for candidate, _ in candidates.most_common()
                     if plausible_town(candidate) and candidate != town), "")
        town, zip_code = repair_town(town, candidates, flat)
        if not plausible_town(town):
            town, zip_code = "", ""
    out["city"] = town
    out["zip"] = location_zip or zip_code
    return out


def plausible_town(town: str) -> bool:
    """Three letters or more (Ajo and Why are real; "Ma" and "St" are not)
    and not a known non-town."""
    return len(re.sub(r"[^A-Za-z]", "", town)) >= 3 and town not in NOT_A_TOWN and town not in GENERIC_WORDS


def unwrap_town(town: str, after: str, flat: str) -> str:
    """"City of Casa" at a line end whose next line starts "Grande, Pinal
    County" is one name split by the wrap: join them when the joined name
    is used as a place elsewhere in the report ("Casa Grande," or "Casa
    Grande, Arizona"), i.e. it occurs at least twice."""
    next_line = re.match(r"\n([A-Z][a-z'’-]+(?:\s[A-Z][a-z'’-]+)?)(?=,|\.|\s)", after)
    if not next_line:
        return town
    joined = re.sub(r"\s+", " ", f"{town} {next_line.group(1)}")
    pattern = r"\s+".join(re.escape(word) for word in joined.split()) + r"(?:,|\s+(?:Arizona|AZ)\b)"
    return joined if len(re.findall(pattern, flat)) >= 2 else town


def town_zips(town: str, flat: str) -> collections.Counter:
    """ZIPs from '<town>, Arizona 85xxx' addresses in the text (the words may
    wrap across a line, so they are joined with \\s+ rather than a space)."""
    pattern = r"\s+".join(re.escape(word) for word in town.split()) + r",?\s+(?:Arizona|AZ),?\s+(8[56]\d{3})\b"
    return collections.Counter(re.findall(pattern, flat, re.I))


def repair_town(town: str, candidates: collections.Counter, flat: str) -> tuple[str, str]:
    """(town, zip) after repairing the "Town/City of X" capture from the
    report itself. A line wrap cuts a name short ("City of Casa / Grande" ->
    "Casa") and running text runs it long ("Town of Payson Gila County,",
    "City of Tucson Standard Detail"), so: extend to a longer, more frequent
    "Town of …" candidate that starts with it; trim trailing words that are
    not a name; and take the ZIP from the report's own "<Town>, Arizona
    85xxx" addresses. A name is never shortened just to find a ZIP (a
    Prescott Valley subdivision whose services sit in Prescott stays in
    Prescott Valley)."""
    town = re.sub(r"\s+", " ", town).strip().rstrip("-'’ ")
    if not town or town.startswith("unincorporated"):
        return town, ""
    longer = [candidate for candidate in candidates
              if candidate.startswith(town + " ") and candidates[candidate] > candidates.get(town, 0)]
    if longer:
        town = max(longer, key=lambda candidate: candidates[candidate])
    elif not town_zips(town, flat):
        # "City of Lake Havasu" (informal, 3x) vs "City of Lake Havasu City" (2x): the report's own
        # addresses say which one is the place name
        addressed = [candidate for candidate in candidates
                     if candidate.startswith(town + " ") and town_zips(candidate, flat)]
        if addressed:
            town = max(addressed,
                       key=lambda candidate: (sum(town_zips(candidate, flat).values()), candidates[candidate]))
    while True:
        trimmed = TOWN_TAIL_RE.sub("", town)
        if trimmed == town or not trimmed:
            break
        town = trimmed
    zips = town_zips(town, flat)
    return town, (zips.most_common(1)[0][0] if zips else "")


def county_name(text: str) -> str:
    """'MARICOPA' / 'Maricopa' -> 'Maricopa' (the card mixes spellings)."""
    return " ".join(word.capitalize() for word in (text or "").split())


def az_county(text: str) -> str | None:
    """The Arizona county named on the detail card ('MARICOPA', 'Maricopa
    County', 'Town Of Queen Creek, Maricopa County,' -> 'Maricopa'); ''
    when the card names none; None when the land is somewhere else — ADRE
    also registers out-of-state subdivisions sold to Arizonans ('Out Of
    State', 'Idaho', 'Lahaina, Maui, Hawaii'), which are not Arizona
    associations."""
    name = county_name(text)
    for county in AZ_COUNTIES:
        if re.search(rf"\b{county}\b", name, re.I):
            return county
    return "" if not name else None


# ---- the pipeline -------------------------------------------------------------

def load_checkpoint() -> dict[int, dict]:
    """{development id: detail} fetched by earlier runs."""
    return {int(detail["id"]): detail for detail in read_rows(CHECKPOINT_PATH, skip_bad=True)}


def csv_targets(client: Client, since: int) -> list[tuple[int, str, int]]:
    """(dev_id, registration_no, year_issued) for DM-format registrations
    issued in `since` or later, newest first."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not REGISTRATIONS_PATH.exists() or time.time() - REGISTRATIONS_PATH.stat().st_mtime > CACHE_DAYS * 86400:
        REGISTRATIONS_PATH.write_bytes(client.get(CSV_URL).content)
    out = []
    for row in csv.DictReader(io.StringIO(REGISTRATIONS_PATH.read_text(encoding="utf-8-sig"))):
        number = re.match(r"DM\d{2}-0*(\d+)$", row.get("RegistrationNumber", "").strip())
        year = re.search(r"/(\d{4}) ", row.get("DateIssued") or "")
        if not number or not year or int(year.group(1)) < since:
            continue
        if row.get("ApplicationStatus", "") != "Issued":
            continue
        out.append((int(number.group(1)), row["RegistrationNumber"].strip(), int(year.group(1))))
    out.sort(key=lambda target: -target[0])
    return out


def cached_text(dev_id: int) -> str:
    path = TEXT_DIR / f"{dev_id}.txt.gz"
    return gzip.decompress(path.read_bytes()).decode("utf-8", "replace") if path.exists() else ""


def fetch_one(client: Client, dev_id: int, want_pdf: bool) -> dict:
    """The detail card of one development, with the report parsed when `want_pdf`."""
    page = client.detail_page(dev_id)
    if page is None:
        return {"id": dev_id, "missing": True}
    detail = {"id": dev_id, **parse_detail(page), "pdf": False}
    if want_pdf:
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        text = cached_text(dev_id)
        if not text:
            pdf = client.report_pdf(page)
            if pdf:
                text = pdf_text(pdf)
                (TEXT_DIR / f"{dev_id}.txt.gz").write_bytes(gzip.compress(text.encode("utf-8")))
        if text:
            detail["pdf"] = True
            detail.update(parse_report(text, detail.get("county", "")))
    return detail


def reparse(details: dict[int, dict]) -> None:
    """Re-run parse_report over the cached text, so parser improvements
    apply to every report already fetched (the checkpoint keeps the first parse)."""
    for dev_id, detail in details.items():
        if detail.get("missing") or not detail.get("pdf"):
            continue
        text = cached_text(dev_id)
        if text:
            detail.update(parse_report(text, detail.get("county", "")))


def to_records(details: dict[int, dict], retrieved_at: str = "") -> list[Association]:
    """One registry record per association named in any report."""
    by_name: dict[str, Association] = {}
    for dev_id in sorted(details, reverse=True):
        detail = details[dev_id]
        if detail.get("missing") or not detail.get("pdf"):
            continue
        county = az_county(detail.get("county", ""))
        if county is None:
            continue                                        # out-of-state land registered for sale in Arizona
        detail["county"] = county
        issued = iso_date(detail.get("date_issued", ""))
        for i, name in enumerate(detail.get("associations") or []):
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            record = by_name.get(key)
            if record is None:
                record = by_name[key] = Association(
                    state="AZ", source=SOURCE, source_url=DETAIL_URL.format(id=dev_id),
                    record_id=detail.get("registration_no", "") or str(dev_id), name=name,
                    status="named in subdivision Public Report", recorded_date=issued,
                    city=detail.get("city", ""), county=detail.get("county", ""), zip=detail.get("zip", ""),
                    registration_type="subdivision public report", retrieved_at=retrieved_at,
                    extras={"developer": detail.get("developer", ""),
                            "assessment": detail.get("assessment", "") if i == 0 else "",
                            "subdivisions": []})
            record.extras["subdivisions"].append({
                "registration_no": detail.get("registration_no", ""),
                "subdivision": detail.get("marketing_name") or detail.get("legal_name", ""),
                "issued": issued, "id": dev_id})
            if issued and (not record.recorded_date or issued < record.recorded_date):
                record.recorded_date = issued
            for column in ("city", "zip", "county"):
                if not getattr(record, column) and detail.get(column):
                    setattr(record, column, detail[column])
            if i == 0 and detail.get("units"):
                record.units = (record.units or 0) + detail["units"]
            if i == 0 and not record.extras["assessment"] and detail.get("assessment"):
                record.extras["assessment"] = detail["assessment"]
    records = []
    for record in by_name.values():
        subdivisions = record.extras["subdivisions"]
        latest = max((entry["issued"] for entry in subdivisions if entry["issued"]), default="")
        detail_bits = []
        if latest:
            detail_bits.append(f"latest issued {latest}")
        if record.extras["assessment"]:
            detail_bits.append(f"regular assessment {record.extras['assessment']}")
        record.status_detail = "; ".join(detail_bits)
        record.status = f"named in {len(subdivisions)} subdivision Public Report{'s' if len(subdivisions) != 1 else ''}"
        records.append(record)
    records.sort(key=lambda record: record.name)
    return records


def finalize(details: dict[int, dict], since: int, started: float) -> int:
    reparse(details)
    retrieved_at = now_iso()
    records = to_records(details, retrieved_at)
    total = replace_rows(REGISTRIES_PATH, (record.as_row() for record in records), {"AZ"})
    with_pdf = sum(1 for detail in details.values() if detail.get("pdf"))
    naming = sum(1 for detail in details.values() if detail.get("associations"))
    update_coverage("AZ", "hoa_registry", {
        "source": SOURCE, "url": SOURCE_PAGE, "records": len(records), "reports": with_pdf,
        "reports_naming_an_association": naming, "years": f"{since}-{datetime.now().year}", "coverage": "partial",
        "note": ("Associations named in developer subdivision Public Reports (A.R.S. 32-2183), "
                 "not a registry: only subdivisions registered since the cut-off year, and only "
                 "where the report names the HOA. City/ZIP are the subdivision's town, from the "
                 "report's location and local-services addresses (town-level placement)."),
        "retrieved_at": retrieved_at, "duration_seconds": round(time.time() - started, 1),
    })
    log.info("%s: %d rows total, %d AZ associations from %d reports (%d named an association)",
             REGISTRIES_PATH.name, total, len(records), with_pdf, naming)
    return len(records)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", type=int, default=DEFAULT_SINCE, help="fetch reports issued this year or later")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pace", type=float, default=0.3, help="seconds between requests per worker")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many new ids (smoke test)")
    parser.add_argument("--fresh", action="store_true", help="ignore the checkpoint")
    parser.add_argument("--finalize-only", action="store_true", help="rebuild the rows from the checkpoint")


def run(args: argparse.Namespace) -> int:
    started = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
    details = load_checkpoint()
    if args.finalize_only:
        finalize(details, args.since, started)
        return 0

    client = Client(args.pace)
    targets = csv_targets(client, args.since)
    todo = [target for target in targets if target[0] not in details]
    have = len(targets) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    log.info("%d registrations since %d; %d already fetched; %d to fetch with %d workers",
             len(targets), args.since, have, len(todo), args.workers)

    lock = threading.Lock()
    done = 0
    with CHECKPOINT_PATH.open("a", encoding="utf-8") as checkpoint, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(fetch_one, client, dev_id, True): (dev_id, registration)
                   for dev_id, registration, _ in todo}
        for future in as_completed(futures):
            dev_id, registration = futures[future]
            try:
                detail = future.result()
            except Exception as exc:              # keep the sweep going
                log.warning("%s (%d): %s", registration, dev_id, exc)
                continue
            with lock:
                append_row(checkpoint, detail)
                details[dev_id] = detail
                done += 1
                if done % 100 == 0:
                    log.info("%d/%d fetched (%d named an association so far)", done, len(todo),
                             sum(1 for entry in details.values() if entry.get("associations")))
    finalize(details, args.since, started)
    return 0
