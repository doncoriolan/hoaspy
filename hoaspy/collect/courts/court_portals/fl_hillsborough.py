"""Hillsborough County, FL Clerk — HOVER case search by business name.

    https://hover.hillsclerk.com/

13th Judicial Circuit, all divisions. An anonymous JSON API: one POST to
HoverApiAccount/LogAnonymous mints a requestor guid, then POST Case/Search
(SearchType=ByBusiness, a DataTables body) returns case number, UCN,
caption, filing date, type and category, status, division and judge.
PerimeterX fronts the site but does not block the search endpoint (it
does refuse the per-case summary call, which is not needed). Hard cap: 500
rows a query, and a start offset returns nothing, so a busy name is split
by filing-date window until every window is under the cap. No per-case
public deep link (case pages read the id from sessionStorage), so `url`
is the case-number search tab and `docket_number` re-enters the case.

Best-effort by construction: the portal matches on the leading words of
the party name, so the query is the distinctive core of the DBPR name and
only captions that carry it are kept.
"""

from __future__ import annotations

import datetime as dt
import re
import time

from hoaspy.lib import http
from hoaspy.lib.http import BROWSER_USER_AGENT

from ._common import clean, normalize, party_is_association, record
from .fl_broward import ET_AL_RE, query_name

STATE = "FL"
KEY = "fl_hillsborough"
NEEDS_COOKIE = False
COUNTIES = {"HILLSBOROUGH"}
PORTAL_URL = "https://hover.hillsclerk.com/"
SEARCH_PAGE = PORTAL_URL + "html/case/caseSearch.html"
INFO = {
    "name": "Hillsborough County Clerk — HOVER case search (business name)",
    "url": SEARCH_PAGE,
    "access": "anonymous JSON API (LogAnonymous guid), no captcha; per-association business-name search",
    "coverage": "Hillsborough County / 13th Circuit, all divisions, filings from 1976",
    "caveat": "best-effort — leading-word name match on the DBPR core name; 500-row cap "
              "per query (date-window split); no per-case public deep link",
}
RESULT_COLUMNS = ["caseID", "caseID", "caseNumber", "citationNumber", "caseStyle", "caseStatus",
                  "caseFiledOn", "caseTypeDescription"]
ROW_CAP = 500
FIRST_DAY = dt.date(1976, 1, 1)
MAX_SPLIT_DEPTH = 8


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.session = http.session(BROWSER_USER_AGENT, headers={
            "Origin": "https://hover.hillsclerk.com", "Referer": PORTAL_URL + "html/case/searchResults.html"})
        self.guid = ""

    def requestor_guid(self) -> str:
        if not self.guid:
            response = self.session.post(PORTAL_URL + "HoverApiAccount/LogAnonymous", timeout=30)
            response.raise_for_status()
            self.guid = response.json()["requestorGuid"]
        return self.guid

    def query(self, name: str, first: dt.date, last: dt.date) -> tuple[list[dict], int]:
        """One business-name search filed in [first, last]: (rows, the server's total)."""
        columns = [{"data": column, "name": "", "searchable": True, "orderable": False,
                    "search": {"value": "", "regex": False, "fixed": []}} for column in RESULT_COLUMNS]
        body = {"UserName": None, "UserBarNumber": "", "UserPartyID": "", "SearchType": "ByBusiness",
                "ExtendedSearch": False, "CaseNumber": "", "CaseID": 0, "CrossReferenceNumber": "",
                "BarNumber": "", "PartyID": "", "LastName": name, "FirstName": "", "MiddleName": "",
                "Telephone": "", "DateOfBirth": "", "AttorneyBarNumber": "", "UseSoundex": False,
                "CitationNumber": "", "DLNumber": "", "CaseStatus": "A",
                "CaseCategory": "", "CaseType": "",
                "DateFiledFrom": first.strftime("%m/%d/%Y"), "DateFiledTo": last.strftime("%m/%d/%Y"),
                "ErrorFound": False, "RequestorToken": "", "RequestorGuid": self.requestor_guid(), "CaptchaHash": "",
                "SendParameters": {"draw": 1, "columns": columns,
                                   "order": [{"column": 0, "dir": "asc", "name": ""}],
                                   "start": 0, "length": ROW_CAP,
                                   "search": {"value": "", "regex": False, "fixed": []}},
                "RequestorUserName": "Anonymous"}
        response = self.session.post(PORTAL_URL + "Case/Search", json=body, timeout=120)
        if response.status_code == 403:
            raise PermissionError("HOVER 403 (PerimeterX) — pause and rerun later")
        response.raise_for_status()
        answer = response.json()
        if answer.get("error"):
            raise RuntimeError("HOVER error: " + str(answer["error"])[:160])
        return answer.get("data", []), int(answer.get("recordsFiltered") or len(answer.get("data", [])))

    def result_rows(self, name: str, first: dt.date, last: dt.date, depth: int = 0) -> list[dict]:
        """Every row for `name`, the window halved wherever the cap is hit."""
        time.sleep(self.pace)
        rows, total = self.query(name, first, last)
        if total < ROW_CAP or depth > MAX_SPLIT_DEPTH or (last - first).days < 2:
            return rows
        middle = first + (last - first) / 2
        return (self.result_rows(name, first, middle, depth + 1)
                + self.result_rows(name, middle + dt.timedelta(days=1), last, depth + 1))

    def search(self, name: str) -> list[dict]:
        core = query_name(name)
        core_key = normalize(core)
        rows = self.result_rows(core, FIRST_DAY, dt.date.today())
        out, seen = [], set()
        for hit in rows:
            case_number = hit.get("caseNumber") or ""
            if not case_number or case_number in seen:
                continue
            style = clean(hit.get("caseStyle") or "")
            sides = [side.strip() for side in re.split(r"\s+vs?\.?\s+", style, maxsplit=1, flags=re.I)]
            matched = [side for side in sides if core_key and party_is_association(side, core)]
            if not matched:
                continue
            seen.add(case_number)
            role = "plaintiff" if core_key in normalize(sides[0]) else "defendant"
            out.append(record(
                key=KEY, state=STATE, case_name=style,
                court=" ".join(part for part in ("Hillsborough County", hit.get("caseDivision") or "") if part),
                docket_number=case_number, date_filed=(hit.get("caseFiledOn") or "")[:10],
                nature_of_suit=" — ".join(part for part in (hit.get("caseCategoryDescription") or "",
                                                            hit.get("caseTypeDescription") or "") if part),
                status=hit.get("caseStatus") or "",
                associations=[ET_AL_RE.sub("", party) for party in matched],
                url=SEARCH_PAGE + "#nav-CaseNumber-tab",
                case_id=hit.get("crossReferenceNumber") or case_number, queried=name,
                association_role=[role], ucn=hit.get("crossReferenceNumber") or "").as_row())
        return out
