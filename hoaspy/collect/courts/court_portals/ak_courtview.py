"""Alaska Court System — CourtView public access, company-name search.

    https://records.courts.alaska.gov/

Statewide trial courts (district + superior; civil, small claims, …),
reliable from 1990 on. Anonymous: no login, captcha or terms click; the
only state is the JSESSIONID and Wicket's per-session encrypted `?x=` URLs,
which must be scraped from each previous response (home → "Search Cases"
→ "Name" tab → form). Company search is STARTS-WITH on the name as filed;
results are one row per party appearance, 500-case cap per search, paged
via "Go to page N" links. Recon 2026-09-02 (courts_recon/AK_courtview.md).

Best-effort by construction: prefix match on our registered spelling; no
stable public deep link exists (detail URLs are session-bound), so `url`
is the portal root and `docket_number` is the durable locator.
"""
from __future__ import annotations

import html
import re
import time

import requests

from ._common import name_matches, record, clean

STATE = "AK"
KEY = "ak_courtview"
NEEDS_COOKIE = False
BASE = "https://records.courts.alaska.gov"
INFO = {
    "name": "Alaska Court System — CourtView Public Access (company-name search)",
    "url": BASE + "/",
    "access": "anonymous, no captcha; per-association company-name search",
    "coverage": "all Alaska trial courts statewide, reliable from 1990",
    "caveat": "best-effort — starts-with name match; 500-case cap per search; "
              "no per-case public deep link (search the case number on the portal)",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_ROW = re.compile(r'<tr class="row(?:even|odd)">(.*?)</tr>', re.S)
_CELL = re.compile(r'<td id="grid~row-\d+~cell-(\d+)"[^>]*>(.*?)</td>', re.S)
_TRAIL = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)


def _strip(h: str) -> str:
    t = re.sub(r"<script.*?</script>", "", h, flags=re.S)
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", t)))


def _abs(url_of_page: str, href: str) -> str:
    href = html.unescape(href)
    return href if href.startswith("http") else url_of_page.split("?")[0] + href


def _parse_results(page_html: str, page_url: str):
    hits = []
    for row in _ROW.findall(page_html):
        cells = {int(n): c for n, c in _CELL.findall(row)}
        if not cells:
            continue
        txt = {n: _strip(c) for n, c in cells.items()}
        ctype = txt.get(4, "")
        loc = re.search(r"\((\w+)\)\s*$", ctype)
        hits.append({"case_number": txt.get(3, ""), "case_type": ctype,
                     "court_loc": loc.group(1) if loc else "", "file_date": txt.get(5, ""),
                     "party": txt.get(6, ""), "party_type": txt.get(7, ""),
                     "status": txt.get(9, "")})
    cur = re.search(r'<span id="\w+" title="Go to page (\d+)">', page_html)
    cur = int(cur.group(1)) if cur else 1
    pages = {int(n): h for h, n in re.findall(r'<a href="([^"]+)"[^>]*title="Go to page (\d+)"', page_html)}
    nxt = pages.get(cur + 1)
    return hits, (_abs(page_url, nxt) if nxt else None)


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = max(pace, 1.0)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA

    def _open_form(self):
        """home → (BrowserInfo postback) → Search Cases → Name tab; returns (form_id, action)."""
        r = self.s.get(BASE + "/", timeout=30)
        r.raise_for_status()
        m = re.search(r'<form name="postback"[^>]*action="([^"]+)"', r.text)
        if m:
            hf = re.search(r'name="(id\w+_hf_0)"', r.text).group(1)
            data = {hf: "", "navigatorAppName": "Netscape", "navigatorAppVersion": "5.0 (Macintosh)",
                    "navigatorAppCodeName": "Mozilla", "navigatorCookieEnabled": "true",
                    "navigatorJavaEnabled": "false", "navigatorLanguage": "en-US",
                    "navigatorPlatform": "MacIntel", "navigatorUserAgent": UA,
                    "screenWidth": "1512", "screenHeight": "982", "screenColorDepth": "24",
                    "utcOffset": "-9", "utcDSTOffset": "-8", "browserWidth": "1400",
                    "browserHeight": "900", "hostname": "records.courts.alaska.gov"}
            r = self.s.post(_abs(r.url, m.group(1)), data=data, timeout=30)
            r.raise_for_status()
        m = re.search(r"wicketSubmitFormById\('(id\w+)', '([^']+)', 'linkFrag:beginButton'", r.text)
        if not m:
            raise RuntimeError("Search Cases link not found: " + _strip(r.text)[:160])
        time.sleep(self.pace)
        r = self.s.post(_abs(r.url, m.group(2)), data={m.group(1) + "_hf_0": "", "linkFrag:beginButton": "1"}, timeout=30)
        r.raise_for_status()
        m = re.search(r'<a href="([^"]+)"[^>]*><span>Name</span>', r.text)
        if not m:
            raise RuntimeError("Name tab not found: " + _strip(r.text)[:160])
        time.sleep(self.pace)
        r = self.s.get(_abs(r.url, m.group(1)), timeout=30)
        r.raise_for_status()
        m = re.search(r'<form id="(id\w+)" method="post" action="([^"]+)"', r.text)
        if not m or "companyName" not in r.text:
            raise RuntimeError("company-name form not found: " + _strip(r.text)[:160])
        return m.group(1), _abs(r.url, m.group(2))

    def _rows(self, name: str, max_pages: int = 20) -> list[dict]:
        fid, action = self._open_form()
        q = _TRAIL.sub("", clean(name)).strip(" ,.") or clean(name)
        data = [(fid + "_hf_0", ""), ("lastName", ""), ("firstName", ""), ("middleName", ""),
                ("sffxCd", ""), ("companyName", q), ("statCd", " "), ("ptyCd", " "),
                ("dobDateRange:dateInputBegin", ""), ("dobDateRange:dateInputEnd", ""),
                ("dodDateRange:dateInputBegin", ""), ("dodDateRange:dateInputEnd", ""),
                ("fileDateRange:dateInputBegin", ""), ("fileDateRange:dateInputEnd", ""),
                ("submitLink", "Search"), ("caseCd", " ")]
        time.sleep(self.pace)
        r = self.s.post(action, data=data, timeout=60)
        r.raise_for_status()
        hits, nxt = _parse_results(r.text, r.url)
        pages = 1
        while nxt and pages < max_pages:
            time.sleep(self.pace)
            r = self.s.get(nxt, timeout=60)
            r.raise_for_status()
            more, nxt = _parse_results(r.text, r.url)
            pages += 1
            if not more:
                break
            hits.extend(more)
        return hits

    def search(self, name: str) -> list[dict]:
        by_case: dict[str, dict] = {}
        for h in self._rows(name):
            if not name_matches(h["party"], name):
                continue
            cur = by_case.setdefault(h["case_number"], dict(h, roles=set(), parties=set()))
            cur["roles"].add(h["party_type"].lower() or "party")
            cur["parties"].add(h["party"])
        out = []
        for cn, h in by_case.items():
            party = sorted(h["parties"])[0]
            rec = record(
                key=KEY, state=STATE,
                case_name=f"{party} ({'/'.join(sorted(h['roles']))}) — {h['case_type']}",
                court=h["case_type"], docket_number=cn, date_filed=h["file_date"],
                nature_of_suit=re.sub(r"\s*\(\w+\)\s*$", "", h["case_type"]),
                status=h["status"], associations=sorted(h["parties"]),
                url=BASE + "/", case_id=cn, queried=name)
            rec["association_role"] = sorted(h["roles"])
            out.append(rec)
        return out
