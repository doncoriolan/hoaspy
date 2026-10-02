"""Virginia General District Courts — Online Case Information System, civil
name search, driven through the local BrowserOS instance.

    https://eapps.courts.state.va.us/gdcourts/

The general district courts hear civil claims up to $25,000 — the tier where
associations sue owners for unpaid assessments (warrants in debt, then
garnishments on the judgment). One court per city/county; the portal makes
you search **one court at a time**, so every name is asked of every civil
court (~127 requests per name before paging).

Access (re-checked 2026-09-30, build 6.4.1.2): the per-session image captcha
is gone. The site sits behind a Cloudflare challenge that a real browser
passes on its own, then a terms page (a disclaimer; nothing in it restricts
automated use) with an Accept button. So this adapter keeps one tab open in
its own BrowserOS browser context over CDP (127.0.0.1:9100), clicks Accept
once, and issues every search as a same-origin fetch() from that tab — plain
`requests` never gets past Cloudflare. If the terms page ever shows a
verification-code field again the run stops (PermissionError): we do not
solve captchas.

Search semantics: "Last/Business Name" is a STARTS-WITH match on either
party as the clerk typed it (max 30 chars), sorted by name, 20 rows a page,
paged with Next until the prefix is exhausted; Data Status "All". Clerks
abbreviate and misspell ("MONTCLAIR PROERTY OWNERS ASSOCIAITON", "LAKE
MONTICELLO OWNER'S ASN"), so we query the distinctive core of the name and
keep only parties that pass `party_matches`. When a prefix floods ("VILLAGE
GREEN" also returns the apartments' evictions) the search is restarted one
level narrower — core + first letter of the association word, then core +
its first four letters and the HOA/POA acronym (`query_levels`).

One record per case: the portal lists every action on a judgment as its own
row (GV23011788-00 warrant in debt, -01 garnishment, …); rows sharing the
base number are folded into one record with an `actions` list. The filing
date and judgment amounts come from the case-detail page of the original
(-00) action, one extra request per case; when only later actions are still
online `date_filed` is empty and `filed_year` (from the case number) stands
in. Set HOASPY_VA_GDC_DETAILS=0 to skip the detail requests and
HOASPY_VA_GDC_COURTS=059,153 to limit the run to some courts (FIPS codes).

Best-effort by construction: prefix match on our spelling, the caption shows
one plaintiff and one defendant, and **the same association name can exist
in several counties** — the court is recorded on every case, nothing ties a
case to the queried association beyond its name. No per-case public deep
link (the detail URL is session-bound): `url` is the portal entry page and
`docket_number` re-enters the case under that court's Case Number Search.
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

from . import RateLimited
from ._common import ASSOC_MARK_RE, clean, has_business_form, iso_date, normalize, record

STATE = "VA"
KEY = "va_gdc"
NEEDS_COOKIE = False
BASE = "https://eapps.courts.state.va.us/gdcourts/"
INFO = {
    "name": "Virginia General District Courts — Online Case Information System (civil name search)",
    "url": BASE,
    "access": "anonymous; Cloudflare challenge passed by the local BrowserOS browser, "
              "terms page accepted once per session; per-association name search in every court",
    "coverage": "all Virginia general district courts, civil division (claims up to $25,000); "
                "cases the courts still hold online",
    "caveat": "best-effort — starts-with match on the core of our name, court by court; "
              "same-named associations in different counties are not told apart (see the court "
              "on each case); circuit-court civil cases are a different portal; no per-case deep link",
}
MAX_QUERY = 30                 # maxlength of the portal's name field
PAGE_ROWS = 20
FLOOD_PAGES = (3, 10)          # still paging after this many pages -> next query level
MAX_PAGES = 500                # hard stop per court search (10,000 rows)
log = logging.getLogger("state_courts")

_SUFFIX = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
# The association word(s) that end the distinctive core of a name.
_KIND = re.compile(
    r"\b(?:HOME ?OWNERS?|HOMES|PROPERTY ?OWNERS?\w*|LOT OWNERS?|LAND ?OWNERS?|UNIT OWNERS?|"
    r"OWNERS?|RESIDENTS?|RESIDENTIAL|TENANTS?|COMMUNITY|CIVIC|CITIZENS?|CONDOMINIUMS?|CONDO|"
    r"TOWNHOUSES?|TOWNHOMES?|CONSERVANCY|ASSOCIATION\w*|ASSOC|ASSN|HOA|POA|COA|COUNCIL)\b.*$", re.I)
_ASSOC_ABBR = {"ASSN", "ASN", "ASSO", "ASSOC", "ASSC", "ASSOCS"}
_KINDS = (
    ("condo", re.compile(r"\bCONDO\w*|\bUNIT OWNERS?\b")),
    ("civic", re.compile(r"\bCIVIC\b|\bCITIZENS?\b")),
    ("home", re.compile(r"\bHOME ?OWNERS?\b|\bHOA\b|\bHOMES\b")),
    ("property", re.compile(r"\bPROP\w*|\bPRO\w*TY\b|\bPOA\b|\bLOT OWNERS?\b|\bLAND ?OWNERS?\b")),
    ("community", re.compile(r"\bCOMM\b|\bCOMM[UI]N\w*")),        # not COMMONS
    ("town", re.compile(r"\bTOWN ?HO\w+|\bTH\b")),
    ("residents", re.compile(r"\bRESIDEN\w+|\bTENANTS?\b")),
)
_KIND_WORDS = (("home", "HOMEOWNERS"), ("property", "PROPERTY"), ("community", "COMMUNITY"),
               ("town", "TOWNHOUSE"), ("condo", "CONDOMINIUM"), ("residents", "RESIDENTS"))
_NOISE = re.compile(r"[^A-Z0-9 ]+")
# Words that say "this is an association" without saying which one; anything
# else after the core ("… Property Owners VOLUNTEER RESCUE SQUAD") is part of
# who the organisation is and must be in the party too.
_GENERIC = {"ASSOCIATION", "ASSOCIATIONS", "HOMEOWNERS", "HOMEOWNER", "HOME", "HOMES", "OWNERS", "OWNER",
            "PROPERTY", "COMMUNITY", "RESIDENTS", "RESIDENT", "RESIDENTIAL", "CONDOMINIUM", "CONDOMINIUMS",
            "CONDO", "TOWNHOUSE", "TOWNHOUSES", "TOWNHOME", "TOWNHOMES", "CIVIC", "CITIZENS",
            "UNIT", "LOT", "LAND", "LANDOWNERS", "TENANT", "TENANTS", "HOA", "POA", "COA", "CONSERVANCY",
            "VIRGINIA", "VA", "IN"}
_NOT_CIVIL = re.compile(r"Criminal|Traffic", re.I)
_ROW = re.compile(r'<tr class="(?:even|odd)Row">(.*?)</tr>', re.S)
_CELL = re.compile(r'<td class="gridrow">(.*?)</td>', re.S)
_LABEL = re.compile(r"<td[^>]*>\s*([^<>]*?)(?:&nbsp;|\s)*:\s*</td>\s*<td[^>]*>(.*?)</td>", re.S)
_STATUS = {"Plaintiff": "Judgment for plaintiff", "Defendant": "Judgment for defendant"}
_JS_FETCH = ("(async()=>{try{const r=await fetch(%s,%s);const t=await r.text();"
             "return JSON.stringify({s:r.status,t:t})}catch(e){return JSON.stringify({s:0,t:String(e)})}})()")
_JS_STATE = ("document.title+'|'+(document.querySelector('input[name=accept]')?'ACCEPT':'')+'|'+"
             "(document.querySelector('form input[type=text],form img,form iframe')?'CODE':'')")


def _text(h: str) -> str:
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\xa0", " "))


# -- names ---------------------------------------------------------------------
def name_core(name: str) -> tuple[str, str]:
    """('LAKE MONTICELLO', 'OWNERS ASSOCIATION'): the distinctive leading words
    of a registered name and what follows them. The core is '' when the name
    starts with its association words ("Residents Association of …")."""
    n = _whole(name)
    core = _KIND.sub("", n).strip(" ,.-&")
    return core, n[len(core):].strip(" ,.-&")


def _whole(name: str) -> str:
    """Registered name without its entity suffix, a leading THE, or the
    care-of text the IRS file runs on after INC ("… ASSOCIATION INC RALPH L FEIL")."""
    n = re.sub(r"\s(?:INC|INCORPORATED)\s.+$", "", clean(name).upper())
    return re.sub(r"^THE\s+", "", _SUFFIX.sub("", n).strip(" ,."))


def query_levels(name: str) -> list[list[str]]:
    """Prefixes to search, widest first; each level is tried only when the
    one before it floods. A core of two or more words goes in bare, then (and
    a single-word core from the start) with the first letter of the
    association word — "MONTCLAIR P" finds PROPERTY/PROERTY/POA without paging
    through every Montclair business — then with its first four letters plus
    the HOA/POA acronym. Names with no usable core go in whole."""
    core, rest = name_core(name)
    words = core.split()
    if not words or (len(words) == 1 and len(core) < 4) or not rest:
        return [[(core if words and not rest else _whole(name))[:MAX_QUERY].rstrip()]]
    kind = rest.split()[0]
    narrow = [f"{core} {kind[:4]}"]
    if kind.startswith("HOME"):
        narrow.append(f"{core} HOA")
    elif kind.startswith("PROP"):
        narrow.append(f"{core} POA")
    levels = [[core]] if len(words) > 1 else []
    levels += [[f"{core} {kind[0]}"], narrow]
    out = []
    for lvl in levels:
        lvl = list(dict.fromkeys(q[:MAX_QUERY].rstrip() for q in lvl))
        if lvl not in out:
            out.append(lvl)
    return out


def query_name(name: str) -> str:
    """The first (widest) prefix searched for `name`."""
    return query_levels(name)[0][0]


def _assoc_word(tok: str) -> str:
    """ASSN, ASN, ASSOC, ASOOCIATION, ASSCIATION, ASSOCIAITON … -> ASSOCIATION."""
    if tok in _ASSOC_ABBR or (tok.startswith("AS") and len(tok) >= 8 and not tok.startswith("ASSOCIATE")
                              and SequenceMatcher(None, tok, "ASSOCIATION").ratio() >= 0.8):
        return "ASSOCIATION"
    return tok


def _canon(party: str) -> str:
    """Upper-case caption party with apostrophes closed up and the clerks'
    spellings of 'association' made uniform."""
    p = re.sub(r"['’`]", "", clean(party).upper())
    p = re.sub(r"\((?:GARNISHEE|TENANT[^)]*)\)", " ", p)
    return re.sub(r"[A-Z]+", lambda m: _assoc_word(m.group(0)), p)


def _kinds(norm: str) -> set[str]:
    """Kinds of association named in a party: by word, and by a clerk's
    misspelling of the word ("PEOPERTY", "HOWEOWNERS", "COMMUNNITY")."""
    found = {k for k, rx in _KINDS if rx.search(norm)}
    for tok in norm.split():
        if len(tok) >= 6:
            found |= {k for k, word in _KIND_WORDS if SequenceMatcher(None, tok, word).ratio() >= 0.8}
    return found


def _alike(a: str, b: str) -> bool:
    """Same word allowing an abbreviation or a clerk's typo."""
    return (len(b) >= 3 and (a.startswith(b) or b.startswith(a))) or SequenceMatcher(None, a, b).ratio() >= 0.75


