"""Pennsylvania Unified Judicial System web portal — case search by
organization name.

    https://ujsportal.pacourts.us/CaseSearch

Anonymous, no captcha; an ASP.NET Core antiforgery token from one GET per
session is the only state. Organization search is SQL LIKE and must carry
one narrowing field: a 1900-to-today filing-date range returns every
docket type in one document (no server paging, no cap observed).

Captions abbreviate: "Hemlock Farms Community Association" is docketed as
"Hemlock Farms Community Assoc." (331 cases), "Hemlock Farms Comm Assoc"
(36) and spelled out in only 9 of 411. So the query is the name's leading
words plus the stem of its first association word ("Hemlock Farms Comm%",
see query_name), and party_matches() folds the abbreviations before
comparing instead of looking for the registered spelling verbatim. A
record's `associations` is therefore the name that was queried, not the
caption's spelling (that stays in `case_name`), so a consumer does not
take "Hemlock Farms Comm Assoc" for a second community.

Coverage caveat: association hits are Magisterial District Judge dockets
(MJ-*-CV/LT-*). Common Pleas *civil* dockets are not on this portal — they
live in each county prothonotary's system — so this is the small-claims
and landlord-tenant tier of HOA litigation, not the whole of it.
"""

from __future__ import annotations

import datetime as dt
import html
import re

from hoaspy.lib import http
from hoaspy.lib.http import BROWSER_USER_AGENT

from . import RateLimited
from ._common import clean, has_business_form, normalize, record

STATE = "PA"
KEY = "pa_ujs"
NEEDS_COOKIE = False
PORTAL_URL = "https://ujsportal.pacourts.us"
SEARCH_URL = f"{PORTAL_URL}/CaseSearch"
INFO = {
    "name": "Pennsylvania UJS Web Portal — organization-name case search",
    "url": SEARCH_URL,
    "access": "anonymous, no captcha; per-association organization-name search",
    "coverage": "Magisterial District Judge dockets statewide (civil + landlord/tenant); "
                "Common Pleas civil dockets are NOT on this portal",
    "caveat": "best-effort — prefix search on the name's leading words, caption "
              "abbreviations folded; MDJ tier only",
}
FIRST_DAY = "1900-01-01"
# Every field the search form posts; the ones not used stay empty.
SEARCH_FORM_FIELDS = ("SearchBy ParticipantSID ParticipantSSN FiledStartDate FiledEndDate County "
                      "JudicialDistrict MDJSCourtOffice DocketType CaseCategory CaseStatus "
                      "DriversLicenseState PADriversLicenseNumber ArrestingAgency ORI JudgeNameID "
                      "AppellateCourtName AppellateDistrict AppellateDocketType AppellateCaseCategory "
                      "AppellateCaseType AppellateAgency AppellateTrialCourt AppellateTrialCourtJudge "
                      "AppellateCaseStatus OrganizationName ParticipantRole ParcelState ParcelCounty "
                      "ParcelMunicipality CourtOffice CourtRoomID CalendarEventType").split()
# The result grid's columns, in order; underscored ones are not read.
RESULT_COLUMNS = ["_hidden0", "_hidden1", "docket_number", "court_type", "caption", "case_status",
                  "filing_date", "primary_participants", "dob", "county", "court_office", "otn",
                  "complaint_no", "incident_no", "event_type", "event_status", "event_date",
                  "event_location", "_icons"]
ENTITY_SUFFIX_RE = re.compile(r"[,\s]+(inc\.?|incorporated|llc|corp\.?|corporation|ltd\.?)\s*$", re.I)
ET_AL_RE = re.compile(r",?\s*et al\.?$", re.I)
DOCKET_KINDS = {"CV": "Civil", "LT": "Landlord/Tenant", "CR": "Criminal", "NT": "Non-Traffic",
                "TR": "Traffic", "MD": "Miscellaneous"}
# Caption spellings of the association vocabulary, folded to one form.
ABBREVIATIONS = {"ASSN": "ASSOCIATION", "ASSO": "ASSOCIATION", "COMM": "COMMUNITY",
                 "CMNTY": "COMMUNITY", "CONDO": "CONDOMINIUM", "CONDOS": "CONDOMINIUM",
                 "CONDOMINIUMS": "CONDOMINIUM", "HOMEOWNER": "HOMEOWNERS", "PROP": "PROPERTY",
                 "OWNER": "OWNERS", "RESIDENT": "RESIDENTS", "TOWNHOMES": "TOWNHOME",
                 "TOWNHOUSES": "TOWNHOUSE", "PROPERTYOWNERS": "PROPERTY OWNERS",
                 "HOA": "HOMEOWNERS ASSOCIATION", "POA": "PROPERTY OWNERS ASSOCIATION",
                 "COA": "CONDOMINIUM ASSOCIATION"}
