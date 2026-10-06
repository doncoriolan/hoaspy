"""Maryland Judiciary Case Search — statewide party search by business name.

    https://casesearch.courts.state.md.us/casesearch/

District Court and Circuit Court civil cases for all 24 Maryland
jurisdictions, back to the 1980s. The React portal talks to one JSON
endpoint, POST /api-caselist/v1/cases with {searchPartyType: "Business",
businessName, caseType: "CIVIL", rangeFrom, rangeTo}, which returns every
**party row** whose business name *starts with* the query — a case appears
once per matching party — with the case number, the party's recorded name
and role (Plaintiff / Defendant / Other / AKA), the court, case type,
status, filing date and caption. 600 rows at most, sorted by party name,
so a capped query is split by filing-date window until every window is
under the cap. No results is an HTTP 400 whose body says `code: 404`.
Every case has a public deep link,
/casesearch/case-detail-page?caseId=<case number without hyphens>.

DataDome fronts the site: a plain request gets a 403 whose body is a
captcha-delivery URL, and headless Chrome is refused outright. A real
browser passes its device check without any click, so this adapter
borrows **your own browser's session**: open the portal in Chrome, press
"I Agree", run any search, and copy the Cookie header of the
/api-caselist/v1/cases request from DevTools (only `datadome=…` matters)
into md_cookie.txt, and on a second line that browser's User-Agent header
(DataDome pairs the two):

    python -m hoaspy state-courts --portal md_casesearch --cookie-file md_cookie.txt

That lasts a few hundred requests: DataDome then refuses a cookie used
outside the browser that minted it. What holds up is to **search from
inside a real Chrome**; the cookie file then says where to find it:

    cdp: http://127.0.0.1:9612

    google-chrome --user-data-dir=/tmp/md-chrome --remote-debugging-port=9612 https://casesearch.courts.state.md.us/casesearch/

The adapter takes the tab that is on the portal (or opens one), presses
"I Agree", and runs every search as the page's own same-origin fetch(), so
the request carries the browser's TLS fingerprint and the DataDome tag
keeps the cookie fresh; a refused fetch reloads the page once (the device
check passes again without a click) before giving up. Nothing is minted or
solved from Python; a real captcha or the restriction page stops the run.

The portal's disclaimer excludes confidential records, disclaims accuracy
and forbids interfering with the systems or altering records; it says
nothing against automated or bulk use. Requests are paced; a 403 mid-run
raises PermissionError so the run stops resumably.

Two kinds of query, both best-effort by construction:

* **per association name** (the driver's list: IRS roster, Montgomery CCOC
  registry, …): the distinctive core of the name as a prefix, and only
  parties that are plausibly that association (`party_is_association`);
* **statewide sweeps** (`SWEEPS`, appended by the driver): the statutory
  and customary Maryland association forms as prefixes. Maryland
  condominiums litigate as "Council of Unit Owners of <X> Condominium"
  (Real Prop. § 11-109; older ones "Council of Co-Owners"), 1990s Prince
  George's suits as "Board of Directors of <X>", and many HOAs file as
  "Homeowners Association of <X>". Those catch communities no roster
  holds. A party that is nothing but the form ("COUNCIL OF UNIT OWNERS")
  names no community and is dropped; so is a business wearing the words
  ("Homeowners Loan Corp").

The association name written out is the party as the court recorded it
(c/o manager tails removed), so a consumer can attach the case to the
registered community through its core name.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import urllib.request

import requests

from hoaspy.lib.http import BROWSER_USER_AGENT

from . import RateLimited
from ._common import clean, has_business_form, looks_like_association, normalize, party_is_association, record

STATE = "MD"
KEY = "md_casesearch"
NEEDS_COOKIE = True
PORTAL_URL = "https://casesearch.courts.state.md.us"
SEARCH_URL = PORTAL_URL + "/casesearch/"
API_URL = PORTAL_URL + "/api-caselist/v1/cases"
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
ROW_CAP = 600
FIRST_DAY = dt.date(1960, 1, 1)
MAX_SPLIT_DEPTH = 24
NO_CASES_CODE = "404"           # what the portal's 400 body says when a search has no cases

# Statewide prefixes queried after the named associations (the driver
# appends them; --no-sweeps leaves them out). Each is one query plus its
# date-window splits.
SWEEPS = (
    "council of unit owners", "the council of unit owners",
    "council of co-owners", "the council of co-owners",
    "homeowners", "home owners", "the homeowners",
    "board of directors of", "condominium", "the condominium",
)
# The statutory and customary wrappers around a community's name. Stripping
# them tells generic parties ("COUNCIL OF UNIT OWNERS") from real ones
# ("COUNCIL OF UNIT OWNERS OF FRENCHMAN'S CREEK CONDOMINIUM").
FORM_RE = re.compile(
    r"^(?:THE\s+)?(?:"
    r"COUNCIL\s+OF\s+(?:UNIT\s*OWNERS|CO\s*OWNERS)|"
    r"(?:BOARD\s+OF\s+DIRECTORS|BD\s+OF\s+DIR(?:ECTORS)?)|"
    r"HOME\s*OWNERS'?\s+(?:ASSOCIATION|ASSOC|ASSN)|HOME\s*OWNERS'?(?=\s+OF\b)|"
    r"CONDOMINIUM\s+(?:ASSOCIATION|ASSOC|ASSN)"
    r")(?:\s+(?:OF|FOR))?\b'?\s*")
BOARD_FORMS = ("BOARD", "BD", "THE BOARD", "THE BD")
# Words that do not make a community name on their own.
FILLER_RE = re.compile(r"^(?:INC|INCORPORATED|CORP|CORPORATION|THE|A|AN|OF|FOR|AND|AT|"
                       r"CONDOMINIUM|CONDOMINIUMS|CONDO|ASSOCIATION|ASSOC|ASSN|COUNCIL|"
                       r"UNIT|OWNERS|BOARD|DIRECTORS|[IVX]+|\d+)$")
# Bodies and businesses that wear the association words in a sweep.
NOT_COMMUNITY_RE = re.compile(
    r"\b(?:DISPUTE|REVIEW BOARD|COMMISSION|INSTITUTE|DEPARTMENT|UNITED STATES|USA|"
    r"BANK|CREDIT UNION|TRUST|TRUSTEES?|CHURCH|SCHOOL|HOSPITAL|HEALTH|MEDICAL|WARRANTY|"
    r"EQUITY|LOANS?|TITLE|ESCROW|GUARDIAN|FUNDING|MARKETING|RELOCATION|CONSULTANTS?|"
    r"CONCEPTS?|SUPPLY|HANDYMAN|INVENTORY|REALTY|REAL ESTATE|MORTGAGE|FINANCIAL|FINANCE|"
    r"INSURANCE|MANAGEMENT|SERVICES?|CONSTRUCTION|CONTRACTORS?|CONTRACTING|INVESTMENTS?|"
    r"INVESTORS|HOLDINGS?|CAPITAL|DEVELOPMENT|DEVELOPERS?|BUILDERS?|ENTERPRISES?|"
    r"INDUSTRIES|HOTLINE|PAYROLL|PROTECTION|SOLUTIONS|RESOURCES?|SOURCES?|NETWORK|"
    r"EXCHANGE|TAX CREDIT|GENERAL CONSTR|LLC|L L C|LP|L P|LTD)\b")
ENTITY_SUFFIX_RE = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
CARE_OF_RE = re.compile(r"\s*(?:,\s*)?\b(?:C/O|IN CARE OF|ATTN)\b.*$", re.I)
# Where a registry name's distinctive core ends (the first association word).
MARKER_RE = re.compile(
    r"\b(?:CONDOMINIUM|CONDO|CONDOMINIUMS|HOMEOWNERS?|HOME OWNERS?|PROPERTY OWNERS?|"
    r"OWNERS|ASSOCIATION|ASSOC|ASSN|HOA|POA|COA|COMMUNITY ASSOCIATION|MASTER ASSOCIATION|"
    r"RESIDENTS ASSOCIATION|TOWNHOMES?|TOWNHOUSES?|VILLAS? ASSOCIATION|A CONDO|"
    r"COUNCIL OF (?:UNIT|CO) ?OWNERS)\b.*$", re.I)
# District Court locations are named for towns; map them to the county
# (the site's spellings). Circuit courts say "<X> County".
TOWN_COUNTIES = {
    "CIVIL": "Baltimore City", "BALTIMORE CITY": "Baltimore City", "WABASH": "Baltimore City",
    "BORGERDING": "Baltimore City", "HARGROVE": "Baltimore City", "EASTSIDE": "Baltimore City",
    "TOWSON": "Baltimore", "CATONSVILLE": "Baltimore", "ESSEX": "Baltimore",
    "ROCKVILLE": "Montgomery", "SILVER SPRING": "Montgomery",
    "UPPER MARLBORO": "Prince George's", "HYATTSVILLE": "Prince George's",
    "LANDOVER": "Prince George's",
    "GLEN BURNIE": "Anne Arundel", "ANNAPOLIS": "Anne Arundel",
    "BEL-AIR": "Harford", "BEL AIR": "Harford", "CUMBERLAND": "Allegany", "OAKLAND": "Garrett",
    "HAGERSTOWN": "Washington", "WESTMINSTER": "Carroll", "ELLICOTT CITY": "Howard",
    "ELKTON": "Cecil", "DENTON": "Caroline", "CHESTERTOWN": "Kent", "CENTREVILLE": "Queen Anne's",
    "EASTON": "Talbot", "CAMBRIDGE": "Dorchester", "SALISBURY": "Wicomico",
    "PRINCESS ANNE": "Somerset", "SNOW HILL": "Worcester", "OCEAN CITY": "Worcester",
    "LEONARDTOWN": "St. Mary's", "LA PLATA": "Charles", "WALDORF": "Charles",
    "PRINCE FREDERICK": "Calvert", "FREDERICK": "Frederick",
}
COUNTY_SPELLINGS = {"SAINT MARY'S": "St. Mary's", "ST MARY'S": "St. Mary's", "ST. MARY'S": "St. Mary's",
                    "QUEEN ANNES": "Queen Anne's", "PRINCE GEORGES": "Prince George's"}
PARTY_ROLES = {"Plaintiff": "plaintiff", "Defendant": "defendant"}
# The page's own same-origin fetch, run inside the browser tab.
FETCH_JS = """(async () => {
  const r = await fetch(%s, {method: "POST", credentials: "include",
                            headers: {"Content-Type": "application/json",
                                      "Accept": "application/json, text/plain, */*"},
                            body: %s});
  return {status: r.status, retry: r.headers.get("Retry-After") || "", text: await r.text()};
})()"""

log = logging.getLogger(__name__)


def county_of(location: str) -> str:
    """'Howard County District Court' -> Howard; 'Upper Marlboro District
    Court' -> Prince George's; 'Civil District Court' -> Baltimore City."""
    upper = clean(location).upper().replace("’", "'")
    match = re.match(r"^(.*?)\s+COUNTY\s+(?:CIRCUIT|DISTRICT)\s+COURT", upper)
    if match:
        name = match.group(1)
        return COUNTY_SPELLINGS.get(name, name.title().replace("'S", "'s"))
    if "BALTIMORE CITY" in upper:
        return "Baltimore City"
    match = re.match(r"^(.*?)\s+DISTRICT\s+COURT", upper)
    town = re.sub(r"\s+\d+$", "", match.group(1) if match else upper)      # "Silver Spring 02"
    return TOWN_COUNTIES.get(town, "")


def case_url(case_number: str) -> str:
    return f"{SEARCH_URL}case-detail-page?caseId={re.sub(r'[^A-Za-z0-9]', '', case_number)}"


def bare_name(name: str) -> str:
    """The registry name without its entity suffix, c/o tail and leading article."""
    bare = ENTITY_SUFFIX_RE.sub("", CARE_OF_RE.sub("", clean(name))).strip(" ,.")
    return re.sub(r"^(?:THE|A)\s+", "", bare, flags=re.I)


def split_form(party: str) -> tuple[str, str]:
    """(form, rest): the leading Maryland form(s) the party is written in
    and the upper-cased community name left after them, c/o tails and
    entity suffixes removed. form is '' when the party starts with no form."""
    upper = " ".join(re.sub(r"[^A-Z0-9' ]+", " ", bare_name(party).upper()).split())
    rest = upper
    while True:
        stripped = FORM_RE.sub("", rest)
        if stripped == rest:
            break
        rest = stripped
    rest = rest.strip()
    return upper[:len(upper) - len(rest)].strip(), rest


def strip_wrappers(party: str) -> str:
    """The community name inside a party written in a Maryland form."""
    return split_form(party)[1]


def query_for(name: str) -> str:
    """The prefix to search for one association name. A name in a Maryland
    form ("Council of Unit Owners of X") is searched whole; otherwise its
    distinctive core ("Waters Edge At North Lake"), and when the core is a
    single word, the core plus the next word ("Grosvenor Homeowners") so an
    abbreviated "GROSVENOR HOMEOWNERS ASSOC" still matches."""
    bare = bare_name(name)
    if not bare or FORM_RE.match(bare.upper()):
        return bare
    core = MARKER_RE.sub("", bare).strip(" ,.-&")
    words = bare.split()
    if len(core.split()) >= 2 and len(core) >= 6:
        return core
    if len(core.split()) == 1 and len(core) >= 4 and len(words) >= 2:
        return " ".join(words[:2])
    return bare


def match_core(name: str) -> str:
    """What a returned party must carry to count as this association: the
    community name inside a Maryland form, else the distinctive core, else
    the whole name (normalized)."""
    bare = bare_name(name)
    if FORM_RE.match(bare.upper()):
        return normalize(strip_wrappers(bare))
    core = MARKER_RE.sub("", bare).strip(" ,.-&")
    return normalize(core) or normalize(bare)


def known_cores(state: str = STATE) -> set[str]:
    """The distinctive cores of every association held for `state` — what a
    "Board of Directors of X" party must name to count in a sweep."""
    from ..records_miamidade_civil import known_association_norms
    return {core for core in (normalize(MARKER_RE.sub("", name)) for name in known_association_norms(state)) if core}


def community_name(party: str, sweep: bool, known: set[str] | None = None) -> str:
    """The party name to attach the case to, or '' when the party is not a
    community: a bare form with no distinctive words, a business wearing
    the words, or (in a sweep) a government body, lender or trade. A sweep
    keeps a party only when it is written in a Maryland form ("Homeowners
    Association of X", never "Homeowners Loan Corp") or is itself a
    condominium; "Board of Directors of X" is any body's board, so X must
    carry an association word or be a community that is held."""
    name = CARE_OF_RE.sub("", clean(party)).strip(" ,.")
    if not name:
        return ""
    form, rest = split_form(name)
    if not any(not FILLER_RE.match(word) for word in rest.split()):
        return ""
    if has_business_form(name):
        return ""
    if sweep:
        if NOT_COMMUNITY_RE.search(rest):
            return ""
        if not form and not re.match(r"^(?:THE\s+)?CONDOMINIUM", rest):
            return ""
        if form.startswith(BOARD_FORMS) and not MARKER_RE.search(rest) \
                and normalize(MARKER_RE.sub("", rest)) not in (known or set()):
            return ""
    return name


class BrowserTab:
    """One tab of a real Chrome reached over its DevTools port (the `cdp:`
    line in the cookie file). The tab sits on the portal, past "I Agree",
    and runs each search as the page's own same-origin fetch(), so DataDome
    sees the browser it already trusts: its TLS and HTTP/2 fingerprint, its
    cookie kept fresh by the DataDome tag. A refused fetch reloads the page
    once (the device check passes again without a click) and tries again."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")
        try:
            tabs = json.load(urllib.request.urlopen(self.endpoint + "/json", timeout=10))
        except Exception as exc:
            raise PermissionError(f"no Chrome at {self.endpoint} ({exc}) — start one with "
                                  "--remote-debugging-port and open the portal in it") from exc
        page = next((tab for tab in tabs
                     if tab.get("type") == "page" and "casesearch.courts.state.md.us" in tab.get("url", "")), None)
        if page is None:
            request = urllib.request.Request(self.endpoint + "/json/new?" + SEARCH_URL, method="PUT")
            page = json.load(urllib.request.urlopen(request, timeout=10))
        import websocket
        self.socket = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=180)
        self.message_id = 0
        self.ready()

    def call(self, method: str, **params):
        self.message_id += 1
        self.socket.send(json.dumps({"id": self.message_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == self.message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"].get("text") or result["exceptionDetails"])[:200])
        return result.get("result", {}).get("value")

    def ready(self, reload: bool = False) -> None:
        """Have the tab on the portal with the disclaimer accepted."""
        if reload or not str(self.evaluate("location.href") or "").startswith(SEARCH_URL):
            self.call("Page.navigate", url=SEARCH_URL)
        for _ in range(30):
            time.sleep(2)
            text = self.evaluate("document.body ? document.body.innerText : ''") or ""
            if "captcha-delivery" in (self.evaluate("document.documentElement.outerHTML.slice(0, 5000)") or ""):
                raise PermissionError("Case Search shows 'Access is temporarily restricted' to this "
                                      "machine — wait and rerun (the checkpoint resumes)")
            if "I Agree" in text:
                self.evaluate("(() => { const b = [...document.querySelectorAll('button')]"
                              ".find(b => b.textContent.trim() === 'I Agree'); if (b) b.click(); })()")
                time.sleep(2)
                return
            if "Party Type" in text or "/inquiry-search" in str(self.evaluate("location.href") or ""):
                return
        raise PermissionError("the portal did not load in the browser tab")

    def post(self, body: dict) -> tuple[int, str, str]:
        """(status, body text, Retry-After) of one API call made by the page."""
        answer = self.evaluate(FETCH_JS % (json.dumps(API_URL), json.dumps(json.dumps(body))))
        if not isinstance(answer, dict):
            raise RuntimeError("no answer from the browser tab")
        return int(answer.get("status") or 0), answer.get("text") or "", answer.get("retry") or ""


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        if not cookie:
            raise ValueError("md_casesearch needs the datadome cookie from your browser, "
                             "or a `cdp: http://127.0.0.1:<port>` line naming a Chrome to search from")
        cdp = re.search(r"^\s*cdp:\s*(\S+)\s*$", cookie, re.I | re.M)
        self.tab = BrowserTab(cdp.group(1)) if cdp else None
        match = re.search(r"datadome=([^;\s]+)", cookie)
        value = match.group(1) if match else ("" if cdp else cookie.strip().splitlines()[0].strip())
        agent = re.search(r"^\s*user-agent:\s*(\S.*?)\s*$", cookie, re.I | re.M)
        self.pace = pace
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": agent.group(1) if agent else BROWSER_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9", "Content-Type": "application/json",
            "Origin": PORTAL_URL, "Referer": SEARCH_URL + "inquiry-search",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
            "Cookie": "datadome=" + value,
        })
        user_agent = self.session.headers["User-Agent"]
        major = re.search(r"Chrome/(\d+)", user_agent)
        if major:
            version = major.group(1)
            platform = "macOS" if "Macintosh" in user_agent else "Windows" if "Windows" in user_agent else "Linux"
            self.session.headers.update({
                "sec-ch-ua": f'"Google Chrome";v="{version}", "Not_A Brand";v="8", "Chromium";v="{version}"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": f'"{platform}"'})
        self.capped: list[str] = []      # "prefix day" windows that still hit the cap
        self.known: set[str] | None = None
        self.seen: dict[str, list[dict]] = {}   # prefix -> rows (phases share a prefix)

    # -- transport ----------------------------------------------------------------
    def query(self, prefix: str, first: dt.date | None = None, last: dt.date | None = None) -> list[dict]:
        """One API call: the party rows for `prefix` filed in [first, last]."""
        body = {"searchPartyType": "Business", "lastName": "", "firstName": "", "middleName": "",
                "businessName": prefix, "caseType": "CIVIL",
                "rangeFrom": first.isoformat() if first else None,
                "rangeTo": last.isoformat() if last else None, "exactDate": None}
        if self.tab is not None:
            status, text, retry = self.tab.post(body)
            if status == 403:                         # the tag's cookie went stale: reload once
                log.info("  browser fetch refused — reloading the portal tab")
                self.tab.ready(reload=True)
                time.sleep(self.pace)
                status, text, retry = self.tab.post(body)
        else:
            response = self.session.post(API_URL, json=body, timeout=120)
            status, text, retry = response.status_code, response.text, response.headers.get("Retry-After") or ""
        if status == 403:
            raise PermissionError("Case Search answered 403 (DataDome) — copy a fresh datadome "
                                  "cookie from your browser into the cookie file and rerun")
        if status == 429:
            raise RateLimited(int(retry or 600))
        try:
            answer = json.loads(text) if text else None
        except ValueError:
            answer = None
        if status == 400:
            if isinstance(answer, dict) and str(answer.get("code")) == NO_CASES_CODE:
                return []
            detail = answer.get("error") if isinstance(answer, dict) else text
            raise RuntimeError(f"Case Search 400: {str(detail)[:160]}")
        if status >= 400:
            raise RuntimeError(f"Case Search HTTP {status}: {text[:120]}")
        return answer if isinstance(answer, list) else []

    def result_rows(self, prefix: str, first: dt.date, last: dt.date, depth: int = 0) -> list[dict]:
        """Every party row for `prefix` filed in [first, last], the window
        halved whenever an answer hits the 600-row cap."""
        time.sleep(self.pace)
        rows = self.query(prefix, first, last)
        if len(rows) < ROW_CAP or first >= last or depth > MAX_SPLIT_DEPTH:
            if len(rows) >= ROW_CAP:
                self.capped.append(f"{prefix} {first.isoformat()}")
                log.warning("  %r still %d rows on %s — some cases that day are missed", prefix, ROW_CAP, first)
            return rows
        middle = first + (last - first) // 2
        return (self.result_rows(prefix, first, middle, depth + 1)
                + self.result_rows(prefix, middle + dt.timedelta(days=1), last, depth + 1))

    # -- one query ----------------------------------------------------------------
    def search(self, name: str) -> list[dict]:
        sweep = name.lower() in SWEEPS
        prefix = name if sweep else query_for(name)
        if not prefix:
            return []
        if sweep and self.known is None:
            self.known = known_cores()
        key = prefix.upper()
        if key not in self.seen:
            self.seen[key] = self.result_rows(prefix, FIRST_DAY, dt.date.today())
        return shape(self.seen[key], name, "" if sweep else match_core(name), sweep, self.known)


def shape(rows: list[dict], queried: str, core: str, sweep: bool, known: set[str] | None = None) -> list[dict]:
    """Party rows -> one docket record per case naming the community."""
    by_case: dict[str, dict] = {}
    for row in rows:
        case_number = clean(row.get("caseNumber") or "")
        party = clean(row.get("fullName") or "")
        if not case_number or not party:
            continue
        if sweep:
            association = community_name(party, sweep=True, known=known)
            if association and not looks_like_association(association):
                association = ""
        else:
            association = community_name(party, sweep=False) if core and party_is_association(party, core) else ""
        if not association:
            continue
        role = PARTY_ROLES.get(row.get("partyTypeDisplay") or "", "other")
        case = by_case.get(case_number)
        if case is None:
            by_case[case_number] = record(
                key=KEY, state=STATE, case_name=clean(row.get("title") or "") or association,
                court=clean(row.get("locationName") or ""), docket_number=case_number,
                date_filed=row.get("filingDate") or "", nature_of_suit=row.get("caseType") or "",
                status=row.get("caseStatus") or "", associations=[association], url=case_url(case_number),
                case_id=case_number, queried=queried, association_role=[role],
                county=county_of(row.get("locationName") or "")).as_row()
        else:
            if association not in case["associations"]:
                case["associations"].append(association)
            if role not in case["association_role"]:
                case["association_role"].append(role)
    return list(by_case.values())
