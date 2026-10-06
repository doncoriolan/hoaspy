"""Virginia General District Courts — Online Case Information System, civil
name search, through a local browser.

    https://eapps.courts.state.va.us/gdcourts/

The general district courts hear civil claims up to $25,000 — the tier
where associations sue owners for unpaid assessments (warrants in debt,
then garnishments on the judgment). One court per city or county; the
portal makes you search **one court at a time**, so every name is asked
of every civil court (~127 requests per name before paging).

Access: the per-session image captcha is gone (build 6.4.1.2). The site
sits behind a Cloudflare challenge that a real browser passes on its own,
then a terms page (a disclaimer; nothing in it restricts automated use)
with an Accept button. So this adapter keeps one tab open in its own
browser context over the DevTools port (hoaspy/lib/browser.py), clicks
Accept once, and issues every search as a same-origin fetch() from that
tab — plain requests never get past Cloudflare. If the terms page ever
shows a verification-code field again the run stops (PermissionError):
captchas are not solved.

Search semantics: "Last/Business Name" is a STARTS-WITH match on either
party as the clerk typed it (30 characters at most), sorted by name, 20
rows a page, paged with Next until the prefix is exhausted; Data Status
"All". Clerks abbreviate and misspell ("MONTCLAIR PROERTY OWNERS
ASSOCIAITON", "LAKE MONTICELLO OWNER'S ASN"), so the query is the
distinctive core of the name and only parties that pass `party_matches`
are kept. When a prefix floods ("VILLAGE GREEN" also returns the
apartments' evictions) the search is restarted one level narrower — core
plus the first letter of the association word, then core plus its first
four letters and the HOA/POA acronym (`query_levels`).

One record per case: the portal lists every action on a judgment as its
own row (GV23011788-00 warrant in debt, -01 garnishment, …); rows sharing
the base number are folded into one record with an `actions` list. The
filing date and judgment amounts come from the case-detail page of the
original (-00) action, one extra request per case; when only later
actions are still online `date_filed` is empty and `filed_year` (from the
case number) stands in. Set HOASPY_VA_GDC_DETAILS=0 to skip the detail
requests and HOASPY_VA_GDC_COURTS=059,153 to limit the run to some courts
(FIPS codes).

Best-effort by construction: a prefix match on the registered spelling,
the caption shows one plaintiff and one defendant, and **the same
association name can exist in several counties** — the court is recorded
on every case, nothing ties a case to the queried association beyond its
name. No per-case public deep link (the detail URL is session-bound):
`url` is the portal entry page and `docket_number` re-enters the case
under that court's Case Number Search.
"""

from __future__ import annotations

import atexit
import html
import json
import logging
import os
import re
import time
from datetime import date
from difflib import SequenceMatcher

from hoaspy.lib.browser import BrowserContext, DevTools, Tab

from . import RateLimited
from ._common import ASSOC_MARK_RE, clean, has_business_form, iso_date, normalize, record

STATE = "VA"
KEY = "va_gdc"
NEEDS_COOKIE = False
PORTAL_URL = "https://eapps.courts.state.va.us/gdcourts/"
SEARCH_PATH = "/gdcourts/nameSearch.do"
INFO = {
    "name": "Virginia General District Courts — Online Case Information System (civil name search)",
    "url": PORTAL_URL,
    "access": "anonymous; Cloudflare challenge passed by a local browser, "
              "terms page accepted once per session; per-association name search in every court",
    "coverage": "all Virginia general district courts, civil division (claims up to $25,000); "
                "cases the courts still hold online",
    "caveat": "best-effort — starts-with match on the core of our name, court by court; "
              "same-named associations in different counties are not told apart (see the court "
              "on each case); circuit-court civil cases are a different portal; no per-case deep link",
}
MAX_QUERY = 30                 # the portal's name field takes this many characters
PAGE_ROWS = 20
FLOOD_PAGES = (3, 10)          # still paging after this many pages -> the next query level
MAX_PAGES = 500                # the hard stop per court search (10,000 rows)
SESSION_RETRIES = 3

ENTITY_SUFFIX_RE = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
# The association word(s) that end the distinctive core of a name.
KIND_RE = re.compile(
    r"\b(?:HOME ?OWNERS?|HOMES|PROPERTY ?OWNERS?\w*|LOT OWNERS?|LAND ?OWNERS?|UNIT OWNERS?|"
    r"OWNERS?|RESIDENTS?|RESIDENTIAL|TENANTS?|COMMUNITY|CIVIC|CITIZENS?|CONDOMINIUMS?|CONDO|"
    r"TOWNHOUSES?|TOWNHOMES?|CONSERVANCY|ASSOCIATION\w*|ASSOC|ASSN|HOA|POA|COA|COUNCIL)\b.*$", re.I)