# Words that say what kind of body it is, not which one.
GENERIC_WORDS = {"ASSOCIATION", "COMMUNITY", "CONDOMINIUM", "HOMEOWNERS", "PROPERTY", "OWNERS",
                 "RESIDENTS", "TOWNHOME", "TOWNHOUSE", "MASTER", "UNIT", "LOT", "PLANNED"}
# What to search for in place of the first generic word: short enough to
# reach its abbreviations ("Comm" finds Community, Comm and Comm.Assoc.).
STEMS = {"ASSOCIATION": "Ass", "COMMUNITY": "Comm", "CONDOMINIUM": "Condo",
         "HOMEOWNERS": "Home", "PROPERTY": "Prop", "OWNERS": "Owner", "RESIDENTS": "Resident"}
ACRONYMS = ("HOA", "POA", "COA")


def cell_text(cell: str) -> str:
    cell = re.sub(r"<br\s*/?>", "; ", cell)
    return clean(html.unescape(re.sub(r"<[^>]+>", "", cell)))


def parse_results(body: str) -> list[dict]:
    """The result grid's rows, keyed by RESULT_COLUMNS, each with its docket-sheet link."""
    grid = re.search(r'<table[^>]*id="caseSearchResultGrid".*?</table>', body, re.S)
    if not grid:
        raise RuntimeError("no caseSearchResultGrid in response (validation failure?)")
    tbody = re.search(r"<tbody[^>]*>(.*?)</tbody>", grid.group(0), re.S)
    hits = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(1) if tbody else "", re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) < len(RESULT_COLUMNS):
            continue
        hit = {key: cell_text(cell) for key, cell in zip(RESULT_COLUMNS, cells, strict=False)
               if not key.startswith("_")}
        links = {}
        for label, href in re.findall(r'aria-label="([^"]+)" href="([^"]+)"', cells[-1]):
            links.setdefault(label, PORTAL_URL + html.unescape(href))
        hit["docket_sheet_url"] = links.get("Docket Sheet")
        hits.append(hit)
    return hits


def fold(name: str) -> list[str]:
    """Stop-word-free tokens with caption abbreviations spelled out:
    'Lake Meade Propertyowners Assoc., Inc.' -> LAKE MEADE PROPERTY OWNERS
    ASSOCIATION. Truncated tails ('ASSOCI') fold too."""
    raw = re.sub(r"\b([A-Za-z])\.([A-Za-z])\.([A-Za-z])\b\.?", r"\1\2\3", name or "")  # H.O.A.
    raw = re.sub(r"\bhome owner", "homeowner", raw.replace("'", "").replace("’", ""), flags=re.I)
    raw = re.sub(r"\bass ociation\b", "association", raw, flags=re.I)          # an IRS field break
    out: list[str] = []
    for token in normalize(raw).split():
        if token.startswith("ASSOC") and not token.startswith("ASSOCIATE"):
            token = "ASSOCIATION"
        out += ABBREVIATIONS.get(token, token).split()
    return out


def split_generic(tokens: list[str]) -> tuple[list[str], set[str]]:
    """(the distinguishing words, the generic ones) of a folded name."""
    distinguishing = [token for token in tokens if token not in GENERIC_WORDS]
    return distinguishing, {token for token in tokens if token in GENERIC_WORDS}


def party_matches(party: str, name: str) -> bool:
    """Is this caption party the association that was queried? Equal once
    folded, or the same distinguishing words with a compatible kind:
    'Hemlock Farms Comm Assoc' and the truncated 'Hemlock Farms Community'
    are Hemlock Farms Community Association; 'Lakeview Homeowners Assoc' is
    not Lakeview Condominium Association, a bare 'Hemlock Farms' is not an
    association, and 'Southpointe II Property Owners Assoc' is a different body."""
    party_tokens, name_tokens = fold(party), fold(name)
    if not party_tokens or not name_tokens:
        return False
    if party_tokens == name_tokens:
        return True
    if has_business_form(party):
        return False
    (party_core, party_generic), (name_core, name_generic) = split_generic(party_tokens), split_generic(name_tokens)
    return (bool(party_core) and party_core == name_core and bool(party_generic) and bool(name_generic)
            and (party_generic <= name_generic or name_generic <= party_generic))


