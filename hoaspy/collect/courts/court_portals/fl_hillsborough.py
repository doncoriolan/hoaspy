"""Hillsborough County (FL) Clerk — HOVER case search by business name.

    https://hover.hillsclerk.com/

13th Judicial Circuit, all divisions. Anonymous JSON API: one
`POST HoverApiAccount/LogAnonymous` mints a requestorGuid, then
`POST Case/Search` (SearchType=ByBusiness, DataTables body) returns case
number, UCN, caption, filed date, type/category, status, division, judge.
PerimeterX fronts the site but did not block the search endpoint (it does
403 the per-case summary call, which we don't need). Hard cap: 500 rows per
query and `start>0` returns nothing, so a busy name is split by filing-date
windows until every window is under the cap. No per-case public deep link
(case pages read the id from sessionStorage), so `url` is the case-number
search tab and `docket_number` re-enters the case.
Recon 2026-09-02 (courts_recon/FL_odyssey_publicaccess.md).

Best-effort by construction: the portal matches on leading words of the
party name, so we query the distinctive core of the DBPR name and keep the
rows whose caption carries it.
"""
from __future__ import annotations

import datetime as _dt
import re
import time

import requests

from ._common import normalize, record, clean
from .fl_broward import query_name

STATE = "FL"
KEY = "fl_hillsborough"
NEEDS_COOKIE = False
COUNTIES = {"HILLSBOROUGH"}
HOVER = "https://hover.hillsclerk.com/"
INFO = {
    "name": "Hillsborough County Clerk — HOVER case search (business name)",
    "url": HOVER + "html/case/caseSearch.html",
    "access": "anonymous JSON API (LogAnonymous guid), no captcha; per-association business-name search",
    "coverage": "Hillsborough County / 13th Circuit, all divisions, filings from 1976",
    "caveat": "best-effort — leading-word name match on the DBPR core name; 500-row cap "
              "per query (date-window split); no per-case public deep link",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_COLS = ["caseID", "caseID", "caseNumber", "citationNumber", "caseStyle", "caseStatus",
         "caseFiledOn", "caseTypeDescription"]
CAP = 500


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Origin": "https://hover.hillsclerk.com",
                               "Referer": HOVER + "html/case/searchResults.html"})
        self.guid = ""

    def _guid(self) -> str:
        if not self.guid:
            r = self.s.post(HOVER + "HoverApiAccount/LogAnonymous", timeout=30)
            r.raise_for_status()
            self.guid = r.json()["requestorGuid"]
        return self.guid

    def _query(self, q: str, d0: _dt.date, d1: _dt.date) -> tuple[list[dict], int]:
        cols = [{"data": c, "name": "", "searchable": True, "orderable": False,
                 "search": {"value": "", "regex": False, "fixed": []}} for c in _COLS]
        body = {"UserName": None, "UserBarNumber": "", "UserPartyID": "", "SearchType": "ByBusiness",
                "ExtendedSearch": False, "CaseNumber": "", "CaseID": 0, "CrossReferenceNumber": "",
                "BarNumber": "", "PartyID": "", "LastName": q, "FirstName": "", "MiddleName": "",
                "Telephone": "", "DateOfBirth": "", "AttorneyBarNumber": "", "UseSoundex": False,
                "CitationNumber": "", "DLNumber": "", "CaseStatus": "A",
                "CaseCategory": "", "CaseType": "",
                "DateFiledFrom": d0.strftime("%m/%d/%Y"), "DateFiledTo": d1.strftime("%m/%d/%Y"),
                "ErrorFound": False, "RequestorToken": "", "RequestorGuid": self._guid(), "CaptchaHash": "",
                "SendParameters": {"draw": 1, "columns": cols,
                                   "order": [{"column": 0, "dir": "asc", "name": ""}],
                                   "start": 0, "length": CAP,
                                   "search": {"value": "", "regex": False, "fixed": []}},
                "RequestorUserName": "Anonymous"}
        r = self.s.post(HOVER + "Case/Search", json=body, timeout=120)
        if r.status_code == 403:
            raise PermissionError("HOVER 403 (PerimeterX) — pause and rerun later")
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError("HOVER error: " + str(j["error"])[:160])
        return j.get("data", []), int(j.get("recordsFiltered") or len(j.get("data", [])))

    def _rows(self, q: str, d0: _dt.date, d1: _dt.date, depth: int = 0) -> list[dict]:
        time.sleep(self.pace)
        rows, total = self._query(q, d0, d1)
        if total < CAP or depth > 8 or (d1 - d0).days < 2:
            return rows
        mid = d0 + (d1 - d0) / 2
        return self._rows(q, d0, mid, depth + 1) + self._rows(q, mid + _dt.timedelta(days=1), d1, depth + 1)

    def search(self, name: str) -> list[dict]:
        core = query_name(name)
        ncore = normalize(core)
        rows = self._rows(core, _dt.date(1976, 1, 1), _dt.date.today())
        out, seen = [], set()
        for d in rows:
            cn = d.get("caseNumber") or ""
            if not cn or cn in seen:
                continue
            style = clean(d.get("caseStyle") or "")
            sides = [s.strip() for s in re.split(r"\s+vs?\.?\s+", style, maxsplit=1, flags=re.I)]
            matched = [s for s in sides if ncore and ncore in normalize(s)]
            if not matched:
                continue
            seen.add(cn)
            role = "plaintiff" if ncore in normalize(sides[0]) else "defendant"
            status = d.get("caseStatus") or ""
            rec = record(
                key=KEY, state=STATE, case_name=style,
                court=" ".join(x for x in ("Hillsborough County", d.get("caseDivision") or "") if x),
                docket_number=cn, date_filed=(d.get("caseFiledOn") or "")[:10],
                nature_of_suit=" — ".join(x for x in (d.get("caseCategoryDescription") or "",
                                                       d.get("caseTypeDescription") or "") if x),
                status=status,
                associations=[re.sub(r",?\s*et al\.?$", "", m, flags=re.I) for m in matched],
                url=HOVER + "html/case/caseSearch.html#nav-CaseNumber-tab",
                case_id=d.get("crossReferenceNumber") or cn, queried=name)
            rec["association_role"] = [role]
            rec["ucn"] = d.get("crossReferenceNumber") or ""
            out.append(rec)
        return out
