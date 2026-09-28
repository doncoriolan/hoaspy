"""Connecticut Judicial Branch — Civil/Family case look-up by party name.

    https://civilinquiry.jud.ct.gov/PartySearch.aspx

Statewide Superior Court civil + family + housing-session cases, all 16
Judicial Districts. Anonymous, no captcha; the only "anti-bot" is an
AjaxControlToolkit NoBot challenge (~n → post -n-1) that the server does not
even enforce. Party search returns 200 rows/page with no result cap; rows are
per party record, so a case naming the association twice appears twice.
Filing date, case type and disposition are not in the grid — they come from
the stateless deep link LoadDocket.aspx?DocketNo=… (one extra request per
case). Recon 2026-09-02 (scratchpad courts_recon/CT_civilinquiry.md).

Best-effort by construction: "Starts With" on our registered name, so a
caption spelled differently is missed (name-match recall, unmeasured).
"""
from __future__ import annotations

import re
import time
from html import unescape

import requests

from ._common import name_matches, record, clean

STATE = "CT"
KEY = "ct_civil"
NEEDS_COOKIE = False
BASE = "https://civilinquiry.jud.ct.gov/"
SEARCH_URL = BASE + "PartySearch.aspx"
INFO = {
    "name": "Connecticut Judicial Branch — Civil/Family Party Name Search",
    "url": SEARCH_URL,
    "access": "anonymous, no captcha; per-association party-name search + per-case detail page",
    "coverage": "CT Superior Court civil, family and housing sessions, statewide",
    "caveat": "best-effort — 'starts with' name match on our registered spelling; "
              "no result cap observed",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_TRAIL = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _hidden_fields(html: str) -> dict:
    return {m.group(1): unescape(m.group(2)) for m in re.finditer(
        r'<input type="hidden" name="([^"]+)"(?: id="[^"]*")? value="([^"]*)"', html)}


def _nobot_response(html: str) -> str:
    m = re.search(r'"ChallengeScript":"~(\d+)"', html)
    return str(~int(m.group(1))) if m else ""


def _clean_cell(cell: str) -> str:
    return clean(unescape(re.sub(r"<[^>]+>", " ", cell)).replace("\xa0", " "))


def _parse_rows(html: str) -> list[dict]:
    hits = []
    for row in re.findall(r'<tr class="grd(?:Row|RowAlt)">(.*?)</tr>', html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 6:
            continue
        hits.append({
            "party_name": _clean_cell(cells[0]), "case_name": _clean_cell(cells[1]),
            "docket_no": _clean_cell(cells[2]), "court_location": _clean_cell(cells[3]),
            "party_no": _clean_cell(cells[4]),
        })
    return hits


def _form_action(html: str) -> str:
    m = re.search(r'<form[^>]+action="([^"]+)"', html)
    return BASE + unescape(m.group(1)).lstrip("./") if m else SEARCH_URL


def query_name(name: str) -> str:
    """'FARMINGTON WOODS MASTER ASSOCIATION, INC.' -> 'FARMINGTON WOODS MASTER ASSOCIATION'
    so 'Starts With' tolerates the court's punctuation/suffix spelling."""
    q = _TRAIL.sub("", clean(name)).strip(" ,.")
    return q or clean(name)


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = max(pace, 1.0)
        self.s = _session()
        self.case_types: dict[str, str] = {}
        self.detail_cache: dict[str, dict] = {}

    def _get_form(self) -> str:
        r = self.s.get(SEARCH_URL, timeout=90)
        r.raise_for_status()
        if not self.case_types:
            sel = re.search(r'name="ctl00\$ContentPlaceHolder1\$ddlCaseType".*?</select>', r.text, re.S)
            if sel:
                for code, label in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', sel.group(0)):
                    self.case_types[code] = _clean_cell(label)
        return r.text

    def _search_rows(self, name: str, max_pages: int = 10) -> list[dict]:
        html = self._get_form()
        form = _hidden_fields(html)
        form["ctl00$NoBot$NoBot_NoBotExtender_ClientState"] = _nobot_response(html)
        form.update({
            "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
            "ctl00$ContentPlaceHolder1$txtLastName": query_name(name),
            "ctl00$ContentPlaceHolder1$txtFirstName": "",
            "ctl00$ContentPlaceHolder1$ddlLocation": "ALL",
            "ctl00$ContentPlaceHolder1$ddlCaseCategory": "ALL",
            "ctl00$ContentPlaceHolder1$ddlCaseType": "All",
            "ctl00$ContentPlaceHolder1$ddlSortOrder": "court_loc, party_name",
            "ctl00$ContentPlaceHolder1$rblLastNameSearchType": "Starts With",
            "ctl00$ContentPlaceHolder1$btnSubmit": "Search",
        })
        time.sleep(2.0)                       # the page's own minimum think-time
        r = self.s.post(SEARCH_URL, data=form, headers={"Referer": SEARCH_URL}, timeout=90)
        r.raise_for_status()
        html = r.text
        if "Not Found" in html and "grdRow" not in html:
            return []
        hits = _parse_rows(html)
        page = 1
        while page < max_pages and f"Page${page + 1}" in html:
            page += 1
            time.sleep(2.0)
            form = _hidden_fields(html)
            form["ctl00$NoBot$NoBot_NoBotExtender_ClientState"] = _nobot_response(html)
            form["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$gvPartyResults"
            form["__EVENTARGUMENT"] = f"Page${page}"
            r = self.s.post(_form_action(html), data=form, headers={"Referer": SEARCH_URL}, timeout=90)
            r.raise_for_status()
            html = r.text
            more = _parse_rows(html)
            if not more:
                break
            hits.extend(more)
        return hits

    def _detail(self, docket_no: str) -> dict:
        if docket_no in self.detail_cache:
            return self.detail_cache[docket_no]
        time.sleep(self.pace)
        r = self.s.get(BASE + "LoadDocket.aspx", params={"DocketNo": docket_no}, timeout=60)
        r.raise_for_status()
        spans = {m.group(1): _clean_cell(m.group(2)) for m in re.finditer(
            r'<span id="ctl00_ContentPlaceHolder1_([A-Za-z0-9_]+)"[^>]*>(.*?)</span>', r.text, re.S)}

        def after(key, label):
            v = spans.get(key, "")
            return v.split(label, 1)[1].strip() if label in v else v
        disp = spans.get("CaseDetailBasicInfo1_lblBasicDisposition", "")
        d = {
            "url": r.url,
            "case_type": spans.get("CaseDetailBasicInfo1_lblBasicCaseType", ""),
            "file_date": after("CaseDetailHeader1_lblFileDate", "File Date:"),
            "court_location": spans.get("CaseDetailBasicInfo1_lblBasicLocation", ""),
            "disposition_date": spans.get("CaseDetailBasicInfo1_lblBasicDispositionDate", ""),
            "disposition": "" if disp.endswith(":") else disp,
        }
        if d["disposition_date"].endswith(":"):
            d["disposition_date"] = ""
        self.detail_cache[docket_no] = d
        return d

    def search(self, name: str) -> list[dict]:
        rows = self._search_rows(name)
        by_docket: dict[str, dict] = {}
        for h in rows:
            if not name_matches(h["party_name"], name):
                continue
            cur = by_docket.setdefault(h["docket_no"], dict(h, roles=set()))
            cur["roles"].add("plaintiff" if h["party_no"].startswith("P") else "defendant")
        out = []
        for docket, h in by_docket.items():
            try:
                d = self._detail(docket)
            except Exception:
                d = {}
            code = d.get("case_type", "")
            nature = self.case_types.get(code, "") or code
            if code and nature != code:
                nature = f"{code} - {nature}" if not nature.startswith(code) else nature
            rec = record(
                key=KEY, state=STATE, case_name=h["case_name"],
                court=d.get("court_location") or h["court_location"],
                docket_number=docket, date_filed=d.get("file_date", ""),
                date_terminated=d.get("disposition_date", ""), nature_of_suit=nature,
                status=d.get("disposition") or ("Closed" if d.get("disposition_date") else "Open"),
                associations=[h["party_name"]],
                url=d.get("url") or f"{BASE}LoadDocket.aspx?DocketNo={docket}",
                case_id=docket, queried=name)
            rec["association_role"] = sorted(h["roles"])
            out.append(rec)
        return out
