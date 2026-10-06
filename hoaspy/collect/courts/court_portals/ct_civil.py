"""Connecticut Judicial Branch — civil and family case look-up by party name.

    https://civilinquiry.jud.ct.gov/PartySearch.aspx

Statewide Superior Court civil, family and housing-session cases, all 16
Judicial Districts. Anonymous, no captcha; the only anti-bot measure is an
AjaxControlToolkit NoBot challenge (~n -> post -n-1) that the server does
not enforce. A party search returns 200 rows a page with no result cap;
rows are per party record, so a case naming the association twice appears
twice. Filing date, case type and disposition are not in the grid — they
come from the stateless deep link LoadDocket.aspx?DocketNo=… (one extra
request per case).

Best-effort by construction: "Starts With" on the registered name, so a
caption spelled differently is missed.
"""

from __future__ import annotations

import re
import time
from html import unescape

from hoaspy.lib import http
from hoaspy.lib.http import BROWSER_USER_AGENT

from ._common import clean, name_matches, record

STATE = "CT"
KEY = "ct_civil"
NEEDS_COOKIE = False
PORTAL_URL = "https://civilinquiry.jud.ct.gov/"
SEARCH_URL = PORTAL_URL + "PartySearch.aspx"
DOCKET_URL = PORTAL_URL + "LoadDocket.aspx"
INFO = {
    "name": "Connecticut Judicial Branch — Civil/Family Party Name Search",
    "url": SEARCH_URL,
    "access": "anonymous, no captcha; per-association party-name search + per-case detail page",
    "coverage": "CT Superior Court civil, family and housing sessions, statewide",
    "caveat": "best-effort — 'starts with' name match on our registered spelling; "
              "no result cap observed",
}
THINK_TIME = 2.0                # the page's own minimum between posts
MAX_PAGES = 10
ENTITY_SUFFIX_RE = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)


def hidden_fields(page_html: str) -> dict:
    return {match.group(1): unescape(match.group(2)) for match in re.finditer(
        r'<input type="hidden" name="([^"]+)"(?: id="[^"]*")? value="([^"]*)"', page_html)}


def nobot_response(page_html: str) -> str:
    match = re.search(r'"ChallengeScript":"~(\d+)"', page_html)
    return str(~int(match.group(1))) if match else ""


def cell_text(cell: str) -> str:
    return clean(unescape(re.sub(r"<[^>]+>", " ", cell)).replace("\xa0", " "))


def parse_rows(page_html: str) -> list[dict]:
    hits = []
    for row_html in re.findall(r'<tr class="grd(?:Row|RowAlt)">(.*?)</tr>', page_html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) < 6:
            continue
        hits.append({"party_name": cell_text(cells[0]), "case_name": cell_text(cells[1]),
                     "docket_no": cell_text(cells[2]), "court_location": cell_text(cells[3]),
                     "party_no": cell_text(cells[4])})
    return hits


def form_action(page_html: str) -> str:
    match = re.search(r'<form[^>]+action="([^"]+)"', page_html)
    return PORTAL_URL + unescape(match.group(1)).lstrip("./") if match else SEARCH_URL