def _distinct(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _GENERIC and not _kinds(t)]


def party_matches(party: str, name: str) -> bool:
    """True when a caption party is plausibly the association `name`: it
    starts with the name's core, is not a business, and either is the name
    itself (allowing clerk truncation) or carries an association word of the
    same kind (homeowners, property owners, townhouse, condominium, community …), with nothing but our own next word or an association
    word right after the core ("MONTCLAIR PROERTY …" yes, "MONTCLAIR PARK
    HOA" no)."""
    core, _rest = name_core(name)
    p = _canon(party)
    pn, nn = normalize(p), normalize(_canon(_whole(name)))
    cn = normalize(core) or nn
    if not pn or not (pn == cn or pn.startswith(cn + " ")):
        return False
    if _NOISE.sub(" ", p).split() == _NOISE.sub(" ", _canon(name)).split():
        return True                       # letter for letter, whatever its form
    if has_business_form(p):
        return False
    if pn == nn:
        return True
    ptoks, ntoks, ctoks = pn.split(), nn.split(), cn.split()
    if len(ptoks) > len(ctoks) and pn.startswith(nn + " "):
        return True                       # ours plus a suffix
    if len(ptoks) > len(ctoks) and nn.startswith(pn + " ") and (
            len(p) >= 40 or not _distinct(ntoks[len(ptoks):])):
        return True                       # cut short by the clerk or the field
    if not ASSOC_MARK_RE.search(pn):
        return False
    pk, nk = _kinds(pn), _kinds(nn)
    if nk and not (pk & nk):
        return False                      # Sugarland Run Townhouse Owners is not the Homeowners Association
    for want in _distinct(ntoks[len(ctoks):]):
        if not any(_alike(want, t) for t in ptoks[len(ctoks):]):
            return False                  # the POA is not its "… Volunteer Rescue Squad"
    if len(ptoks) > len(ctoks) and len(ntoks) > len(ctoks):
        # The word after the core must be ours (however the clerk spelt it) or
        # an association word: "COURTHOUSE GREEN FIRST HOMES ASSOCIATION" is a
        # neighbour of "Courthouse Green Property Owners Association", not it.
        a, b = ntoks[len(ctoks)], ptoks[len(ctoks)]
        similar = _alike(a, b)
        bk = _kinds(b)
        if not (similar or (bk & nk) or (len(ctoks) > 1 and b in ("ASSOCIATION", "OWNERS", "OWNER"))):
            return False
    return True


