"""Maryland Judiciary Case Search — statewide party search by business name.

    https://casesearch.courts.state.md.us/casesearch/

District Court and Circuit Court civil cases for all 24 Maryland
jurisdictions, back to the 1980s (older in a few). The React portal talks to
one JSON endpoint, `POST /api-caselist/v1/cases` with
`{searchPartyType: "Business", businessName, caseType: "CIVIL", rangeFrom,
rangeTo}`, which returns every **party row** whose business name *starts
with* the query — a case appears once per matching party — with the case
number, the party's recorded name and role (Plaintiff / Defendant / Other /
AKA), the court, case type, status, filing date and caption. 600 rows at
most, sorted by party name, so a capped query is split by filing-date window
until every window is under the cap (Hillsborough's trick). No results is an
HTTP 400 whose body says `code: 404`. Every case has a public deep link,
`/casesearch/case-detail-page?caseId=<case number without hyphens>`.

DataDome fronts the site: a plain request gets a 403 whose body is a
captcha-delivery URL, and headless Chrome is refused outright ("Access is
temporarily restricted"). A real browser passes its device check without any
click, so — like re:SearchTX — this adapter borrows **your own browser's
session**: open the portal in Chrome, press "I Agree", run any search, and
copy the `Cookie:` header of the `/api-caselist/v1/cases` request from
DevTools (only `datadome=…` matters) into `md_cookie.txt`, and on a second
line that browser's `User-Agent: …` header (DataDome pairs the two):

    ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal md_casesearch --cookie-file md_cookie.txt

That lasts a few hundred requests: DataDome then refuses the cookie when
it is used outside the browser that minted it (the first sweep, 2026-09-30,
was cut off twice and the host sat on "Access is temporarily restricted"
for five hours). The way that holds up is to **search from inside a real
Chrome** — the cookie file then says where to find it:

    cdp: http://127.0.0.1:9612

    google-chrome --user-data-dir=/tmp/md-chrome --remote-debugging-port=9612 https://casesearch.courts.state.md.us/casesearch/

The adapter takes the tab that is on the portal (or opens one), presses
"I Agree", and runs every search as the page's own same-origin `fetch()`,
so the request carries the browser's TLS fingerprint and the DataDome tag
keeps the cookie fresh; a refused fetch reloads the page once (the device
check passes again without a click) before giving up. Nothing is minted or
solved from Python; a real captcha or the restriction page stops the run.
Headless Chrome is refused, so the window has to be a real one.

The disclaimer the portal shows (recorded 2026-09-30) excludes confidential
records, disclaims accuracy and forbids interfering with the systems or
altering records; it says nothing against automated or bulk use. Requests
are paced; a 403 mid-run raises PermissionError so the run stops resumably.

Two kinds of query, both best-effort by construction:

* **per association name** (the driver's list: IRS roster, Montgomery CCOC
  registry, …) — the distinctive core of the name as a prefix, and only
  parties that are plausibly that association (`party_is_association`);
* **statewide sweeps** (`SWEEPS`, appended by the driver) — the statutory and
  customary Maryland association forms as prefixes: Maryland condominiums
  litigate as "Council of Unit Owners of <X> Condominium" (Real Prop.
  § 11-109; older ones "Council of Co-Owners"), 1990s Prince George's
  suits as "Board of Directors of <X>", and many HOAs file as "Homeowners
  Association of <X>". Those catch communities no roster holds. A party
  that is nothing but the form ("COUNCIL OF UNIT OWNERS") names no
  community and is dropped; so is a business wearing the words
  ("Homeowners Loan Corp").

The association name written out is the party as the court recorded it
(c/o manager tails removed), so build_site can attach the case to the
registered community through its core name.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time

import json
import urllib.request

import requests

from . import RateLimited
from ._common import (clean, has_business_form, looks_like_association,
                      normalize, party_is_association, record)

STATE = "MD"
KEY = "md_casesearch"
NEEDS_COOKIE = True
BASE = "https://casesearch.courts.state.md.us"
SEARCH_URL = BASE + "/casesearch/"
API = BASE + "/api-caselist/v1/cases"
INFO = {
    "name": "Maryland Judiciary Case Search — business-name party search (civil)",
    "url": SEARCH_URL,
    "access": "searched from your own Chrome window over its DevTools port, or with its DataDome "
              "cookie (no account); per-association prefix search plus statewide sweeps of the "
              "Maryland association name forms",
    "coverage": "District and Circuit Court civil cases, all 24 Maryland jurisdictions, from the 1980s",
    "caveat": "best-effort — starts-with match on the party's recorded name; 600-row cap per query "
              "(split by filing-date window); a party recorded only as 'Council of Unit Owners' names "
              "no community and is dropped",
}
# DataDome pairs the cookie with the browser that minted it, so the cookie
# file may carry that browser's `User-Agent:` line; this is the fallback.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
CAP = 600
FIRST_DAY = _dt.date(1960, 1, 1)
log = logging.getLogger("state_courts.md")

# Statewide prefixes queried after the named associations (the driver
# appends them; --no-sweeps leaves them out). Each is one query plus its
# date-window splits.
SWEEPS = (
    "council of unit owners", "the council of unit owners",
    "council of co-owners", "the council of co-owners",
    "homeowners", "home owners", "the homeowners",
    "board of directors of", "condominium", "the condominium",
)
# The statutory / customary wrappers around a community's name. Stripping
# them tells generic parties ("COUNCIL OF UNIT OWNERS") from real ones
# ("COUNCIL OF UNIT OWNERS OF FRENCHMAN'S CREEK CONDOMINIUM").
_WRAPPER = re.compile(
    r"^(?:THE\s+)?(?:"
    r"COUNCIL\s+OF\s+(?:UNIT\s*OWNERS|CO\s*OWNERS)|"
    r"(?:BOARD\s+OF\s+DIRECTORS|BD\s+OF\s+DIR(?:ECTORS)?)|"
    r"HOME\s*OWNERS'?\s+(?:ASSOCIATION|ASSOC|ASSN)|HOME\s*OWNERS'?(?=\s+OF\b)|"
    r"CONDOMINIUM\s+(?:ASSOCIATION|ASSOC|ASSN)"
    r")(?:\s+(?:OF|FOR))?\b'?\s*")
# Words that do not make a community name on their own.
_FILLER = re.compile(r"^(?:INC|INCORPORATED|CORP|CORPORATION|THE|A|AN|OF|FOR|AND|AT|"
                     r"CONDOMINIUM|CONDOMINIUMS|CONDO|ASSOCIATION|ASSOC|ASSN|COUNCIL|"
                     r"UNIT|OWNERS|BOARD|DIRECTORS|[IVX]+|\d+)$")
# Bodies and businesses that wear the association words in a sweep.
_NOT_COMMUNITY = re.compile(
    r"\b(?:DISPUTE|REVIEW BOARD|COMMISSION|INSTITUTE|DEPARTMENT|UNITED STATES|USA|"
    r"BANK|CREDIT UNION|TRUST|TRUSTEES?|CHURCH|SCHOOL|HOSPITAL|HEALTH|MEDICAL|WARRANTY|"
    r"EQUITY|LOANS?|TITLE|ESCROW|GUARDIAN|FUNDING|MARKETING|RELOCATION|CONSULTANTS?|"
    r"CONCEPTS?|SUPPLY|HANDYMAN|INVENTORY|REALTY|REAL ESTATE|MORTGAGE|FINANCIAL|FINANCE|"
    r"INSURANCE|MANAGEMENT|SERVICES?|CONSTRUCTION|CONTRACTORS?|CONTRACTING|INVESTMENTS?|"
    r"INVESTORS|HOLDINGS?|CAPITAL|DEVELOPMENT|DEVELOPERS?|BUILDERS?|ENTERPRISES?|"
    r"INDUSTRIES|HOTLINE|PAYROLL|PROTECTION|SOLUTIONS|RESOURCES?|SOURCES?|NETWORK|"
    r"EXCHANGE|TAX CREDIT|GENERAL CONSTR|LLC|L L C|LP|L P|LTD)\b")
_SUFFIX = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
_CARE_OF = re.compile(r"\s*(?:,\s*)?\b(?:C/O|IN CARE OF|ATTN)\b.*$", re.I)
# Where a registry name's distinctive core ends (first association word).
_MARK = re.compile(
    r"\b(?:CONDOMINIUM|CONDO|CONDOMINIUMS|HOMEOWNERS?|HOME OWNERS?|PROPERTY OWNERS?|"
    r"OWNERS|ASSOCIATION|ASSOC|ASSN|HOA|POA|COA|COMMUNITY ASSOCIATION|MASTER ASSOCIATION|"
    r"RESIDENTS ASSOCIATION|TOWNHOMES?|TOWNHOUSES?|VILLAS? ASSOCIATION|A CONDO|"
    r"COUNCIL OF (?:UNIT|CO) ?OWNERS)\b.*$", re.I)

# District Court locations are named for towns; map them to the county the
# site uses (site/counties.json spellings). Circuit courts say "<X> County".
_TOWN_COUNTY = {
    "CIVIL": "Baltimore City", "BALTIMORE CITY": "Baltimore City", "WABASH": "Baltimore City",
    "BORGERDING": "Baltimore City", "HARGROVE": "Baltimore City", "EASTSIDE": "Baltimore City",
    "TOWSON": "Baltimore", "CATONSVILLE": "Baltimore", "ESSEX": "Baltimore",
    "ROCKVILLE": "Montgomery", "SILVER SPRING": "Montgomery",
    "UPPER MARLBORO": "Prince George's", "HYATTSVILLE": "Prince George's", "LANDOVER": "Prince George's",
    "GLEN BURNIE": "Anne Arundel", "ANNAPOLIS": "Anne Arundel",
    "BEL-AIR": "Harford", "BEL AIR": "Harford", "CUMBERLAND": "Allegany", "OAKLAND": "Garrett",
    "HAGERSTOWN": "Washington", "WESTMINSTER": "Carroll", "ELLICOTT CITY": "Howard",
    "ELKTON": "Cecil", "DENTON": "Caroline", "CHESTERTOWN": "Kent", "CENTREVILLE": "Queen Anne's",
    "EASTON": "Talbot", "CAMBRIDGE": "Dorchester", "SALISBURY": "Wicomico",
    "PRINCESS ANNE": "Somerset", "SNOW HILL": "Worcester", "OCEAN CITY": "Worcester",
    "LEONARDTOWN": "St. Mary's", "LA PLATA": "Charles", "WALDORF": "Charles",
    "PRINCE FREDERICK": "Calvert", "FREDERICK": "Frederick",
}
_COUNTY_FIX = {"SAINT MARY'S": "St. Mary's", "ST MARY'S": "St. Mary's", "ST. MARY'S": "St. Mary's",
               "QUEEN ANNES": "Queen Anne's", "PRINCE GEORGES": "Prince George's"}


def county_of(location: str) -> str:
    """'Howard County District Court' -> Howard; 'Upper Marlboro District
    Court' -> Prince George's; 'Civil District Court' -> Baltimore City."""
    loc = clean(location).upper().replace("’", "'")
    m = re.match(r"^(.*?)\s+COUNTY\s+(?:CIRCUIT|DISTRICT)\s+COURT", loc)
    if m:
        name = m.group(1)
        return _COUNTY_FIX.get(name, name.title().replace("'S", "'s"))
    if "BALTIMORE CITY" in loc:
        return "Baltimore City"
    m = re.match(r"^(.*?)\s+DISTRICT\s+COURT", loc)
    town = re.sub(r"\s+\d+$", "", m.group(1) if m else loc)      # "Silver Spring 02"
    return _TOWN_COUNTY.get(town, "")


def case_url(case_number: str) -> str:
    return f"{BASE}/casesearch/case-detail-page?caseId={re.sub(r'[^A-Za-z0-9]', '', case_number)}"


def _bare(name: str) -> str:
    """The registry name without its entity suffix and leading article."""
    n = _SUFFIX.sub("", _CARE_OF.sub("", clean(name))).strip(" ,.")
    return re.sub(r"^(?:THE|A)\s+", "", n, flags=re.I)


def split_form(party: str) -> tuple[str, str]:
    """(form, rest): the leading Maryland form(s) the party is written in
    and the upper-cased community name left after them, with c/o tails and
    entity suffixes removed. form is '' when the party starts with no form."""
    p = " ".join(re.sub(r"[^A-Z0-9' ]+", " ", _bare(party).upper()).split())
    rest = p
    while True:
        q = _WRAPPER.sub("", rest)
        if q == rest:
            break
        rest = q
    rest = rest.strip()
    return p[:len(p) - len(rest)].strip(), rest


def strip_wrappers(party: str) -> str:
    """The community name inside a party written in a Maryland form."""
    return split_form(party)[1]


def query_for(name: str) -> str:
    """The prefix to search for one of our association names. A name in a
    Maryland form ("Council of Unit Owners of X") is searched whole; otherwise
    its distinctive core ("Waters Edge At North Lake"), and when the core is
    a single word, the core plus the next word ("Grosvenor Homeowners") so an
    abbreviated "GROSVENOR HOMEOWNERS ASSOC" still matches."""
    n = _bare(name)
    if not n or _WRAPPER.match(n.upper()):
        return n
    core = _MARK.sub("", n).strip(" ,.-&")
    words = n.split()
    if len(core.split()) >= 2 and len(core) >= 6:
        return core
    if len(core.split()) == 1 and len(core) >= 4 and len(words) >= 2:
        return " ".join(words[:2])
    return n


def match_core(name: str) -> str:
    """What a returned party must carry to count as this association:
    the community name inside a Maryland form, else the distinctive core,
    else the whole name (normalized)."""
    n = _bare(name)
    if _WRAPPER.match(n.upper()):
        return normalize(strip_wrappers(n))
    core = _MARK.sub("", n).strip(" ,.-&")
    return normalize(core) or normalize(n)


def known_cores(state: str = STATE) -> set[str]:
    """Distinctive cores of every association we hold for `state` — what a
    'Board of Directors of X' party must name to count in a sweep."""
    from ..records_miamidade_civil import known_association_norms
    return {c for c in (normalize(_MARK.sub("", n)) for n in known_association_norms(state)) if c}


def community_name(party: str, sweep: bool, known: set[str] | None = None) -> str:
    """The party name to attach the case to, or '' when the party is not a
    community: a bare form with no distinctive words, a business wearing the
    words, or (in a sweep) a government body, lender or trade. A sweep
    keeps a party only when it is written in a Maryland form ("Homeowners
    Association of X", never "Homeowners Loan Corp") or is itself a
    condominium; 'Board of Directors of X' is any body's board, so X must
    carry an association word or be a community we hold."""
    name = _CARE_OF.sub("", clean(party)).strip(" ,.")
    if not name:
        return ""
    form, rest = split_form(name)
    if not any(not _FILLER.match(w) for w in rest.split()):
        return ""
    if has_business_form(name):
        return ""
    if sweep:
        if _NOT_COMMUNITY.search(rest):
            return ""
        if not form and not re.match(r"^(?:THE\s+)?CONDOMINIUM", rest):
            return ""
        if form.startswith(("BOARD", "BD", "THE BOARD", "THE BD")):
            if not _MARK.search(rest) and normalize(_MARK.sub("", rest)) not in (known or set()):
                return ""
    return name


_FETCH_JS = """(async () => {
  const r = await fetch(%s, {method: "POST", credentials: "include",
                            headers: {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"},
                            body: %s});
  return {status: r.status, retry: r.headers.get("Retry-After") || "", text: await r.text()};
})()"""


class _Tab:
    """One tab of a real Chrome reached over its DevTools port (`cdp:` line
    in the cookie file). The tab sits on the portal, past "I Agree", and runs
    each search as the page's own same-origin fetch(), so DataDome sees the
    browser it already trusts: its TLS and HTTP/2 fingerprint, its cookie
    kept fresh by the DataDome tag. A refused fetch reloads the page once —
    the device check passes again without a click — and tries again."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")
        try:
            tabs = json.load(urllib.request.urlopen(self.endpoint + "/json", timeout=10))
        except Exception as exc:
            raise PermissionError(f"no Chrome at {self.endpoint} ({exc}) — start one with "
                                  "--remote-debugging-port and open the portal in it") from exc
        page = next((t for t in tabs if t.get("type") == "page" and "casesearch.courts.state.md.us" in t.get("url", "")), None)
        if page is None:
            req = urllib.request.Request(self.endpoint + "/json/new?" + SEARCH_URL, method="PUT")
            page = json.load(urllib.request.urlopen(req, timeout=10))
        import websocket
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=180)
        self.n = 0
        self.ready()

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def ev(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"].get("text") or r["exceptionDetails"])[:200])
        return r.get("result", {}).get("value")

    def ready(self, reload: bool = False) -> None:
        """Have the tab on the portal with the disclaimer accepted."""
        if reload or not str(self.ev("location.href") or "").startswith(SEARCH_URL):
            self.call("Page.navigate", url=SEARCH_URL)
        for _ in range(30):
            time.sleep(2)
            txt = self.ev("document.body ? document.body.innerText : ''") or ""
            if "captcha-delivery" in (self.ev("document.documentElement.outerHTML.slice(0, 5000)") or ""):
                raise PermissionError("Case Search shows 'Access is temporarily restricted' to this "
                                      "machine — wait and rerun (the checkpoint resumes)")
            if "I Agree" in txt:
                self.ev("(() => { const b = [...document.querySelectorAll('button')]"
                        ".find(b => b.textContent.trim() === 'I Agree'); if (b) b.click(); })()")
                time.sleep(2)
                return
            if "Party Type" in txt or "/inquiry-search" in str(self.ev("location.href") or ""):
                return
        raise PermissionError("the portal did not load in the browser tab")

    def post(self, body: dict) -> tuple[int, str, str]:
        r = self.ev(_FETCH_JS % (json.dumps(API), json.dumps(json.dumps(body))))
        if not isinstance(r, dict):
            raise RuntimeError("no answer from the browser tab")
        return int(r.get("status") or 0), r.get("text") or "", r.get("retry") or ""


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        if not cookie:
            raise ValueError("md_casesearch needs the datadome cookie from your browser, "
                             "or a `cdp: http://127.0.0.1:<port>` line naming a Chrome to search from")
        cdp = re.search(r"^\s*cdp:\s*(\S+)\s*$", cookie, re.I | re.M)
        self.tab = _Tab(cdp.group(1)) if cdp else None
        m = re.search(r"datadome=([^;\s]+)", cookie)
        value = m.group(1) if m else ("" if cdp else cookie.strip().splitlines()[0].strip())
        ua = re.search(r"^\s*user-agent:\s*(\S.*?)\s*$", cookie, re.I | re.M)
        self.pace = pace
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ua.group(1) if ua else UA, "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9", "Content-Type": "application/json",
            "Origin": BASE, "Referer": SEARCH_URL + "inquiry-search",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
            "Cookie": "datadome=" + value,
        })
        agent = self.s.headers["User-Agent"]
        major = re.search(r"Chrome/(\d+)", agent)
        if major:
            v = major.group(1)
            platform = "macOS" if "Macintosh" in agent else "Windows" if "Windows" in agent else "Linux"
            self.s.headers.update({
                "sec-ch-ua": f'"Google Chrome";v="{v}", "Not_A Brand";v="8", "Chromium";v="{v}"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": f'"{platform}"'})
        self.capped: list[str] = []      # "prefix day" windows that still hit the cap
        self.known: set[str] | None = None
        self._seen: dict[str, list[dict]] = {}   # prefix -> rows (phases share a prefix)

    # -- transport --------------------------------------------------------------------
    def _query(self, prefix: str, d0: _dt.date | None = None, d1: _dt.date | None = None) -> list[dict]:
        body = {"searchPartyType": "Business", "lastName": "", "firstName": "", "middleName": "",
                "businessName": prefix, "caseType": "CIVIL",
                "rangeFrom": d0.isoformat() if d0 else None,
                "rangeTo": d1.isoformat() if d1 else None, "exactDate": None}
        if self.tab is not None:
            status, text, retry = self.tab.post(body)
            if status == 403:                         # the tag's cookie went stale: reload once
                log.info("  browser fetch refused — reloading the portal tab")
                self.tab.ready(reload=True)
                time.sleep(self.pace)
                status, text, retry = self.tab.post(body)
        else:
            r = self.s.post(API, json=body, timeout=120)
            status, text, retry = r.status_code, r.text, r.headers.get("Retry-After") or ""
        if status == 403:
            raise PermissionError("Case Search answered 403 (DataDome) — copy a fresh datadome "
                                  "cookie from your browser into the cookie file and rerun")
        if status == 429:
            raise RateLimited(int(retry or 600))
        try:
            j = json.loads(text) if text else None
        except ValueError:
            j = None
        if status == 400:
            if isinstance(j, dict) and str(j.get("code")) == "404":   # "no cases exist for this search"
                return []
            raise RuntimeError(f"Case Search 400: {str((j or {}).get('error') if isinstance(j, dict) else text)[:160]}")
        if status >= 400:
            raise RuntimeError(f"Case Search HTTP {status}: {text[:120]}")
        return j if isinstance(j, list) else []

    def _rows(self, prefix: str, d0: _dt.date, d1: _dt.date, depth: int = 0) -> list[dict]:
        """Every party row for `prefix` filed in [d0, d1], splitting the window
        in two whenever a response hits the 600-row cap."""
        time.sleep(self.pace)
        rows = self._query(prefix, d0, d1)
        if len(rows) < CAP or d0 >= d1 or depth > 24:
            if len(rows) >= CAP:
                self.capped.append(f"{prefix} {d0.isoformat()}")
                log.warning("  %r still %d rows on %s — some cases that day are missed", prefix, CAP, d0)
            return rows
        mid = d0 + (d1 - d0) // 2
        return (self._rows(prefix, d0, mid, depth + 1)
                + self._rows(prefix, mid + _dt.timedelta(days=1), d1, depth + 1))

    # -- one query ----------------------------------------------------------------------
    def search(self, name: str) -> list[dict]:
        sweep = name.lower() in SWEEPS
        prefix = name if sweep else query_for(name)
        if not prefix:
            return []
        if sweep and self.known is None:
            self.known = known_cores()
        key = prefix.upper()
        if key not in self._seen:
            self._seen[key] = self._rows(prefix, FIRST_DAY, _dt.date.today())
        return shape(self._seen[key], name, "" if sweep else match_core(name), sweep, self.known)


def shape(rows: list[dict], queried: str, core: str, sweep: bool,
          known: set[str] | None = None) -> list[dict]:
    """Party rows -> one docket record per case naming the community."""
    by_case: dict[str, dict] = {}
    for d in rows:
        cn = clean(d.get("caseNumber") or "")
        party = clean(d.get("fullName") or "")
        if not cn or not party:
            continue
        if sweep:
            assoc = community_name(party, sweep=True, known=known)
            if assoc and not looks_like_association(assoc):
                assoc = ""
        else:
            assoc = community_name(party, sweep=False) if core and party_is_association(party, core) else ""
        if not assoc:
            continue
        role = {"Plaintiff": "plaintiff", "Defendant": "defendant"}.get(d.get("partyTypeDisplay") or "", "other")
        rec = by_case.get(cn)
        if rec is None:
            rec = record(key=KEY, state=STATE, case_name=clean(d.get("title") or "") or assoc,
                         court=clean(d.get("locationName") or ""), docket_number=cn,
                         date_filed=d.get("filingDate") or "",
                         nature_of_suit=d.get("caseType") or "", status=d.get("caseStatus") or "",
                         associations=[assoc], url=case_url(cn), case_id=cn, queried=queried)
            rec["association_role"] = [role]
            county = county_of(d.get("locationName") or "")
            if county:
                rec["county"] = county
            by_case[cn] = rec
        else:
            if assoc not in rec["associations"]:
                rec["associations"].append(assoc)
            if role not in rec["association_role"]:
                rec["association_role"].append(role)
    return list(by_case.values())