ASSOCIATION_ABBREVIATIONS = {"ASSN", "ASN", "ASSO", "ASSOC", "ASSC", "ASSOCS"}
# The kinds of association a party or a name can say it is.
KINDS = (
    ("condo", re.compile(r"\bCONDO\w*|\bUNIT OWNERS?\b")),
    ("civic", re.compile(r"\bCIVIC\b|\bCITIZENS?\b")),
    ("home", re.compile(r"\bHOME ?OWNERS?\b|\bHOA\b|\bHOMES\b")),
    ("property", re.compile(r"\bPROP\w*|\bPRO\w*TY\b|\bPOA\b|\bLOT OWNERS?\b|\bLAND ?OWNERS?\b")),
    ("community", re.compile(r"\bCOMM\b|\bCOMM[UI]N\w*")),        # not COMMONS
    ("town", re.compile(r"\bTOWN ?HO\w+|\bTH\b")),
    ("residents", re.compile(r"\bRESIDEN\w+|\bTENANTS?\b")),
)
KIND_WORDS = (("home", "HOMEOWNERS"), ("property", "PROPERTY"), ("community", "COMMUNITY"),
              ("town", "TOWNHOUSE"), ("condo", "CONDOMINIUM"), ("residents", "RESIDENTS"))
NOISE_RE = re.compile(r"[^A-Z0-9 ]+")
# Words that say "this is an association" without saying which one;
# anything else after the core ("… Property Owners VOLUNTEER RESCUE SQUAD")
# is part of who the organisation is and must be in the party too.
GENERIC_WORDS = {"ASSOCIATION", "ASSOCIATIONS", "HOMEOWNERS", "HOMEOWNER", "HOME", "HOMES", "OWNERS", "OWNER",
                 "PROPERTY", "COMMUNITY", "RESIDENTS", "RESIDENT", "RESIDENTIAL", "CONDOMINIUM", "CONDOMINIUMS",
                 "CONDO", "TOWNHOUSE", "TOWNHOUSES", "TOWNHOME", "TOWNHOMES", "CIVIC", "CITIZENS",
                 "UNIT", "LOT", "LAND", "LANDOWNERS", "TENANT", "TENANTS", "HOA", "POA", "COA", "CONSERVANCY",
                 "VIRGINIA", "VA", "IN"}
NOT_CIVIL_RE = re.compile(r"Criminal|Traffic", re.I)
ROW_RE = re.compile(r'<tr class="(?:even|odd)Row">(.*?)</tr>', re.S)
CELL_RE = re.compile(r'<td class="gridrow">(.*?)</td>', re.S)
LABEL_RE = re.compile(r"<td[^>]*>\s*([^<>]*?)(?:&nbsp;|\s)*:\s*</td>\s*<td[^>]*>(.*?)</td>", re.S)
JUDGMENT_STATUS = {"Plaintiff": "Judgment for plaintiff", "Defendant": "Judgment for defendant"}
JUDGMENT_FIELDS = ("Judgment", "Principal Amount", "Costs", "Attorney Fees", "Other Amount", "Interest Award",
                   "Is Judgment Satisfied", "Date Satisfaction Filed")
CASE_NUMBER_RE = re.compile(r"[A-Z]{2}\d{8}-\d{2}$")
# A same-origin fetch run inside the tab; the page's own session cookies ride along.
FETCH_JS = ("(async()=>{try{const r=await fetch(%s,%s);const t=await r.text();"
            "return JSON.stringify({s:r.status,t:t})}catch(e){return JSON.stringify({s:0,t:String(e)})}})()")
# Where the tab is: the title, whether the terms page's Accept button is
# shown, and whether a verification-code field is.
STATE_JS = ("document.title+'|'+(document.querySelector('input[name=accept]')?'ACCEPT':'')+'|'+"
            "(document.querySelector('form input[type=text],form img,form iframe')?'CODE':'')")
BLOCKED_STATUSES = (403, 429, 503)

log = logging.getLogger(__name__)


def html_text(fragment: str) -> str:
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " "))


# -- names ----------------------------------------------------------------------

