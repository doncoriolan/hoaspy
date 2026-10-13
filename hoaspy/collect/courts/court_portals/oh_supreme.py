"""Supreme Court of Ohio — the Clerk's online docket, party search by entity name.

    https://www.supremecourt.ohio.gov/clerk/ecms/

Every case filed in the Supreme Court of Ohio since 1985: jurisdictional
and discretionary appeals from the twelve District Courts of Appeals,
certified conflicts, appeals from the Board of Tax Appeals and the PUCO,
and original actions (mandamus, prohibition). This is the top of the state
system, not a trial court — a case here was already tried in a county
court and appealed once — so every record carries `level: "supreme"`, the
lower court and its case number, and the county the case came from.

The page talks to one handler, POST Ajax.ashx, form-encoded:

* `action=CaseSearch&paramPartyEntityName=<words>`: cases with a party
  whose entity name carries the words as an adjacent phrase, each word a
  prefix ("condo" finds "Condominium", "home owner" finds "Home-Owners";
  "owners" does not find "Homeowners"). Punctuation splits words, so
  "johnsons island" misses "Johnson's Island". Optional
  `paramCaseFiledFrom` / `paramCaseFiledTo` (MM-DD-YYYY). 1,000 rows at
  most, newest first, so a capped query is split by filing-date window.
  Rows hold the case number, caption, filing date, status, case type and
  prior court — not the parties.
* `action=GetCaseDetails&paramCaseYear=2026&paramCaseNumber=0797`: the
  parties with their roles (Appellant, Appellee, Relator, Amicus Curiae …),
  the prior court with county and case numbers, the docket and the
  decisions. One request per case, cached for the run.

Anonymous: no account, no captcha, no cookie. The handler wants the
X-CSRF-TOKEN header — a constant published in the page's own
scripts/dist/site.min.js, read from there on first use — and a Referer;
without them it answers 200 with an empty body, which is how a rotated
token shows up (re-read once, then stop). robots.txt does not cover
/clerk/ and the page states no terms against automated use. Every case has
a public deep link, #/caseinfo/<year>/<number>.

    python -m hoaspy state-courts --portal oh_supreme

Two kinds of query, both best-effort by construction:

* **per association name** (the driver's list — for Ohio the IRS roster
  and the nonprofit-articles report): the distinctive core of the name as
  the phrase, with its '&' / 'and' twin, and only parties whose own core is
  that core (`same_community`);
* **statewide sweeps** (`SWEEPS`, appended by the driver): the association
  words themselves — "condo", "homeowner", "owners assoc" … — which return
  every case with such a party, a few hundred in all. A party is kept when
  it is a community (`community_name`): a condominium, a homeowners /
  property owners / unit owners / community / master association, with a
  distinctive name, and not an insurer ("Auto-Owners Insurance"), a
  developer LLC, a trust, a county landlords' group or an ad-hoc group of
  residents.

Friends of the court are not parties to the dispute, so an association
that appears only as amicus curiae does not get the case.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time

from hoaspy.lib import http
from hoaspy.lib.http import BROWSER_USER_AGENT

from . import RateLimited
from ._common import clean, has_business_form, normalize, record

STATE = "OH"
KEY = "oh_supreme"
NEEDS_COOKIE = False
LEVEL = "supreme"
COURT = "Supreme Court of Ohio"
PORTAL_URL = "https://www.supremecourt.ohio.gov/clerk/ecms/"
API_URL = PORTAL_URL + "Ajax.ashx"
SCRIPT_URL = PORTAL_URL + "scripts/dist/site.min.js"
INFO = {
    "name": "Supreme Court of Ohio — Clerk's online docket (party entity-name search)",
    "url": PORTAL_URL,
    "access": "anonymous, no captcha; per-association phrase search plus statewide sweeps of "
              "the association words, then one case-details request per case",
    "coverage": "every Supreme Court of Ohio case since 1985 — appeals from the District Courts "
                "of Appeals, the Board of Tax Appeals and the PUCO, and original actions",
    "caveat": "best-effort — the state's highest court only (county Common Pleas and municipal "
              "dockets are not here); word-prefix phrase match on the party's recorded name; "
              "amicus-only appearances are left out",
}
ROW_CAP = 1000
FIRST_DAY = dt.date(1980, 1, 1)
MAX_SPLIT_DEPTH = 24

# Statewide phrases queried after the named associations (the driver appends
# them; --no-sweeps leaves them out). Words are prefixes, so "condo" is also
# "condominium" and "owners assoc" is also "Owners' Association".
SWEEPS = (
    "condo", "homeowner", "home owner", "owners assoc", "owners assn",
    "property owners", "unit owners", "community assoc", "community assn",
    "master assoc",
)
ASSOCIATION_WORD = r"(?:ASSOCIATION|ASSOCIATIO|ASSOCIATED|ASSOC|ASSN)"
# What makes a party a community association in a sweep. Townhomes, villas,
# lake and civic associations are too often apartments, businesses or
# advocacy groups to take on the word alone.
HOA_MARK_RE = re.compile(
    r"\bCONDOMINI?UMS?\b|\bCONDOS?\b|\bHOME ?OWNERS?\b|\bUNIT OW[A-Z]*NERS?\b|"
    rf"\bOWNERS? {ASSOCIATION_WORD}\b|\b(?:COMMUNITY|MASTER) {ASSOCIATION_WORD}\b")
# Insurers, trusts and ad-hoc groups that wear the words (has_business_form
# already drops LLCs and a business activity after the association word).
NOT_COMMUNITY_RE = re.compile(
    r"\b(?:INSURANCE|INS|MUTUAL|MUT|TRUST|TRUSTEE|PRESERVATION|MOBILE|"
    r"CITIZENS|TENANTS|TAXPAYERS|SEEKING|AFFECTED|NETWORK|INSTITUTE|MANAGERS?|"
    r"MANAGEMENT|REALTY)\b|\b(?:COUNTY|CTY) PROPERTY OWNERS\b")
# Words that do not name a community on their own ("Property Owners").
FILLER_RE = re.compile(
    rf"^(?:INC|INCORPORATED|CORP|CORPORATION|THE|A|AN|OF|FOR|AND|AT|CONDOMINI?UMS?|CONDOS?|"
    rf"{ASSOCIATION_WORD}|HOME|HOMEOWNERS?|PROPERTY|UNIT|LOT|OW[A-Z]*NERS?|COMMUNITY|MASTER|"
    r"NO|[IVX]+|\d+)$")
# Officers and boards sued in the association's name.
OFFICER_HEAD_RE = re.compile(
    r"^(?:.*?,\s*(?:President|Vice President|Treasurer|Secretary|Trustee|Officer)s?,\s*|"
    r"(?:The\s+)?(?:Officers|Trustees|Directors|Members)\s+of\s+(?:the\s+)?|"
    r"(?:The\s+)?Board\s+of\s+(?:Directors|Managers|Trustees)\s+of\s+(?:the\s+)?)", re.I)
BOARD_TAIL_RE = re.compile(r"[\s,]+Board\s+of\s+(?:Directors|Managers|Trustees)\s*$", re.I)
ET_AL_RE = re.compile(r"[\s,]+et\.? al\.?\s*$", re.I)
ENTITY_SUFFIX_RE = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD|CO)\b[.]?[\s,.]*)+$", re.I)
# Where a name's distinctive core ends (the first association word).
MARKER_RE = re.compile(
    r"\b(?:CONDOMINI?UMS?|CONDOS?|HOME ?OWNERS?|PROPERTY OWNERS?|PROP OWNERS?|UNIT OWNERS?|"
    r"LAND OWNERS?|LOT OWNERS?|OWNERS?|COMMUNITY|MASTER|RESIDENTS?|CIVIC|NEIGHBORHOOD|"
    r"TOWNHOMES?|TOWNHOUSES?|ASSOCIATIONS?|ASSOCIATON|ASSOCATION|ASSOC|ASSN|HOA|POA|COA)\b.*$", re.I)
PARTY_ROLES = ("appellant", "appellee", "relator", "respondent", "petitioner", "intervenor", "intervening")
NO_COUNTY = ("", "none", "(none)")

log = logging.getLogger(__name__)


def case_url(case_number: str) -> str:
    year, _, number = clean(case_number).partition("-")
    return f"{PORTAL_URL}#/caseinfo/{year}/{number}"


def flat_name(name: str) -> str:
    """Upper-case words with apostrophes closed up ("Owner's" -> OWNERS) and other punctuation as spaces."""
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", re.sub(r"['’]", "", (name or "").upper())).split())


def party_name(party: str) -> str:
    """The association inside a party as the clerk wrote it: 'et al.' and
    officer / board wrappers removed ("Hazel Smith, President, Lost Hollow
    Property Owners Association Board of Directors" -> the association)."""
    name = ET_AL_RE.sub("", clean(party))
    return BOARD_TAIL_RE.sub("", OFFICER_HEAD_RE.sub("", name)).strip(" ,")


def core_of(name: str) -> str:
    """The distinctive core of an association name: the words before the
    first association word, entity suffix and leading article dropped."""
    bare = ENTITY_SUFFIX_RE.sub("", party_name(name)).strip(" ,.")
    bare = re.sub(r"^(?:THE|A)\s+", "", bare, flags=re.I)
    return MARKER_RE.sub("", bare).strip(" ,.-&")


def query_for(name: str) -> str:
    """The phrase to search for one association name: its core ("Wellington
    Hills"); for a one-word core, the core plus the next word ("Zoar
    Community"); the whole name when it has no core ("Community
    Associations Institute")."""
    bare = re.sub(r"^(?:THE|A)\s+", "", ENTITY_SUFFIX_RE.sub("", clean(name)).strip(" ,."), flags=re.I)
    core = core_of(name)
    words = bare.split()
    if len(core.split()) >= 2:
        return core
    if core and len(words) >= 2 and words[0].upper() == core.upper():
        return " ".join(words[:2])
    return bare


def phrases_for(name: str) -> list[str]:
    """query_for(name) and its '&' / 'and' twin: the clerk writes "Hills and
    Dales" where the IRS roster writes "HILLS & DALES", and the search
    matches words, not meaning."""
    phrase = query_for(name)
    if "&" in phrase:
        twin = " ".join(phrase.replace("&", " and ").split())
    else:
        twin = re.sub(r"\s+and\s+", " & ", phrase, flags=re.I)
    return [text for text in dict.fromkeys((phrase, twin)) if text]


def leading_words(name: str) -> str:
    """The first two words of a name, article dropped, "Home Owners" closed
    up and plurals levelled — what tells "Edgewater Homeowners" from
    "Edgewater Condominium" when the core is a single word."""
    flat = re.sub(r"\bHOME OWNER", "HOMEOWNER", re.sub(r"^(?:THE|A) ", "", flat_name(party_name(name))))
    return " ".join(word.rstrip("S") for word in flat.split()[:2])


def same_community(party: str, queried: str) -> bool:
    """True when a returned party is the queried association: an
    association word and the same distinctive core ("Hills and Dales
    Owners Association" for "HILLS & DALES HOME OWNERS ASSOCIATION"; a
    one-word core must also share the word after it), or the same whole
    name when there is no core. Never a business form (an LLC, insurance,
    management …)."""
    name = party_name(party)
    if not name or has_business_form(name):
        return False
    party_core, queried_core = normalize(core_of(name)), normalize(core_of(queried))
    if not queried_core:
        return normalize(name) == normalize(queried)
    if party_core != queried_core or not MARKER_RE.search(name):
        return False
    return len(queried_core.split()) >= 2 or leading_words(name) == leading_words(queried)


def community_name(party: str) -> str:
    """The association a sweep hit names, or '' when the party is not a
    community association: no association form, nothing distinctive left
    once the form words go ("Property Owners"), a business or lender
    wearing the words ("Auto-Owners Insurance", "East Bank Condominiums
    II, LLC"), or a person ("Pat Condo")."""
    name = party_name(party)
    flat = flat_name(name)
    if not name or not HOA_MARK_RE.search(flat):
        return ""
    if has_business_form(name) or NOT_COMMUNITY_RE.search(flat):
        return ""
    if not any(not FILLER_RE.match(word) for word in flat.split()):
        return ""
    words = flat.split()
    if len(words) == 2 and words[1] in ("CONDO", "CONDOS"):
        return ""
    return name


def html_text(fragment: str | None) -> str:
    return clean(html.unescape(re.sub(r"<[^>]+>", "", fragment or "")))


def county_text(value: str | None) -> str:
    county = clean(value)
    return "" if county.lower() in NO_COUNTY else county


def shape(detail: dict, queried: str, sweep: bool) -> dict | None:
    """One GetCaseDetails answer -> a docket record naming the association(s)
    among its parties, or None when no party qualifies."""
    info = detail.get("CaseInfo") or {}
    number = clean(info.get("CaseNumber"))
    if not number:
        return None
    associations: list[str] = []
    roles: list[str] = []
    for party in detail.get("Parties") or []:
        role = clean(party.get("Type")).lower()
        if not role.startswith(PARTY_ROLES):
            continue                                  # amicus, alias, non-party
        raw = clean(party.get("Name"))
        name = community_name(raw) if sweep else (party_name(raw) if same_community(raw, queried) else "")
        if not name:
            continue
        if name not in associations:
            associations.append(name)
        if role not in roles:
            roles.append(role)
    if not associations:
        return None
    closing = next((item for item in detail.get("DecisionItems") or [] if item.get("DisposesCase")), None)
    jurisdiction = detail.get("CaseJurisdiction") or {}
    columns: dict = {"level": LEVEL, "association_role": roles}
    county = county_text(jurisdiction.get("County"))
    if county:
        columns["county"] = county
    lower = county_text(jurisdiction.get("Name"))
    if lower:
        columns["lower_court"] = lower
        numbers = [clean(prior.get("Number")) for prior in jurisdiction.get("PriorCaseNumbers") or []
                   if clean(prior.get("Number"))]
        if numbers:
            columns["lower_court_case"] = numbers
    if closing:
        columns["disposition"] = html_text(closing.get("Description"))[:300]
    return record(
        key=KEY, state=STATE, case_name=re.sub(r"\s*[\r\n]+\s*", " ", info.get("Caption") or ""),
        court=COURT, docket_number=number, date_filed=(info.get("DateFiled") or "")[:10],
        date_terminated=((closing or {}).get("ReleaseDate") or "")[:10],
        nature_of_suit=info.get("CaseType") or "", status=info.get("Status") or "",
        associations=associations, url=case_url(number), case_id=number, queried=queried, **columns).as_row()


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.session = http.session(BROWSER_USER_AGENT,
                                    headers={"Referer": PORTAL_URL, "X-Requested-With": "XMLHttpRequest"})
        self.token = ""
        self.capped: list[str] = []                 # "phrase day" windows still at the cap
        self.hits: dict[str, list[dict]] = {}       # phrase -> search rows
        self.details: dict[str, dict | None] = {}

    # -- transport ----------------------------------------------------------------
    def csrf_token(self) -> str:
        if not self.token:
            response = self.session.get(SCRIPT_URL, timeout=60)
            response.raise_for_status()
            match = re.search(r'"X-CSRF-TOKEN":"([^"]+)"', response.text)
            if not match:
                raise RuntimeError("no X-CSRF-TOKEN in the docket's site.min.js")
            self.token = match.group(1)
        return self.token

    def call(self, **form):
        """One Ajax.ashx call -> decoded JSON (a list, a dict or a string
        such as "Too many results"). An empty body means the token was
        refused: re-read it once."""
        for _ in (1, 2):
            time.sleep(self.pace)
            response = self.session.post(API_URL, data=form, headers={"X-CSRF-TOKEN": self.csrf_token()}, timeout=120)
            if response.status_code == 429:
                raise RateLimited(int(response.headers.get("Retry-After") or 600))
            response.raise_for_status()
            if response.text.strip():
                return response.json()
            self.token = ""
        raise PermissionError("the Ohio docket answered with an empty body twice — its token or access rules changed")

    def search_rows(self, phrase: str, first: dt.date, last: dt.date, depth: int = 0) -> list[dict]:
        """Every search row for `phrase` filed in [first, last], the window
        halved whenever an answer hits the 1,000-row cap."""
        rows = self.call(action="CaseSearch", paramPartyEntityName=phrase,
                         paramCaseFiledFrom=first.strftime("%m-%d-%Y"), paramCaseFiledTo=last.strftime("%m-%d-%Y"))
        if not isinstance(rows, list):
            raise RuntimeError(f"CaseSearch answered {str(rows)[:80]!r}")
        if len(rows) < ROW_CAP or first >= last or depth > MAX_SPLIT_DEPTH:
            if len(rows) >= ROW_CAP:
                self.capped.append(f"{phrase} {first.isoformat()}")
                log.warning("  %r still %d rows on %s — some cases that day are missed", phrase, ROW_CAP, first)
            return rows
        middle = first + (last - first) // 2
        return (self.search_rows(phrase, first, middle, depth + 1)
                + self.search_rows(phrase, middle + dt.timedelta(days=1), last, depth + 1))

    def detail(self, number: str) -> dict | None:
        if number not in self.details:
            year, _, case = number.partition("-")
            answer = self.call(action="GetCaseDetails", paramCaseYear=year, paramCaseNumber=case)
            self.details[number] = answer if isinstance(answer, dict) else None   # "Sealed", "Too many results"
        return self.details[number]

    # -- one query ----------------------------------------------------------------
    def search(self, name: str) -> list[dict]:
        sweep = name.lower() in SWEEPS
        numbers: list[str] = []
        for phrase in ([name] if sweep else phrases_for(name)):
            key = phrase.upper()
            if key not in self.hits:
                self.hits[key] = self.search_rows(phrase, FIRST_DAY, dt.date.today())
            for row in self.hits[key]:
                number = clean(row.get("CaseNumber"))
                if number and number not in numbers:
                    numbers.append(number)
        out = []
        for number in numbers:
            detail = self.detail(number)
            case = shape(detail, name, sweep) if detail else None
            if case:
                out.append(case)
        return out