def query_name(name: str) -> str:
    """The LIKE pattern for one association: the words before its first
    generic word plus that word's stem — 'Lake Meade Property Owners
    Association, Inc.' -> 'Lake Meade Prop%'. A name that opens with a
    generic word, has none, or carries distinguishing words after them
    ('Rental Property Owners Association of Lebanon County': 'Rental Prop%'
    would be half the docket) is searched whole, minus Inc/LLC."""
    full = ENTITY_SUFFIX_RE.sub("", clean(name)).strip(" ,.") or clean(name)
    words = full.split()
    generic = [bool(folded) and folded[0] in GENERIC_WORDS for folded in (fold(word) for word in words)]
    for i, word in enumerate(words[:-1]):                 # "Home Owners" is one generic word
        if word.upper() == "HOME" and words[i + 1].upper().startswith("OWNER"):
            generic[i] = True
    first = generic.index(True) if True in generic else 0
    if first == 0 or any(fold(word) and not is_generic for word, is_generic in zip(words[first:], generic[first:],
                                                                                   strict=False)):
        return full + "%"
    word = words[first].strip(",.")
    if word.upper() in ACRONYMS:                        # 'Elk Manor Estates H%' reaches HOA and Homeowners alike
        stem = word[0] if first > 1 else word
    else:
        stem = STEMS.get(fold(word)[0], word)
    return " ".join(words[:first] + [stem]) + "%"


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.session = http.session(BROWSER_USER_AGENT)
        self.token = ""

    def antiforgery_token(self) -> str:
        if self.token:
            return self.token
        response = self.session.get(SEARCH_URL, timeout=60)
        response.raise_for_status()
        match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', response.text)
        if not match:
            raise RuntimeError("no __RequestVerificationToken on /CaseSearch")
        self.token = match.group(1)
        return self.token

    def post_search(self, pattern: str) -> list[dict]:
        """One organization-name search; the antiforgery pair is refreshed once on 400/403."""
        data = dict.fromkeys(SEARCH_FORM_FIELDS, "")
        data.update(SearchBy="Organization", OrganizationName=pattern, FiledStartDate=FIRST_DAY,
                    FiledEndDate=dt.date.today().isoformat(), __RequestVerificationToken=self.antiforgery_token())
        headers = {"Referer": SEARCH_URL, "Origin": PORTAL_URL}
        response = self.session.post(SEARCH_URL, data=data, timeout=180, headers=headers)
        if response.status_code == 429:
            try:
                retry_after = int(response.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
            raise RateLimited(retry_after or 900)
        if response.status_code in (400, 403):
            self.token = ""
            data["__RequestVerificationToken"] = self.antiforgery_token()
            response = self.session.post(SEARCH_URL, data=data, timeout=180, headers=headers)
        response.raise_for_status()
        return parse_results(response.text)

    def search(self, name: str) -> list[dict]:
        out = []
        for hit in self.post_search(query_name(name)):
            caption = hit["caption"]
            parties = [side.strip() for side in re.split(r"\s+v\.?\s+", caption, maxsplit=1, flags=re.I)]
            matched = [party for party in parties if party_matches(ET_AL_RE.sub("", party), name)]
            if not matched:
                continue
            role = "plaintiff" if party_matches(ET_AL_RE.sub("", parties[0]), name) else "defendant"
            kind = DOCKET_KINDS.get((hit["docket_number"].split("-") + ["", "", ""])[2], "")
            out.append(record(
                key=KEY, state=STATE, case_name=caption,
                court=" ".join(part for part in (f"{hit['county']} County" if hit["county"] else "",
                                                 hit["court_type"], hit["court_office"]) if part),
                docket_number=hit["docket_number"], date_filed=hit["filing_date"],
                nature_of_suit=" — ".join(part for part in (hit["court_type"], kind) if part),
                status=hit["case_status"], associations=[name],
                url=hit.get("docket_sheet_url") or SEARCH_URL, case_id=hit["docket_number"], queried=name,
                association_role=[role], parties=hit["primary_participants"]).as_row())
        return out