def whole_name(name: str) -> str:
    """The registered name without its entity suffix, a leading THE, or the
    care-of text the IRS file runs on after INC ("… ASSOCIATION INC RALPH L FEIL")."""
    bare = re.sub(r"\s(?:INC|INCORPORATED)\s.+$", "", clean(name).upper())
    return re.sub(r"^THE\s+", "", ENTITY_SUFFIX_RE.sub("", bare).strip(" ,."))


def name_core(name: str) -> tuple[str, str]:
    """('LAKE MONTICELLO', 'OWNERS ASSOCIATION'): the distinctive leading
    words of a registered name and what follows them. The core is '' when
    the name starts with its association words ("Residents Association of …")."""
    whole = whole_name(name)
    core = KIND_RE.sub("", whole).strip(" ,.-&")
    return core, whole[len(core):].strip(" ,.-&")


def query_levels(name: str) -> list[list[str]]:
    """The prefixes to search, widest first; each level is tried only when
    the one before it floods. A core of two or more words goes in bare,
    then (and a single-word core from the start) with the first letter of
    the association word — "MONTCLAIR P" finds PROPERTY/PROERTY/POA without
    paging through every Montclair business — then with its first four
    letters plus the HOA/POA acronym. Names with no usable core go in whole."""
    core, rest = name_core(name)
    words = core.split()
    if not words or (len(words) == 1 and len(core) < 4) or not rest:
        return [[(core if words and not rest else whole_name(name))[:MAX_QUERY].rstrip()]]
    kind = rest.split()[0]
    narrow = [f"{core} {kind[:4]}"]
    if kind.startswith("HOME"):
        narrow.append(f"{core} HOA")
    elif kind.startswith("PROP"):
        narrow.append(f"{core} POA")
    levels = [[core]] if len(words) > 1 else []
    levels += [[f"{core} {kind[0]}"], narrow]
    out = []
    for level in levels:
        level = list(dict.fromkeys(query[:MAX_QUERY].rstrip() for query in level))
        if level not in out:
            out.append(level)
    return out


def query_name(name: str) -> str:
    """The first (widest) prefix searched for `name`."""
    return query_levels(name)[0][0]


def association_word(token: str) -> str:
    """ASSN, ASN, ASSOC, ASOOCIATION, ASSCIATION, ASSOCIAITON … -> ASSOCIATION."""
    if token in ASSOCIATION_ABBREVIATIONS or (token.startswith("AS") and len(token) >= 8
                                              and not token.startswith("ASSOCIATE")
                                              and SequenceMatcher(None, token, "ASSOCIATION").ratio() >= 0.8):
        return "ASSOCIATION"
    return token


def canonical(party: str) -> str:
    """An upper-case caption party with apostrophes closed up and the
    clerks' spellings of 'association' made uniform."""
    text = re.sub(r"['’`]", "", clean(party).upper())
    text = re.sub(r"\((?:GARNISHEE|TENANT[^)]*)\)", " ", text)
    return re.sub(r"[A-Z]+", lambda match: association_word(match.group(0)), text)


def kinds_of(text: str) -> set[str]:
    """The kinds of association named in a party: by word, and by a clerk's
    misspelling of the word ("PEOPERTY", "HOWEOWNERS", "COMMUNNITY")."""
    found = {kind for kind, pattern in KINDS if pattern.search(text)}
    for token in text.split():
        if len(token) >= 6:
            found |= {kind for kind, word in KIND_WORDS if SequenceMatcher(None, token, word).ratio() >= 0.8}
    return found


def alike(first: str, second: str) -> bool:
    """The same word allowing an abbreviation or a clerk's typo."""
    return (len(second) >= 3 and (first.startswith(second) or second.startswith(first))) \
        or SequenceMatcher(None, first, second).ratio() >= 0.75


def distinct_words(tokens: list[str]) -> list[str]:
    return [token for token in tokens if token not in GENERIC_WORDS and not kinds_of(token)]


