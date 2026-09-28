"""Pennsylvania Unified Judicial System web portal — case search by
organization name.

    https://ujsportal.pacourts.us/CaseSearch

Anonymous, no captcha; an ASP.NET Core antiforgery token from one GET per
session is the only state. Organization search is SQL-LIKE (we append '%'
after stripping Inc/LLC) and must carry one narrowing field — a 1900-to-today
filing-date range returns every docket type in one document (no server
paging, no cap observed). Recon 2026-09-02 (scratchpad courts_recon/PA_ujs.md).

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
from ._common import name_matches, record, clean

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
    "caveat": "best-effort — prefix name match on our registered spelling; MDJ tier only",
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


def query_name(name: str) -> str:
    q = _SUFFIX.sub("", clean(name)).strip(" ,.")
    return (q or clean(name)) + "%"


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
            matched = [p for p in parties if name_matches(re.sub(r",?\s*et al\.?$", "", p, flags=re.I), name)]
            if not matched:
                continue
            role = "plaintiff" if name_matches(re.sub(r",?\s*et al\.?$", "", parties[0], flags=re.I), name) else "defendant"
            kind = _DOCKET_KIND.get((h["docket_number"].split("-") + ["", "", ""])[2], "")
            rec = record(
                key=KEY, state=STATE, case_name=caption,
                court=" ".join(x for x in (f"{h['county']} County" if h["county"] else "",
                                            h["court_type"], h["court_office"]) if x),
                docket_number=h["docket_number"], date_filed=h["filing_date"],
                nature_of_suit=" — ".join(x for x in (h["court_type"], kind) if x),
                status=h["case_status"],
                associations=[re.sub(r",?\s*et al\.?$", "", m, flags=re.I) for m in matched],
                url=h.get("docket_sheet_url") or f"{BASE}/CaseSearch",
                case_id=h["docket_number"], queried=name)
            rec["association_role"] = [role]
            rec["parties"] = h["primary_participants"]
            out.append(rec)
        return out
