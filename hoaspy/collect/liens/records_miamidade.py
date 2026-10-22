"""Miami-Dade County, FL official-records index, over the Clerk's public search API.

Broward hands out its whole index over SFTP; Miami-Dade does not. What it
exposes is the JSON API behind the public Official Records search at
https://onlineservices.miamidadeclerk.gov/officialrecords/. Two calls:

    POST /api/home/standardsearch?partyName=&documentType=<T>&dateRangeFrom=..&
         dateRangeTo=..&searchT=<T>&firstQuery=y&searchtype=Name/Document
         -> {"isValidSearch": true, "qs": "<opaque encrypted criteria blob>"}
    GET  /api/SearchResults/getStandardRecords?qs=<blob>
         -> {"searchCritiriea": {...}, "recordingModels": [ <row>, ... ]}

Three things shape the collector:

1. Auth is one opaque cookie, `.PremierIDDade`: anonymous, issued by the
   site's SSO, no login. Copy it once from a browser (DevTools ->
   Application -> Cookies); it is never committed. Since October 2026 the
   page is a React app behind Cloudflare Turnstile and `standardsearch`
   answers `isValidSearch: false` to any call without a fresh
   `x-recaptcha-token` from the page's own widget: a token made outside a
   browser would be captcha circumvention, so `BrowserSession` (bottom)
   runs the same two calls inside the site's own page in a local browser
   (hoaspy/lib/browser.py), the page's own widget minting the token. The
   cookie session (`open_session`) stays for a cookie copied from a
   browser that the Clerk still honours.
2. Results are capped at 500 rows, ordered by (recorded date, sequence)
   ascending: a wider window silently returns only its earliest dates.
   The complete mode is therefore the per-association party-name query
   (`search_party`), which stays far under the cap; the date-window mode
   (`enumerate_window`) reports the days it had to truncate.
3. Every document is returned once per party (a D "direct/filer" row and
   an R "reverse/respondent" row), so rows are collapsed to one record per
   clerk file number (CFN), gathering all parties.

Unlike Broward, the Miami-Dade index carries the folio (parcel), the legal
description and often the street address on the same row, so these
records do locate the property. Rows carry the shared lien columns plus
`cfn`, `subdivision`, `book_page`, `address` and, from the name-driven
mode, `query_name`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from hoaspy.lib.http import BROWSER_USER_AGENT
from hoaspy.lib.names import LIEN_PARTY_RE
from hoaspy.lib.records import Lien

log = logging.getLogger(__name__)

RECORDER_URL = "https://onlineservices.miamidadeclerk.gov/officialrecords"
SEARCH_PAGE = f"{RECORDER_URL}/StandardSearch"
SOURCE = "miamidade-or-standardsearch"
COOKIE_NAME = ".PremierIDDade"
ROW_CAP = 500                   # the server's hard limit on rows per result set
FIRST_DAY = "1978-01-01"

# The Miami-Dade document-type strings (exact, from /api/home/documentTypes)
# mapped to the shared codes. The federal-tax-lien label carries a double
# space, kept verbatim.
LIEN_TYPES = {
    "LIEN - LIE": ("LIE", "claim_of_lien"),
    "LIS PENDENS - LIS": ("LP", "lis_pendens"),
    "NOTICE OF CONTEST OF LIEN - NCT": ("NCL", "notice_of_contest_of_lien"),
    "CANCELLATION OF LIS PENDENS - CLP": ("CLP", "cancellation_of_lis_pendens"),
    "FEDERAL TAX LIEN  - FTL": ("FTL", "federal_tax_lien"),
    "NOTICE OF TAX LIEN - NTL": ("NTL", "notice_of_tax_lien"),
    "SATISFACTION OF JUDGMENT - SJU": ("SJU", "satisfaction_of_judgment"),
}
# The lien itself plus escalation, as for Broward. Tax liens and
# satisfactions are opt-in: huge and mostly not association matters.
DEFAULT_TYPES = ["LIEN - LIE", "LIS PENDENS - LIS", "NOTICE OF CONTEST OF LIEN - NCT"]


class MiamiDadeError(RuntimeError):
    """The API refused or answered with something other than records."""


def open_session(premier_id_cookie: str) -> requests.Session:
    """A session carrying the `.PremierIDDade` cookie (its value, with or
    without the `.PremierIDDade=` prefix)."""
    value = premier_id_cookie.strip()
    if value.startswith(COOKIE_NAME + "="):
        value = value.split("=", 1)[1]
    if not value:
        raise MiamiDadeError(f"empty {COOKIE_NAME} cookie")
    session = requests.Session()
    session.headers.update({
        "user-agent": BROWSER_USER_AGENT, "accept": "application/json",
        "referer": SEARCH_PAGE, "origin": "https://onlineservices.miamidadeclerk.gov",
    })
    session.cookies.set(COOKIE_NAME, value, domain="onlineservices.miamidadeclerk.gov")
    return session


def mint_query(session: requests.Session, doctype: str, party: str, frm: str, to: str,
               timeout: int = 30) -> str | None:
    """The encrypted `qs` blob for one search, or None when the server
    reports no matching records (isValidSearch false)."""
    params = {"partyName": party, "dateRangeFrom": frm, "dateRangeTo": to, "documentType": doctype,
              "searchT": doctype, "firstQuery": "y", "searchtype": "Name/Document"}
    url = f"{RECORDER_URL}/api/home/standardsearch?" + urllib.parse.urlencode(params)
    response = session.post(url, data="", headers={"content-type": "application/json; charset=utf-8"},
                            timeout=timeout)
    if response.status_code != 200:
        raise MiamiDadeError(f"standardsearch HTTP {response.status_code}")
    body = response.json()
    if not body.get("isValidSearch") or not body.get("qs"):
        return None
    return body["qs"]


def fetch_rows(session: requests.Session, query: str, timeout: int = 60) -> list[dict]:
    """The rows a minted query answers with."""
    url = f"{RECORDER_URL}/api/SearchResults/getStandardRecords?qs=" + urllib.parse.quote(query, safe="")
    response = session.get(url, timeout=timeout)
    if response.status_code != 200:
        raise MiamiDadeError(f"getStandardRecords HTTP {response.status_code}")
    body = response.json()
    # A query that decrypts to null criteria (a bad or expired cookie)
    # answers with rows and no criteria: an auth failure, not "no data".
    criteria = body.get("searchCritiriea") or {}
    rows = body.get("recordingModels") or []
    if rows and criteria.get("documentType") is None:
        raise MiamiDadeError(f"results returned with null criteria — the {COOKIE_NAME} cookie is "
                             "stale or wrong; refresh it from the browser")
    return rows


def query(session, doctype: str, frm: str, to: str, party: str = "") -> tuple[list[dict], bool]:
    """One search: (rows, capped), where capped means the row count hit the
    server limit and the window's later dates were cut off. `session` is
    the cookie session or a `BrowserSession`."""
    if isinstance(session, BrowserSession):
        return session.query(doctype, frm, to, party)
    minted = mint_query(session, doctype, party, frm, to)
    if minted is None:
        return [], False
    rows = fetch_rows(session, minted)
    return rows, len(rows) >= ROW_CAP


def collapse_parties(rows: list[dict]) -> dict[int, dict]:
    """The per-party rows grouped into one row per clerk file number, with
    every (party name, party code) kept under `_parties`."""
    by_cfn: dict[int, dict] = {}
    parties: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        cfn = row.get("cfN_MASTER_ID")
        if cfn is None:
            continue
        parties[cfn].append((str(row.get("firsT_PARTY") or "").strip(), str(row.get("partY_CODE") or "").strip()))
        by_cfn.setdefault(cfn, row)
    for cfn, row in by_cfn.items():
        row["_parties"] = parties[cfn]
    return by_cfn


def iso_date(recorded: str) -> str:
    """'8/30/2004 12:00:00 AM' -> '2004-08-30'; '' when the text is not a date."""
    if not recorded:
        return ""
    try:
        return datetime.strptime(recorded.split(" ")[0], "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def to_record(row: dict, doctype: str, retrieved_at: str = "") -> Lien:
    """One collapsed row as a lien record."""
    code, label = LIEN_TYPES.get(doctype, (doctype, doctype))
    people = row.get("_parties") or [(str(row.get("firsT_PARTY") or "").strip(),
                                      str(row.get("partY_CODE") or "").strip())]
    filers = [name for name, role in people if role == "D" and name]
    respondents = [name for name, role in people if role == "R" and name]
    # The association is normally the filer (lienor) but occasionally
    # indexed on the reverse side: filer first, then respondent, as for Broward.
    association = next((name for name in filers if LIEN_PARTY_RE.search(name)), "")
    if not association:
        association = next((name for name in respondents if LIEN_PARTY_RE.search(name)), "")
    recorded = iso_date(row.get("reC_DATE") or "")
    folio = row.get("foliO_NUMBER") or 0
    return Lien(
        doc_id=(row.get("clerk_File") or f"{row.get('cfN_YEAR')}-{row.get('cfN_SEQ')}").strip(),
        doc_type=code, doc_type_label=label, recorded_date=recorded, recorded_ymd=recorded,
        year=row.get("cfN_YEAR"), state="FL", county="Miami-Dade", source=SOURCE, source_page=SEARCH_PAGE,
        association=association, filers=filers, respondents=respondents, n_parties=len(people),
        amount=row.get("consideratioN_1") or 0, case_number=(row.get("casE_NUM") or "") or "",
        parcel_id=str(folio) if folio else "",
        legal_description=(row.get("legaL_DESCRIPTION") or row.get("subdiV_NAME") or "").strip(),
        property_address=(row.get("address") or "").strip(), retrieved_at=retrieved_at,
        extras={"cfn": (row.get("clerk_File") or "").strip(), "subdivision": (row.get("subdiV_NAME") or "").strip(),
                "book_page": (row.get("reC_BOOKPAGE") or "").strip(), "address": (row.get("address") or "").strip()})


# ---- name-driven mode: complete ------------------------------------------------
# An exact-name party search returns every recording naming that party
# across all years in one response, and a lone association's lien history
# is far under the 500-row cap (the busiest condo associations run a few
# hundred lifetime filings). This is the mode that is complete.

def search_party(session: requests.Session, name: str, doctypes: list[str] | None = None,
                 frm: str = FIRST_DAY, to: str | None = None, pace: float = 0.4,
                 retrieved_at: str = "") -> list[dict]:
    """Every lien-family record naming `name`, as rows. Each document type
    is queried on its own (each result set is small and complete); a type
    that comes back at the cap for one name is logged — that would be an
    association with 250+ filings of one type, to be looked at, not
    silently cut."""
    doctypes = doctypes or DEFAULT_TYPES
    to = to or date.today().isoformat()
    by_cfn: dict[int, tuple[dict, str]] = {}
    for doctype in doctypes:
        rows, capped = query(session, doctype, frm, to, party=name)
        time.sleep(pace)
        if capped:
            log.warning("Miami-Dade name %r type %s hit the %d-row cap — results for this association may be "
                        "incomplete", name, doctype, ROW_CAP)
        for cfn, row in collapse_parties(rows).items():
            by_cfn.setdefault(cfn, (row, doctype))
    return [to_record(row, doctype, retrieved_at).as_row() for row, doctype in by_cfn.values()]


def fetch_for_names(session: requests.Session, names: list[str], doctypes: list[str] | None = None,
                    pace: float = 0.4, require_association: bool = False, progress=None,
                    skip: set[str] | None = None, seen_ids: set[str] | None = None,
                    on_name_done=None, retrieved_at: str = "") -> list[dict]:
    """search_party over many names, de-duplicated across names; each row
    carries `query_name`. `require_association` re-applies the name filter
    to the returned parties — off by default, since the queried name is
    itself the association even when the clerk's spelling dodges the
    vocabulary. For long runs: `skip` holds names already collected,
    `seen_ids` seeds the cross-name de-duplication from a resume, and
    `on_name_done(name, rows)` fires after each name so the caller can
    checkpoint."""
    skip = skip or set()
    seen: set[str] = set(seen_ids or ())
    out: list[dict] = []
    for i, name in enumerate(names, 1):
        if name in skip:
            if progress:
                progress(i, len(names), name, len(out))
            continue
        try:
            rows = search_party(session, name, doctypes=doctypes, pace=pace, retrieved_at=retrieved_at)
        except MiamiDadeError:
            raise
        except Exception as exc:        # one bad name must not sink the whole run
            log.warning("Miami-Dade name %r failed: %s", name, exc)
            continue
        fresh: list[dict] = []
        for row in rows:
            if require_association and not row["association"]:
                continue
            row = {**row, "query_name": name}
            if row["doc_id"] in seen:
                continue
            seen.add(row["doc_id"])
            fresh.append(row)
        out.extend(fresh)
        if on_name_done:
            on_name_done(name, fresh)
        if progress:
            progress(i, len(names), name, len(out))
    return out


# ---- date-window mode: discovery -----------------------------------------------
# Walks a date range to surface liens filed by associations not yet on any
# list. Honest about its limit: a day with more than ~250 lien-family
# recordings is truncated at the cap, and that is reported rather than
# paged past. Good for a recent rolling window, where daily volume is low.

def enumerate_window(session: requests.Session, start: date, end: date, doctypes: list[str] | None = None,
                     pace: float = 0.5, associations_only: bool = True,
                     retrieved_at: str = "") -> tuple[list[dict], list[str]]:
    """(rows, truncated days) for the association lien-family documents
    recorded in [start, end], day by day; a truncated day's result set hit
    the cap and is incomplete."""
    doctypes = doctypes or DEFAULT_TYPES
    seen: set[int] = set()
    out: list[dict] = []
    truncated: list[str] = []
    for doctype in doctypes:
        day = start
        while day <= end:
            rows, capped = query(session, doctype, day.isoformat(), day.isoformat())
            time.sleep(pace)
            if capped:
                truncated.append(f"{doctype} {day.isoformat()}")
                log.warning("Miami-Dade %s %s: %d-row cap hit — the day is truncated, not complete",
                            doctype, day.isoformat(), ROW_CAP)
            for cfn, row in collapse_parties(rows).items():
                if cfn in seen:
                    continue
                seen.add(cfn)
                record = to_record(row, doctype, retrieved_at)
                if associations_only and not record.association:
                    continue
                out.append(record.as_row())
            day += timedelta(days=1)
    return out, truncated


# ---- the page's own search, in a local browser ---------------------------------
# The Turnstile widget the app renders for its Search button is used as the
# app uses it: rendered from the site key the page itself carries, executed
# in the page, the token sent on the page's own fetch. Nothing is minted or
# solved outside the browser.

SEARCH_PAGE_READY_JS = "!!(window.turnstile && document.readyState === 'complete')"

# The app's site key: off the widget iframe when the app has rendered one,
# else off the app bundle, where the widget is rendered from a literal.
SITE_KEY_JS = r"""(async () => {
  const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
  const m = f && f.src.match(/\/(0x[0-9A-Za-z_-]{16,})\//);
  if (m) return m[1];
  for (const s of document.querySelectorAll('script[src*="/assets/"]')) {
    try {
      const t = await (await fetch(s.src)).text();
      const k = t.match(/"(0x4[0-9A-Za-z_-]{19,40})"/);
      if (k) return k[1];
    } catch (e) {}
  }
  return "";
})()"""

# One search: a token from a widget executed for it, then the app's two
# calls. Answers JSON: {ok, rows, criteria} or {ok: false, err}.
SEARCH_JS = r"""(async () => {
  const P = __PARAMS__;
  const div = document.createElement('div');
  div.style.cssText = 'position:absolute;left:-9999px';
  document.body.appendChild(div);
  const token = await new Promise((ok) => {
    let w;
    try {
      w = window.turnstile.render(div, {sitekey: P.sitekey, action: 'submit', execution: 'execute',
        appearance: 'interaction-only', callback: ok,
        'error-callback': (e) => ok('ERR:' + e), 'expired-callback': () => ok('ERR:expired')});
      window.turnstile.execute(w);
    } catch (e) { ok('ERR:' + e); }
    setTimeout(() => ok('ERR:timeout'), 45000);
  });
  div.remove();
  if (!token || token.startsWith('ERR:')) return JSON.stringify({ok: false, err: 'turnstile ' + token});
  const q = new URLSearchParams({partyName: P.party, dateRangeFrom: P.frm, dateRangeTo: P.to,
    documentType: P.doctype, searchT: P.doctype, firstQuery: 'y', searchtype: 'Name/Document'});
  const r = await fetch('/officialrecords/api/home/standardsearch?' + q, {method: 'POST', body: '',
    headers: {Accept: 'application/json', 'x-recaptcha-token': token, 'content-type': 'application/json; charset=utf-8'}});
  if (!r.ok) return JSON.stringify({ok: false, err: 'standardsearch HTTP ' + r.status});
  const t = await r.json();
  if (!t || !t.isValidSearch || !t.qs) return JSON.stringify({ok: true, rows: [], criteria: null});
  const r2 = await fetch('/officialrecords/api/SearchResults/getStandardRecords?qs=' + encodeURIComponent(t.qs),
    {headers: {Accept: 'application/json'}});
  if (!r2.ok) return JSON.stringify({ok: false, err: 'getStandardRecords HTTP ' + r2.status});
  const b = await r2.json();
  return JSON.stringify({ok: true, rows: b.recordingModels || [], criteria: b.searchCritiriea || null});
})()"""


class BrowserSession:
    """The search page open in a throw-away context of the local browser
    (DevTools on 127.0.0.1:9100 unless `devtools_url` says otherwise);
    `query()` has the same contract as the module function."""

    def __init__(self, devtools_url: str | None = None):
        from hoaspy.lib.browser import DEVTOOLS_URL, DevTools
        self.devtools = DevTools(devtools_url or DEVTOOLS_URL)
        self.context = self.devtools.context()
        self.tab = self.context.open_tab(SEARCH_PAGE, background=False)
        for _ in range(60):
            time.sleep(0.5)
            if self.tab.evaluate(SEARCH_PAGE_READY_JS, timeout=5):
                break
        else:
            raise MiamiDadeError("the Official Records page never finished loading in the browser")
        self.sitekey = self.tab.evaluate(SITE_KEY_JS, timeout=60, await_promise=True) or ""
        if not self.sitekey:
            raise MiamiDadeError("no Turnstile site key on the Official Records page — has the app changed?")

    def query(self, doctype: str, frm: str, to: str, party: str = "") -> tuple[list[dict], bool]:
        params = {"sitekey": self.sitekey, "doctype": doctype, "frm": frm, "to": to, "party": party}
        answer = self.tab.evaluate(SEARCH_JS.replace("__PARAMS__", json.dumps(params)), timeout=180, await_promise=True)
        try:
            out = json.loads(answer or "")
        except ValueError:
            raise MiamiDadeError("no answer from the search page") from None
        if not out.get("ok"):
            raise MiamiDadeError(str(out.get("err") or "search failed in the browser"))
        rows = out.get("rows") or []
        if rows and (out.get("criteria") or {}).get("documentType") is None:
            raise MiamiDadeError("results returned with null criteria — the search was not accepted")
        return rows, len(rows) >= ROW_CAP

    def close(self) -> None:
        try:
            self.context.dispose()
        finally:
            self.devtools.close()