def party_matches(party: str, name: str) -> bool:
    """True when a caption party is plausibly the association `name`: it
    starts with the name's core, is not a business, and either is the name
    itself (allowing clerk truncation) or carries an association word of
    the same kind (homeowners, property owners, townhouse, condominium,
    community …), with nothing but our own next word or an association
    word right after the core ("MONTCLAIR PROERTY …" yes, "MONTCLAIR PARK
    HOA" no)."""
    core, _ = name_core(name)
    party_text = canonical(party)
    party_key, name_key = normalize(party_text), normalize(canonical(whole_name(name)))
    core_key = normalize(core) or name_key
    if not party_key or not (party_key == core_key or party_key.startswith(core_key + " ")):
        return False
    if NOISE_RE.sub(" ", party_text).split() == NOISE_RE.sub(" ", canonical(name)).split():
        return True                       # letter for letter, whatever its form
    if has_business_form(party_text):
        return False
    if party_key == name_key:
        return True
    party_tokens, name_tokens, core_tokens = party_key.split(), name_key.split(), core_key.split()
    if len(party_tokens) > len(core_tokens) and party_key.startswith(name_key + " "):
        return True                       # ours plus a suffix
    if len(party_tokens) > len(core_tokens) and name_key.startswith(party_key + " ") and (
            len(party_text) >= 40 or not distinct_words(name_tokens[len(party_tokens):])):
        return True                       # cut short by the clerk or the field
    if not ASSOC_MARK_RE.search(party_key):
        return False
    party_kinds, name_kinds = kinds_of(party_key), kinds_of(name_key)
    if name_kinds and not (party_kinds & name_kinds):
        return False                      # Sugarland Run Townhouse Owners is not the Homeowners Association
    for wanted in distinct_words(name_tokens[len(core_tokens):]):
        if not any(alike(wanted, token) for token in party_tokens[len(core_tokens):]):
            return False                  # the POA is not its "… Volunteer Rescue Squad"
    if len(party_tokens) > len(core_tokens) and len(name_tokens) > len(core_tokens):
        # The word after the core must be ours (however the clerk spelt it) or
        # an association word: "COURTHOUSE GREEN FIRST HOMES ASSOCIATION" is a
        # neighbour of "Courthouse Green Property Owners Association", not it.
        ours, theirs = name_tokens[len(core_tokens)], party_tokens[len(core_tokens)]
        theirs_kinds = kinds_of(theirs)
        if not (alike(ours, theirs) or (theirs_kinds & name_kinds)
                or (len(core_tokens) > 1 and theirs in ("ASSOCIATION", "OWNERS", "OWNER"))):
            return False
    return True


# -- parsing --------------------------------------------------------------------

def parse_courts(page_html: str) -> list[tuple[str, str]]:
    """[(fips, court name)] for every court with a civil docket, from the
    hidden courtName/courtFips inputs behind the court drop-down."""
    names = [html.unescape(value) for value in re.findall(r'name="courtName" value="([^"]*)"', page_html)]
    fips = re.findall(r'name="courtFips" value="([^"]*)"', page_html)
    return [(code, clean(name)) for code, name in zip(fips, names, strict=False) if not NOT_CIVIL_RE.search(name)]


def parse_results(page_html: str) -> dict:
    """The rows of one result page plus what the Next request needs."""
    rows = []
    for row_html in ROW_RE.findall(page_html):
        cells = [html_text(cell) for cell in CELL_RE.findall(row_html)]
        if len(cells) < 8 or not CASE_NUMBER_RE.match(cells[1]):
            continue
        rows.append({"number": cells[1], "plaintiff": cells[2], "defendant": cells[3],
                     "hearing_date": iso_date(cells[4]), "result": cells[6], "type": cells[7]})

    def hidden(name: str) -> str:
        match = re.search(rf'name="{name}" value="([^"]*)"', page_html)
        return html.unescape(match.group(1)) if match else ""

    counter = re.search(r"var searchCounter=(\d+)", page_html)
    return {"rows": rows, "next": 'value="Next"' in page_html, "counter": counter.group(1) if counter else "0",
            "cursor": {key: hidden(key) for key in ("firstRowName", "firstRowCaseNumber",
                                                    "lastRowName", "lastRowCaseNumber")}}


def parse_detail(page_html: str) -> dict:
    """The filed date and the judgment block of a civil case-detail page."""
    main = re.search(r"<main>(.*?)</main>", page_html, re.S)
    body = re.sub(r"<script.*?</script>", " ", main.group(1) if main else page_html, flags=re.S)
    fields = {}
    for label, value in LABEL_RE.findall(body):
        label, value = html_text(label), html_text(value)
        if label and value and label not in fields:
            fields[label] = value
    judgment = {key: fields[key] for key in JUDGMENT_FIELDS if fields.get(key)}
    return {"number": fields.get("Case Number", ""), "date_filed": iso_date(fields.get("Filed Date", "")),
            "type": fields.get("Case Type", ""), "judgment": judgment}


