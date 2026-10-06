"""Alaska Court System — CourtView public access, company-name search.

    https://records.courts.alaska.gov/

Statewide trial courts (district and superior; civil, small claims …),
reliable from 1990 on. Anonymous: no login, captcha or terms click; the
only state is the JSESSIONID and Wicket's per-session encrypted `?x=` URLs,
which are read off each previous response (home -> "Search Cases" ->
"Name" tab -> form). Company search is STARTS-WITH on the name as filed;
results are one row per party appearance, 500 cases at most per search,
paged through "Go to page N" links.

Best-effort by construction: a prefix match on the registered spelling.
No stable public deep link exists (detail URLs are session-bound), so
`url` is the portal root and `docket_number` is the durable locator.
"""

from __future__ import annotations

import html
import re
import time

from hoaspy.lib import http
from hoaspy.lib.http import BROWSER_USER_AGENT

from ._common import clean, name_matches, record

STATE = "AK"
KEY = "ak_courtview"
NEEDS_COOKIE = False
PORTAL_URL = "https://records.courts.alaska.gov"
INFO = {
    "name": "Alaska Court System — CourtView Public Access (company-name search)",
    "url": PORTAL_URL + "/",
    "access": "anonymous, no captcha; per-association company-name search",
    "coverage": "all Alaska trial courts statewide, reliable from 1990",
    "caveat": "best-effort — starts-with name match; 500-case cap per search; "
              "no per-case public deep link (search the case number on the portal)",
}
MAX_PAGES = 20
ROW_RE = re.compile(r'<tr class="row(?:even|odd)">(.*?)</tr>', re.S)
CELL_RE = re.compile(r'<td id="grid~row-\d+~cell-(\d+)"[^>]*>(.*?)</td>', re.S)
ENTITY_SUFFIX_RE = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
# The result grid's columns, by cell index.
CASE_NUMBER, CASE_TYPE, FILE_DATE, PARTY, PARTY_TYPE, STATUS = 3, 4, 5, 6, 7, 9


def page_text(page_html: str) -> str:
    without_scripts = re.sub(r"<script.*?</script>", "", page_html, flags=re.S)
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", without_scripts)))


def absolute_url(page_url: str, href: str) -> str:
    href = html.unescape(href)
    return href if href.startswith("http") else page_url.split("?")[0] + href