# -- parsing -------------------------------------------------------------------
def parse_courts(page_html: str) -> list[tuple[str, str]]:
    """[(fips, court name)] for every court with a civil docket, from the
    hidden courtName/courtFips inputs behind the court drop-down."""
    names = [html.unescape(v) for v in re.findall(r'name="courtName" value="([^"]*)"', page_html)]
    fips = re.findall(r'name="courtFips" value="([^"]*)"', page_html)
    return [(f, clean(n)) for f, n in zip(fips, names) if not _NOT_CIVIL.search(n)]


def parse_results(page_html: str) -> dict:
    """Rows of one result page plus what the Next request needs."""
    rows = []
    for tr in _ROW.findall(page_html):
        cells = [_text(c) for c in _CELL.findall(tr)]
        if len(cells) < 8 or not re.match(r"[A-Z]{2}\d{8}-\d{2}$", cells[1]):
            continue
        rows.append({"number": cells[1], "plaintiff": cells[2], "defendant": cells[3],
                     "hearing_date": iso_date(cells[4]), "result": cells[6], "type": cells[7]})

    def hidden(n):
        m = re.search(r'name="%s" value="([^"]*)"' % n, page_html)
        return html.unescape(m.group(1)) if m else ""
    m = re.search(r"var searchCounter=(\d+)", page_html)
    return {"rows": rows, "next": 'value="Next"' in page_html, "counter": m.group(1) if m else "0",
            "cursor": {k: hidden(k) for k in ("firstRowName", "firstRowCaseNumber",
                                               "lastRowName", "lastRowCaseNumber")}}