def filed_year(number: str) -> int | None:
    """GV23011788-00 -> 2023 (the two digits after the prefix are the filing year)."""
    match = re.match(r"[A-Z]{2}(\d{2})\d{6}", number or "")
    if not match:
        return None
    short = int(match.group(1))
    return (2000 if short <= date.today().year % 100 + 1 else 1900) + short


def build_records(name: str, fips: str, court: str, rows: list[dict],
                  details: dict[str, dict] | None = None) -> list[dict]:
    """Fold result rows into one record per base case number, keeping only
    cases where a party passes party_matches(name)."""
    cases: dict[str, list[dict]] = {}
    for row in rows:
        cases.setdefault(row["number"].split("-")[0], []).append(row)
    out = []
    for base, actions in cases.items():
        actions.sort(key=lambda row: row["number"])
        matched, roles = [], []
        for row in actions:
            for role in ("plaintiff", "defendant"):
                if party_matches(row[role], name):
                    if row[role] not in matched:
                        matched.append(row[role])
                    if role not in roles:
                        roles.append(role)
        if not matched:
            continue
        lead = actions[0]
        detail = (details or {}).get(lead["number"]) or {}
        columns: dict = {
            "association_role": roles, "court_fips": fips, "filed_year": filed_year(lead["number"]),
            "hearing_date": max((row["hearing_date"] for row in actions), default=""),
            "actions": [{"number": row["number"], "type": row["type"], "hearing_date": row["hearing_date"],
                         "result": row["result"]} for row in actions],
        }
        if detail.get("judgment"):
            columns["judgment"] = detail["judgment"]
        out.append(record(
            key=KEY, state=STATE, case_name=f'{lead["plaintiff"]} v. {lead["defendant"]}', court=court,
            docket_number=lead["number"], date_filed=detail.get("date_filed", ""), nature_of_suit=lead["type"],
            status=JUDGMENT_STATUS.get(lead["result"], lead["result"]), associations=matched, url=PORTAL_URL,
            case_id=f"{fips}-{base}", queried=name, **columns).as_row())
    return out


# -- the browser session ----------------------------------------------------------

class SessionLost(Exception):
    """The portal session timed out; the caller reopens and redoes the court."""