def parse_results(page_html: str, page_url: str) -> tuple[list[dict], str | None]:
    """(the rows of one result page, the URL of the next page or None)."""
    hits = []
    for row_html in ROW_RE.findall(page_html):
        cells = {int(index): cell for index, cell in CELL_RE.findall(row_html)}
        if not cells:
            continue
        text = {index: page_text(cell) for index, cell in cells.items()}
        case_type = text.get(CASE_TYPE, "")
        location = re.search(r"\((\w+)\)\s*$", case_type)
        hits.append({"case_number": text.get(CASE_NUMBER, ""), "case_type": case_type,
                     "court_loc": location.group(1) if location else "", "file_date": text.get(FILE_DATE, ""),
                     "party": text.get(PARTY, ""), "party_type": text.get(PARTY_TYPE, ""),
                     "status": text.get(STATUS, "")})
    current = re.search(r'<span id="\w+" title="Go to page (\d+)">', page_html)
    current = int(current.group(1)) if current else 1
    links = re.findall(r'<a href="([^"]+)"[^>]*title="Go to page (\d+)"', page_html)
    pages = {int(number): href for href, number in links}
    following = pages.get(current + 1)
    return hits, (absolute_url(page_url, following) if following else None)


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = max(pace, 1.0)
        self.session = http.session(BROWSER_USER_AGENT)

    def open_form(self) -> tuple[str, str]:
        """home -> (BrowserInfo postback) -> Search Cases -> Name tab; (form id, action URL)."""
        response = self.session.get(PORTAL_URL + "/", timeout=30)
        response.raise_for_status()
        postback = re.search(r'<form name="postback"[^>]*action="([^"]+)"', response.text)
        if postback:
            hidden = re.search(r'name="(id\w+_hf_0)"', response.text).group(1)
            data = {hidden: "", "navigatorAppName": "Netscape", "navigatorAppVersion": "5.0 (Macintosh)",
                    "navigatorAppCodeName": "Mozilla", "navigatorCookieEnabled": "true",
                    "navigatorJavaEnabled": "false", "navigatorLanguage": "en-US",
                    "navigatorPlatform": "MacIntel", "navigatorUserAgent": BROWSER_USER_AGENT,
                    "screenWidth": "1512", "screenHeight": "982", "screenColorDepth": "24",
                    "utcOffset": "-9", "utcDSTOffset": "-8", "browserWidth": "1400",
                    "browserHeight": "900", "hostname": "records.courts.alaska.gov"}
            response = self.session.post(absolute_url(response.url, postback.group(1)), data=data, timeout=30)
            response.raise_for_status()
        begin = re.search(r"wicketSubmitFormById\('(id\w+)', '([^']+)', 'linkFrag:beginButton'", response.text)
        if not begin:
            raise RuntimeError("Search Cases link not found: " + page_text(response.text)[:160])
        time.sleep(self.pace)
        response = self.session.post(absolute_url(response.url, begin.group(2)),
                                     data={begin.group(1) + "_hf_0": "", "linkFrag:beginButton": "1"}, timeout=30)
        response.raise_for_status()
        tab = re.search(r'<a href="([^"]+)"[^>]*><span>Name</span>', response.text)
        if not tab:
            raise RuntimeError("Name tab not found: " + page_text(response.text)[:160])
        time.sleep(self.pace)
        response = self.session.get(absolute_url(response.url, tab.group(1)), timeout=30)
        response.raise_for_status()
        form = re.search(r'<form id="(id\w+)" method="post" action="([^"]+)"', response.text)
        if not form or "companyName" not in response.text:
            raise RuntimeError("company-name form not found: " + page_text(response.text)[:160])
        return form.group(1), absolute_url(response.url, form.group(2))

    def result_rows(self, name: str, max_pages: int = MAX_PAGES) -> list[dict]:
        """Every result row of a company-name search, paged."""
        form_id, action = self.open_form()
        query = ENTITY_SUFFIX_RE.sub("", clean(name)).strip(" ,.") or clean(name)
        data = [(form_id + "_hf_0", ""), ("lastName", ""), ("firstName", ""), ("middleName", ""),
                ("sffxCd", ""), ("companyName", query), ("statCd", " "), ("ptyCd", " "),
                ("dobDateRange:dateInputBegin", ""), ("dobDateRange:dateInputEnd", ""),
                ("dodDateRange:dateInputBegin", ""), ("dodDateRange:dateInputEnd", ""),
                ("fileDateRange:dateInputBegin", ""), ("fileDateRange:dateInputEnd", ""),
                ("submitLink", "Search"), ("caseCd", " ")]
        time.sleep(self.pace)
        response = self.session.post(action, data=data, timeout=60)
        response.raise_for_status()
        hits, following = parse_results(response.text, response.url)
        pages = 1
        while following and pages < max_pages:
            time.sleep(self.pace)
            response = self.session.get(following, timeout=60)
            response.raise_for_status()
            more, following = parse_results(response.text, response.url)
            pages += 1
            if not more:
                break
            hits.extend(more)
        return hits

    def search(self, name: str) -> list[dict]:
        by_case: dict[str, dict] = {}
        for hit in self.result_rows(name):
            if not name_matches(hit["party"], name):
                continue
            case = by_case.setdefault(hit["case_number"], dict(hit, roles=set(), parties=set()))
            case["roles"].add(hit["party_type"].lower() or "party")
            case["parties"].add(hit["party"])
        out = []
        for case_number, case in by_case.items():
            party = sorted(case["parties"])[0]
            out.append(record(
                key=KEY, state=STATE,
                case_name=f"{party} ({'/'.join(sorted(case['roles']))}) — {case['case_type']}",
                court=case["case_type"], docket_number=case_number, date_filed=case["file_date"],
                nature_of_suit=re.sub(r"\s*\(\w+\)\s*$", "", case["case_type"]),
                status=case["status"], associations=sorted(case["parties"]),
                url=PORTAL_URL + "/", case_id=case_number, queried=name,
                association_role=sorted(case["roles"])).as_row())
        return out