def parse_detail(page_html: str) -> dict:
    """Filed date and the judgment block of a civil case-detail page."""
    m = re.search(r"<main>(.*?)</main>", page_html, re.S)
    body = re.sub(r"<script.*?</script>", " ", m.group(1) if m else page_html, flags=re.S)
    fields = {}
    for label, value in _LABEL.findall(body):
        label, value = _text(label), _text(value)
        if label and value and label not in fields:
            fields[label] = value
    judgment = {k: fields[k] for k in ("Judgment", "Principal Amount", "Costs", "Attorney Fees",
                                       "Other Amount", "Interest Award", "Is Judgment Satisfied",
                                       "Date Satisfaction Filed") if fields.get(k)}
    return {"number": fields.get("Case Number", ""), "date_filed": iso_date(fields.get("Filed Date", "")),
            "type": fields.get("Case Type", ""), "judgment": judgment}


def filed_year(number: str) -> int | None:
    """GV23011788-00 -> 2023 (the two digits after the prefix are the filing year)."""
    m = re.match(r"[A-Z]{2}(\d{2})\d{6}", number or "")
    if not m:
        return None
    yy = int(m.group(1))
    return (2000 if yy <= date.today().year % 100 + 1 else 1900) + yy


def build_records(name: str, fips: str, court: str, rows: list[dict],
                  details: dict[str, dict] | None = None) -> list[dict]:
    """Fold result rows into one record per base case number, keeping only
    cases where a party passes party_matches(name)."""
    cases: dict[str, list[dict]] = {}
    for r in rows:
        cases.setdefault(r["number"].split("-")[0], []).append(r)
    out = []
    for base, acts in cases.items():
        acts.sort(key=lambda r: r["number"])
        matched, roles = [], []
        for r in acts:
            for role in ("plaintiff", "defendant"):
                if party_matches(r[role], name):
                    if r[role] not in matched:
                        matched.append(r[role])
                    if role not in roles:
                        roles.append(role)
        if not matched:
            continue
        lead = acts[0]
        det = (details or {}).get(lead["number"]) or {}
        rec = record(key=KEY, state=STATE, case_name=f'{lead["plaintiff"]} v. {lead["defendant"]}',
                     court=court, docket_number=lead["number"], date_filed=det.get("date_filed", ""),
                     nature_of_suit=lead["type"], status=_STATUS.get(lead["result"], lead["result"]),
                     associations=matched, url=BASE, case_id=f"{fips}-{base}", queried=name)
        rec["association_role"] = roles
        rec["court_fips"] = fips
        rec["filed_year"] = filed_year(lead["number"])
        rec["hearing_date"] = max((r["hearing_date"] for r in acts), default="")
        rec["actions"] = [{"number": r["number"], "type": r["type"], "hearing_date": r["hearing_date"],
                           "result": r["result"]} for r in acts]
        if det.get("judgment"):
            rec["judgment"] = det["judgment"]
        out.append(rec)
    return out