def query_name(name: str) -> str:
    """'FARMINGTON WOODS MASTER ASSOCIATION, INC.' -> 'FARMINGTON WOODS MASTER ASSOCIATION',
    so "Starts With" tolerates the court's punctuation and suffix spelling."""
    query = ENTITY_SUFFIX_RE.sub("", clean(name)).strip(" ,.")
    return query or clean(name)


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = max(pace, 1.0)
        self.session = http.session(BROWSER_USER_AGENT, headers={"Accept-Language": "en-US,en;q=0.9"})
        self.case_types: dict[str, str] = {}
        self.details: dict[str, dict] = {}

    def search_form(self) -> str:
        """The search page, with the case-type vocabulary read off it once."""
        response = self.session.get(SEARCH_URL, timeout=90)
        response.raise_for_status()
        if not self.case_types:
            select = re.search(r'name="ctl00\$ContentPlaceHolder1\$ddlCaseType".*?</select>', response.text, re.S)
            if select:
                for code, label in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', select.group(0)):
                    self.case_types[code] = cell_text(label)
        return response.text

    def result_rows(self, name: str, max_pages: int = MAX_PAGES) -> list[dict]:
        page_html = self.search_form()
        form = hidden_fields(page_html)
        form["ctl00$NoBot$NoBot_NoBotExtender_ClientState"] = nobot_response(page_html)
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
        time.sleep(THINK_TIME)
        response = self.session.post(SEARCH_URL, data=form, headers={"Referer": SEARCH_URL}, timeout=90)
        response.raise_for_status()
        page_html = response.text
        if "Not Found" in page_html and "grdRow" not in page_html:
            return []
        hits = parse_rows(page_html)
        page = 1
        while page < max_pages and f"Page${page + 1}" in page_html:
            page += 1
            time.sleep(THINK_TIME)
            form = hidden_fields(page_html)
            form["ctl00$NoBot$NoBot_NoBotExtender_ClientState"] = nobot_response(page_html)
            form["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$gvPartyResults"
            form["__EVENTARGUMENT"] = f"Page${page}"
            response = self.session.post(form_action(page_html), data=form, headers={"Referer": SEARCH_URL}, timeout=90)
            response.raise_for_status()
            page_html = response.text
            more = parse_rows(page_html)
            if not more:
                break
            hits.extend(more)
        return hits

    def detail(self, docket_no: str) -> dict:
        """Case type, dates and disposition from the docket page, cached per run."""
        if docket_no in self.details:
            return self.details[docket_no]
        time.sleep(self.pace)
        response = self.session.get(DOCKET_URL, params={"DocketNo": docket_no}, timeout=60)
        response.raise_for_status()
        spans = {match.group(1): cell_text(match.group(2)) for match in re.finditer(
            r'<span id="ctl00_ContentPlaceHolder1_([A-Za-z0-9_]+)"[^>]*>(.*?)</span>', response.text, re.S)}

        def after(key: str, label: str) -> str:
            value = spans.get(key, "")
            return value.split(label, 1)[1].strip() if label in value else value

        disposition = spans.get("CaseDetailBasicInfo1_lblBasicDisposition", "")
        detail = {
            "url": response.url,
            "case_type": spans.get("CaseDetailBasicInfo1_lblBasicCaseType", ""),
            "file_date": after("CaseDetailHeader1_lblFileDate", "File Date:"),
            "court_location": spans.get("CaseDetailBasicInfo1_lblBasicLocation", ""),
            "disposition_date": spans.get("CaseDetailBasicInfo1_lblBasicDispositionDate", ""),
            "disposition": "" if disposition.endswith(":") else disposition,
        }
        if detail["disposition_date"].endswith(":"):
            detail["disposition_date"] = ""
        self.details[docket_no] = detail
        return detail

    def search(self, name: str) -> list[dict]:
        by_docket: dict[str, dict] = {}
        for hit in self.result_rows(name):
            if not name_matches(hit["party_name"], name):
                continue
            case = by_docket.setdefault(hit["docket_no"], dict(hit, roles=set()))
            case["roles"].add("plaintiff" if hit["party_no"].startswith("P") else "defendant")
        out = []
        for docket, case in by_docket.items():
            try:
                detail = self.detail(docket)
            except Exception:
                detail = {}
            code = detail.get("case_type", "")
            nature = self.case_types.get(code, "") or code
            if code and nature != code:
                nature = f"{code} - {nature}" if not nature.startswith(code) else nature
            out.append(record(
                key=KEY, state=STATE, case_name=case["case_name"],
                court=detail.get("court_location") or case["court_location"],
                docket_number=docket, date_filed=detail.get("file_date", ""),
                date_terminated=detail.get("disposition_date", ""), nature_of_suit=nature,
                status=detail.get("disposition") or ("Closed" if detail.get("disposition_date") else "Open"),
                associations=[case["party_name"]],
                url=detail.get("url") or f"{DOCKET_URL}?DocketNo={docket}",
                case_id=docket, queried=name, association_role=sorted(case["roles"])).as_row())
        return out