class Portal:
    """One tab in its own browser context, past Cloudflare and the terms page."""

    def __init__(self):
        self.devtools: DevTools | None = None
        self.context: BrowserContext | None = None
        self.tab: Tab | None = None
        self.courts: list[tuple[str, str]] = []

    def open(self) -> None:
        self.close()
        self.devtools = DevTools()
        self.context = self.devtools.context()
        self.tab = self.context.open_tab(PORTAL_URL)
        state, accepted, deadline = "", False, time.time() + 90
        while time.time() < deadline:
            time.sleep(2)
            state = self.tab.evaluate(STATE_JS, timeout=10) or ""
            if "Welcome Page" in state:
                break
            if "|ACCEPT|" in state and not accepted:
                if state.endswith("|CODE"):
                    raise PermissionError("the portal's terms page is asking for a verification "
                                          "code again — captchas are not solved by design")
                self.tab.evaluate("document.querySelector('input[name=accept]').click()", timeout=10)
                accepted = True
        else:
            raise PermissionError(f"could not get past the portal's entry pages (last state: {state!r}); "
                                  "is the browser able to pass the Cloudflare check?")
        self.courts = parse_courts(self.tab.evaluate("document.documentElement.outerHTML", timeout=20) or "")
        if not self.courts:
            raise RuntimeError("no court list on the welcome page — portal layout changed?")

    def close(self) -> None:
        if self.context is not None:
            self.context.dispose()
        if self.devtools is not None:
            self.devtools.close()
        self.devtools = self.context = self.tab = None

    def fetch(self, path: str, params: dict | None = None) -> str:
        """GET (or POST `params` to) `path` from inside the tab."""
        options = "{credentials:'include'}" if params is None else (
            "{method:'POST',credentials:'include',headers:{'Content-Type':"
            "'application/x-www-form-urlencoded'},body:new URLSearchParams(" + json.dumps(params) + ").toString()}")
        raw = self.tab.evaluate(FETCH_JS % (json.dumps(path), options), timeout=60, await_promise=True)
        try:
            answer = json.loads(raw)
        except (TypeError, ValueError):
            answer = {"s": 0, "t": ""}
        status, text = answer["s"], answer["t"]
        if status in BLOCKED_STATUSES or "Just a moment" in text[:2000] or "cf-chl" in text[:5000]:
            raise RateLimited(900)
        if status != 200 or "Terms and Conditions Page" in text or "unauthorized.html" in text[:600]:
            raise SessionLost(f"status {status}")
        return text


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.portal = Portal()
        atexit.register(self.portal.close)           # do not leave the tab open in the browser
        self.details = os.environ.get("HOASPY_VA_GDC_DETAILS", "1") != "0"
        self.courts_only = {code.strip() for code in os.environ.get("HOASPY_VA_GDC_COURTS", "").split(",")
                            if code.strip()}
        self.counter = "0"

    def post_search(self, fips: str, query: str, action: str, cursor: dict | None = None) -> dict:
        """One name-search page in one court: a new search, or the next page of the last one."""
        params = {"formAction": action, "displayCaseNumber": "", "formBean": "", "localFipsCode": fips,
                  "caseActive": "", "localLastName": "", "forward": "", "back": "",
                  "localnamesearchlastName": query, "lastName": query,
                  "localnamesearchfirstName": "", "firstName": "", "localnamesearchmiddleName": "",
                  "middleName": "", "localnamesearchsuffix": "", "suffix": "",
                  "localnamesearchsearchCategory": "O", "searchCategory": "O", "searchFipsCode": fips,
                  "searchDivision": "V", "searchType": "name", "clientSearchCounter": self.counter}
        if cursor:
            params.update(cursor, unCheckedCases="")
        page = parse_results(self.portal.fetch(SEARCH_PATH, params))
        self.counter = page["counter"]
        time.sleep(self.pace)
        return page

    def court_rows(self, fips: str, name: str) -> list[dict]:
        """Every result row for `name` in one court. A level whose search is
        still paging after its FLOOD_PAGES limit is abandoned for the next,
        narrower level — unless most of what came back is the association
        itself (a busy plaintiff, not noise), in which case it is paged out."""
        levels = query_levels(name)
        for i, queries in enumerate(levels):
            last_level = i == len(levels) - 1
            limit = MAX_PAGES if last_level else FLOOD_PAGES[min(i, len(FLOOD_PAGES) - 1)]
            rows, flooded = [], False
            for query in queries:
                page = self.post_search(fips, query, "newSearch")
                got, pages = list(page["rows"]), 1
                while page["next"] and page["rows"] and pages < limit:
                    page = self.post_search(fips, query, "next", page["cursor"])
                    got += page["rows"]
                    pages += 1
                    if pages == limit and not last_level and page["next"] and 2 * sum(
                            party_matches(row["plaintiff"], name) or party_matches(row["defendant"], name)
                            for row in got) >= len(got):
                        limit = MAX_PAGES
                rows += got
                if page["next"] and page["rows"]:
                    if limit == MAX_PAGES:
                        log.warning("va_gdc: %r in court %s still paging after %d pages — truncated",
                                    query, fips, MAX_PAGES)
                    else:
                        flooded = True
                        break
            if not flooded:
                return rows
        return []

    def detail(self, fips: str, number: str) -> dict:
        text = self.portal.fetch(f"{SEARCH_PATH}?formAction=caseDetails&displayCaseNumber={number}"
                                 f"&localFipsCode={fips}&caseActive=true&clientSearchCounter={self.counter}")
        time.sleep(self.pace)
        detail = parse_detail(text)
        return detail if detail["number"] == number else {}

    def court_cases(self, fips: str, court: str, name: str) -> list[dict]:
        rows = self.court_rows(fips, name)
        found = build_records(name, fips, court, rows) if rows else []
        if not found or not self.details:
            return found
        details = {case["docket_number"]: self.detail(fips, case["docket_number"])
                   for case in found if case["docket_number"].endswith("-00")}
        return build_records(name, fips, court, rows, details)

    def search(self, name: str) -> list[dict]:
        if not self.portal.devtools:
            self.portal.open()
        out = []
        for fips, court in list(self.portal.courts):
            if self.courts_only and fips not in self.courts_only:
                continue
            for attempt in range(1, SESSION_RETRIES + 1):
                try:
                    out += self.court_cases(fips, court, name)
                    break
                except SessionLost as exc:      # timed out: a new session, this court again
                    log.info("va_gdc: session lost in court %s (%s) — reopening", fips, exc)
                    if attempt == SESSION_RETRIES:
                        raise RuntimeError(f"portal session keeps dropping in court {fips}") from exc
                    time.sleep(5 * attempt)
                    self.portal.open()
                    self.counter = "0"
        return out