# -- browser session -----------------------------------------------------------
class _SessionLost(Exception):
    """The portal session timed out; the caller reopens and redoes the court."""


class _Portal:
    """One tab in its own BrowserOS context, past Cloudflare and the terms page."""

    def __init__(self):
        self.cdp = self.ctx = self.tid = self.sid = None
        self.courts: list[tuple[str, str]] = []

    def open(self) -> None:
        from .fl_broward import _CDP                 # same CDP plumbing, same browser
        self.close()
        self.cdp = _CDP()
        self.ctx = self.cdp.send("Target.createBrowserContext")["result"]["browserContextId"]
        self.tid = self.cdp.send("Target.createTarget", {
            "url": "about:blank", "browserContextId": self.ctx, "background": True})["result"]["targetId"]
        self.sid = self.cdp.send("Target.attachToTarget", {
            "targetId": self.tid, "flatten": True})["result"]["sessionId"]
        self.cdp.send("Page.enable", sid=self.sid)
        self.cdp.send("Page.navigate", {"url": BASE}, sid=self.sid)
        state, accepted, deadline = "", False, time.time() + 90
        while time.time() < deadline:
            time.sleep(2)
            state = self.cdp.eval(self.sid, _JS_STATE, timeout=10) or ""
            if "Welcome Page" in state:
                break
            if "|ACCEPT|" in state and not accepted:
                if state.endswith("|CODE"):
                    raise PermissionError("the portal's terms page is asking for a verification "
                                          "code again — captchas are not solved by design")
                self.cdp.eval(self.sid, "document.querySelector('input[name=accept]').click()", timeout=10)
                accepted = True
        else:
            raise PermissionError(f"could not get past the portal's entry pages (last state: {state!r}); "
                                  "is BrowserOS able to pass the Cloudflare check?")
        self.courts = parse_courts(self.cdp.eval(self.sid, "document.documentElement.outerHTML", timeout=20) or "")
        if not self.courts:
            raise RuntimeError("no court list on the welcome page — portal layout changed?")

    def close(self) -> None:
        if self.cdp:
            try:
                self.cdp.send("Target.closeTarget", {"targetId": self.tid})
                self.cdp.send("Target.disposeBrowserContext", {"browserContextId": self.ctx})
            except Exception:
                pass
            self.cdp.close()
        self.cdp = None

    def fetch(self, path: str, params: dict | None = None) -> str:
        """GET (or POST `params`) `path` from inside the tab."""
        opts = "{credentials:'include'}" if params is None else (
            "{method:'POST',credentials:'include',headers:{'Content-Type':"
            "'application/x-www-form-urlencoded'},body:new URLSearchParams(%s).toString()}" % json.dumps(params))
        raw = self.cdp.eval(self.sid, _JS_FETCH % (json.dumps(path), opts), timeout=60, await_promise=True)
        try:
            res = json.loads(raw)
        except (TypeError, ValueError):
            res = {"s": 0, "t": ""}
        status, text = res["s"], res["t"]
        if status in (403, 429, 503) or "Just a moment" in text[:2000] or "cf-chl" in text[:5000]:
            raise RateLimited(900)
        if status != 200 or "Terms and Conditions Page" in text or "unauthorized.html" in text[:600]:
            raise _SessionLost(f"status {status}")
        return text


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.portal = _Portal()
        atexit.register(self.portal.close)           # do not leave the tab open in the browser
        self.details = os.environ.get("HOASPY_VA_GDC_DETAILS", "1") != "0"
        self.only = {c.strip() for c in os.environ.get("HOASPY_VA_GDC_COURTS", "").split(",") if c.strip()}
        self.counter = "0"

    def _post(self, fips: str, query: str, action: str, cursor: dict | None = None) -> dict:
        params = {"formAction": action, "displayCaseNumber": "", "formBean": "", "localFipsCode": fips,
                  "caseActive": "", "localLastName": "", "forward": "", "back": "",
                  "localnamesearchlastName": query, "lastName": query,
                  "localnamesearchfirstName": "", "firstName": "", "localnamesearchmiddleName": "",
                  "middleName": "", "localnamesearchsuffix": "", "suffix": "",
                  "localnamesearchsearchCategory": "O", "searchCategory": "O", "searchFipsCode": fips,
                  "searchDivision": "V", "searchType": "name", "clientSearchCounter": self.counter}
        if cursor:
            params.update(cursor, unCheckedCases="")
        page = parse_results(self.portal.fetch("/gdcourts/nameSearch.do", params))
        self.counter = page["counter"]
        time.sleep(self.pace)
        return page

    def _court_rows(self, fips: str, name: str) -> list[dict]:
        """Every result row for `name` in one court. A level whose search is
        still paging after its FLOOD_PAGES limit is abandoned for the next,
        narrower level — unless most of what came back is the association
        itself (a busy plaintiff, not noise), in which case it is paged out."""
        levels = query_levels(name)
        for i, queries in enumerate(levels):
            last = i == len(levels) - 1
            limit = MAX_PAGES if last else FLOOD_PAGES[min(i, len(FLOOD_PAGES) - 1)]
            rows, flooded = [], False
            for query in queries:
                page = self._post(fips, query, "newSearch")
                got, pages = list(page["rows"]), 1
                while page["next"] and page["rows"] and pages < limit:
                    page = self._post(fips, query, "next", page["cursor"])
                    got += page["rows"]
                    pages += 1
                    if pages == limit and not last and page["next"] and 2 * sum(
                            party_matches(r["plaintiff"], name) or party_matches(r["defendant"], name)
                            for r in got) >= len(got):
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

    def _detail(self, fips: str, number: str) -> dict:
        text = self.portal.fetch(f"/gdcourts/nameSearch.do?formAction=caseDetails&displayCaseNumber={number}"
                                 f"&localFipsCode={fips}&caseActive=true&clientSearchCounter={self.counter}")
        time.sleep(self.pace)
        det = parse_detail(text)
        return det if det["number"] == number else {}

    def _court(self, fips: str, court: str, name: str) -> list[dict]:
        rows = self._court_rows(fips, name)
        found = build_records(name, fips, court, rows) if rows else []
        if not found or not self.details:
            return found
        details = {rec["docket_number"]: self._detail(fips, rec["docket_number"])
                   for rec in found if rec["docket_number"].endswith("-00")}
        return build_records(name, fips, court, rows, details)

    def search(self, name: str) -> list[dict]:
        if not self.portal.cdp:
            self.portal.open()
        out = []
        for fips, court in list(self.portal.courts):
            if self.only and fips not in self.only:
                continue
            for attempt in (1, 2, 3):
                try:
                    out += self._court(fips, court, name)
                    break
                except _SessionLost as exc:      # timed out: new session, redo this court
                    log.info("va_gdc: session lost in court %s (%s) — reopening", fips, exc)
                    if attempt == 3:
                        raise RuntimeError(f"portal session keeps dropping in court {fips}") from exc
                    time.sleep(5 * attempt)
                    self.portal.open()
                    self.counter = "0"
        return out
