"""Pennsylvania Unified Judicial System web portal — case search by
organization name.

    https://ujsportal.pacourts.us/CaseSearch

Anonymous, no captcha; an ASP.NET Core antiforgery token from one GET per
session is the only state. Organization search is SQL-LIKE and must carry
one narrowing field — a 1900-to-today filing-date range returns every docket
type in one document (no server paging, no cap observed). Recon 2026-09-02
(scratchpad courts_recon/PA_ujs.md).

Captions abbreviate: "Hemlock Farms Community Association" is docketed as
"Hemlock Farms Community Assoc." (331 cases), "Hemlock Farms Comm Assoc"
(36) and spelled out in only 9 of 411. So the query is the name's leading
words plus the stem of its first association word ("Hemlock Farms Comm%",
see query_name), and party_matches() folds the abbreviations before
comparing instead of looking for the registered spelling verbatim. A record's
`associations` is therefore the name we queried, not the caption's spelling
(that stays in `case_name`): build_site would make "Hemlock Farms Comm
Assoc" a second community.

Coverage caveat: association hits are Magisterial District Judge dockets
(MJ-*-CV/LT-*). Common Pleas *civil* dockets are not on this portal — they
live in each county prothonotary's system — so this is the small-claims /
landlord-tenant tier of HOA litigation, not the whole of it.
"""
from __future__ import annotations

import datetime as _dt
import html
import re

import requests

from . import RateLimited
from ._common import has_business_form, normalize, record, clean

STATE = "PA"
KEY = "pa_ujs"
NEEDS_COOKIE = False
BASE = "https://ujsportal.pacourts.us"
INFO = {
    "name": "Pennsylvania UJS Web Portal — organization-name case search",
    "url": f"{BASE}/CaseSearch",
    "access": "anonymous, no captcha; per-association organization-name search",
    "coverage": "Magisterial District Judge dockets statewide (civil + landlord/tenant); "
                "Common Pleas civil dockets are NOT on this portal",
    "caveat": "best-effort — prefix search on the name's leading words, caption "
              "abbreviations folded; MDJ tier only",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
FIELDS = ("SearchBy ParticipantSID ParticipantSSN FiledStartDate FiledEndDate County "
          "JudicialDistrict MDJSCourtOffice DocketType CaseCategory CaseStatus "
          "DriversLicenseState PADriversLicenseNumber ArrestingAgency ORI JudgeNameID "
          "AppellateCourtName AppellateDistrict AppellateDocketType AppellateCaseCategory "
          "AppellateCaseType AppellateAgency AppellateTrialCourt AppellateTrialCourtJudge "
          "AppellateCaseStatus OrganizationName ParticipantRole ParcelState ParcelCounty "
          "ParcelMunicipality CourtOffice CourtRoomID CalendarEventType").split()
COLS = ["_hidden0", "_hidden1", "docket_number", "court_type", "caption", "case_status",
        "filing_date", "primary_participants", "dob", "county", "court_office", "otn",
        "complaint_no", "incident_no", "event_type", "event_status", "event_date",
        "event_location", "_icons"]
_SUFFIX = re.compile(r"[,\s]+(inc\.?|incorporated|llc|corp\.?|corporation|ltd\.?)\s*$", re.I)
_DOCKET_KIND = {"CV": "Civil", "LT": "Landlord/Tenant", "CR": "Criminal", "NT": "Non-Traffic",
                "TR": "Traffic", "MD": "Miscellaneous"}


def _cell(c: str) -> str:
    c = re.sub(r"<br\s*/?>", "; ", c)
    return clean(html.unescape(re.sub(r"<[^>]+>", "", c)))


def _parse(body: str) -> list[dict]:
    grid = re.search(r'<table[^>]*id="caseSearchResultGrid".*?</table>', body, re.S)
    if not grid:
        raise RuntimeError("no caseSearchResultGrid in response (validation failure?)")
    tb = re.search(r"<tbody[^>]*>(.*?)</tbody>", grid.group(0), re.S)
    hits = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb.group(1) if tb else "", re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < len(COLS):
            continue
        rec = {k: _cell(v) for k, v in zip(COLS, tds) if not k.startswith("_")}
        links = {}
        for label, href in re.findall(r'aria-label="([^"]+)" href="([^"]+)"', tds[-1]):
            links.setdefault(label, BASE + html.unescape(href))
        rec["docket_sheet_url"] = links.get("Docket Sheet")
        hits.append(rec)
    return hits


# Caption spellings of the association vocabulary, folded to one form.
_ABBREV = {"ASSN": "ASSOCIATION", "ASSO": "ASSOCIATION", "COMM": "COMMUNITY",
           "CMNTY": "COMMUNITY", "CONDO": "CONDOMINIUM", "CONDOS": "CONDOMINIUM",
           "CONDOMINIUMS": "CONDOMINIUM", "HOMEOWNER": "HOMEOWNERS", "PROP": "PROPERTY",
           "OWNER": "OWNERS", "RESIDENT": "RESIDENTS", "TOWNHOMES": "TOWNHOME",
           "TOWNHOUSES": "TOWNHOUSE", "PROPERTYOWNERS": "PROPERTY OWNERS",
           "HOA": "HOMEOWNERS ASSOCIATION", "POA": "PROPERTY OWNERS ASSOCIATION",
           "COA": "CONDOMINIUM ASSOCIATION"}
# Words that say what kind of body it is, not which one.
_GENERIC = {"ASSOCIATION", "COMMUNITY", "CONDOMINIUM", "HOMEOWNERS", "PROPERTY", "OWNERS",
            "RESIDENTS", "TOWNHOME", "TOWNHOUSE", "MASTER", "UNIT", "LOT", "PLANNED"}
# What to search for in place of the first generic word: short enough to
# reach its abbreviations ("Comm" finds Community, Comm and Comm.Assoc.).
_STEM = {"ASSOCIATION": "Ass", "COMMUNITY": "Comm", "CONDOMINIUM": "Condo",
         "HOMEOWNERS": "Home", "PROPERTY": "Prop", "OWNERS": "Owner", "RESIDENTS": "Resident"}


def _fold(name: str) -> list[str]:
    """Stop-word-free tokens with caption abbreviations spelled out:
    'Lake Meade Propertyowners Assoc., Inc.' -> LAKE MEADE PROPERTY OWNERS
    ASSOCIATION. Truncated tails ('ASSOCI') fold too."""
    raw = re.sub(r"\b([A-Za-z])\.([A-Za-z])\.([A-Za-z])\b\.?", r"\1\2\3", name or "")  # H.O.A.
    raw = re.sub(r"\bhome owner", "homeowner", raw.replace("'", "").replace("’", ""), flags=re.I)
    raw = re.sub(r"\bass ociation\b", "association", raw, flags=re.I)          # IRS field break
    out: list[str] = []
    for t in normalize(raw).split():
        if t.startswith("ASSOC") and not t.startswith("ASSOCIATE"):
            t = "ASSOCIATION"
        out += _ABBREV.get(t, t).split()
    return out


def _core(tokens: list[str]) -> tuple[list[str], set[str]]:
    return [t for t in tokens if t not in _GENERIC], {t for t in tokens if t in _GENERIC}


def party_matches(party: str, name: str) -> bool:
    """Is this caption party the association we queried? Equal once folded,
    or the same distinguishing words with a compatible kind — 'Hemlock Farms
    Comm Assoc' and the truncated 'Hemlock Farms Community' are Hemlock
    Farms Community Association; 'Lakeview Homeowners Assoc' is not Lakeview
    Condominium Association, a bare 'Hemlock Farms' is not an association,
    and 'Southpointe II Property Owners Assoc' is a different body."""
    pt, qt = _fold(party), _fold(name)
    if not pt or not qt:
        return False
    if pt == qt:
        return True
    if has_business_form(party):
        return False
    (pc, pg), (qc, qg) = _core(pt), _core(qt)
    return bool(pc) and pc == qc and bool(pg) and bool(qg) and (pg <= qg or qg <= pg)


def query_name(name: str) -> str:
    """LIKE pattern for one association: the words before its first generic
    word plus that word's stem — 'Lake Meade Property Owners Association,
    Inc.' -> 'Lake Meade Prop%'. A name that opens with a generic word, has
    none, or carries distinguishing words after them ('Rental Property
    Owners Association of Lebanon County' — 'Rental Prop%' would be half
    the docket) is searched whole, minus Inc/LLC."""
    full = _SUFFIX.sub("", clean(name)).strip(" ,.") or clean(name)
    words = full.split()
    kinds = [bool(f) and f[0] in _GENERIC for f in (_fold(w) for w in words)]
    for i, w in enumerate(words[:-1]):                 # "Home Owners" is one generic word
        if w.upper() == "HOME" and words[i + 1].upper().startswith("OWNER"):
            kinds[i] = True
    first = kinds.index(True) if True in kinds else 0
    if first == 0 or any(_fold(w) and not k for w, k in zip(words[first:], kinds[first:])):
        return full + "%"
    word = words[first].strip(",.")
    if word.upper() in ("HOA", "POA", "COA"):          # 'Elk Manor Estates H%' reaches
        stem = word[0] if first > 1 else word          # HOA and Homeowners alike
    else:
        stem = _STEM.get(_fold(word)[0], word)
    return " ".join(words[:first] + [stem]) + "%"


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.token = ""

    def _token(self) -> str:
        if self.token:
            return self.token
        r = self.s.get(f"{BASE}/CaseSearch", timeout=60)
        r.raise_for_status()
        m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("no __RequestVerificationToken on /CaseSearch")
        self.token = m.group(1)
        return self.token

    def _post(self, q: str) -> list[dict]:
        data = {f: "" for f in FIELDS}
        data.update(SearchBy="Organization", OrganizationName=q,
                    FiledStartDate="1900-01-01", FiledEndDate=_dt.date.today().isoformat(),
                    __RequestVerificationToken=self._token())
        r = self.s.post(f"{BASE}/CaseSearch", data=data, timeout=180,
                        headers={"Referer": f"{BASE}/CaseSearch", "Origin": BASE})
        if r.status_code == 429:
            try:
                ra = int(r.headers.get("Retry-After", "0"))
            except ValueError:
                ra = 0
            raise RateLimited(ra or 900)
        if r.status_code in (400, 403):
            self.token = ""                      # antiforgery pair rotated — refresh once
            data["__RequestVerificationToken"] = self._token()
            r = self.s.post(f"{BASE}/CaseSearch", data=data, timeout=180,
                            headers={"Referer": f"{BASE}/CaseSearch", "Origin": BASE})
        r.raise_for_status()
        return _parse(r.text)

    def search(self, name: str) -> list[dict]:
        out = []
        for h in self._post(query_name(name)):
            caption = h["caption"]
            sides = re.split(r"\s+v\.?\s+", caption, maxsplit=1, flags=re.I)
            parties = [p.strip() for p in sides]
            matched = [p for p in parties if party_matches(re.sub(r",?\s*et al\.?$", "", p, flags=re.I), name)]
            if not matched:
                continue
            role = "plaintiff" if party_matches(re.sub(r",?\s*et al\.?$", "", parties[0], flags=re.I), name) else "defendant"
            kind = _DOCKET_KIND.get((h["docket_number"].split("-") + ["", "", ""])[2], "")
            rec = record(
                key=KEY, state=STATE, case_name=caption,
                court=" ".join(x for x in (f"{h['county']} County" if h["county"] else "",
                                            h["court_type"], h["court_office"]) if x),
                docket_number=h["docket_number"], date_filed=h["filing_date"],
                nature_of_suit=" — ".join(x for x in (h["court_type"], kind) if x),
                status=h["case_status"],
                associations=[name],
                url=h.get("docket_sheet_url") or f"{BASE}/CaseSearch",
                case_id=h["docket_number"], queried=name)
            rec["association_role"] = [role]
            rec["parties"] = h["primary_participants"]
            out.append(rec)
        return out
