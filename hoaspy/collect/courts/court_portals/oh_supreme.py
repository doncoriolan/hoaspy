"""Supreme Court of Ohio — Clerk's online docket, party search by entity name.

    https://www.supremecourt.ohio.gov/clerk/ecms/

Every case filed in the Supreme Court of Ohio since 1985: jurisdictional and
discretionary appeals from the twelve District Courts of Appeals, certified
conflicts, appeals from the Board of Tax Appeals and the PUCO, and original
actions (mandamus, prohibition). This is the top of the state system, not a
trial court — a case here was already tried in a county court and appealed
once — so every record carries `level: "supreme"`, the lower court and its
case number, and the county the case came from.

The Ember page talks to one handler, `POST Ajax.ashx`, form-encoded:

* `action=CaseSearch&paramPartyEntityName=<words>` — cases with a party whose
  entity name carries the words as an adjacent phrase, each word matched as
  a prefix ("condo" finds "Condominium", "home owner" finds "Home-Owners";
  "owners" does not find "Homeowners"). Punctuation splits words, so
  "johnsons island" misses "Johnson's Island". Optional
  `paramCaseFiledFrom` / `paramCaseFiledTo` (MM-DD-YYYY). 1,000 rows at most,
  newest first, so a capped query is split by filing-date window. Rows hold
  case number, caption, filing date, status, case type and prior court —
  not the parties.
* `action=GetCaseDetails&paramCaseYear=2026&paramCaseNumber=0797` — the
  parties with their roles (Appellant, Appellee, Relator, Amicus Curiae…),
  the prior court with county and case numbers, the docket and the
  decisions. One request per case, cached for the run.

Anonymous: no account, no captcha, no cookie. The handler wants the
`X-CSRF-TOKEN` header — a constant published in the page's own
`scripts/dist/site.min.js`, read from there on first use — and a `Referer`;
without them it answers 200 with an empty body, which is how a rotated
token shows up (re-read once, then stop). robots.txt does not cover
`/clerk/` and the page states no terms against automated use (recorded
2026-09-30). Every case has a public deep link, `#/caseinfo/<year>/<number>`.

    ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal oh_supreme

Two kinds of query, both best-effort by construction:

* **per association name** (the driver's list — for Ohio the IRS
  exempt-organization roster and the nonprofit-articles report): the
  distinctive core of the name as the phrase, with its '&' / 'and' twin,
  and only parties whose own core is that core (`same_community`);
* **statewide sweeps** (`SWEEPS`, appended by the driver): the association
  words themselves — "condo", "homeowner", "owners assoc"… — which return
  every case with such a party, a few hundred in all. A party is kept when
  it is a community (`community_name`): a condominium, a homeowners /
  property owners / unit owners / community / master association, with a
  distinctive name, and not an insurer ("Auto-Owners Insurance"), a
  developer LLC, a trust, a county landlords' group or an ad-hoc group of
  residents.

Friends of the court are not parties to the dispute, so an association that
appears only as amicus curiae does not get the case.
"""
from __future__ import annotations

import datetime as _dt
import html
import logging
import re
import time

import requests

from . import RateLimited
from ._common import clean, has_business_form, normalize, record

STATE = "OH"
KEY = "oh_supreme"
NEEDS_COOKIE = False
LEVEL = "supreme"
COURT = "Supreme Court of Ohio"
BASE = "https://www.supremecourt.ohio.gov/clerk/ecms/"
API = BASE + "Ajax.ashx"
SCRIPT = BASE + "scripts/dist/site.min.js"
INFO = {
    "name": "Supreme Court of Ohio — Clerk's online docket (party entity-name search)",
    "url": BASE,
    "access": "anonymous, no captcha; per-association phrase search plus statewide sweeps of "
              "the association words, then one case-details request per case",
    "coverage": "every Supreme Court of Ohio case since 1985 — appeals from the District Courts "
                "of Appeals, the Board of Tax Appeals and the PUCO, and original actions",
    "caveat": "best-effort — the state's highest court only (county Common Pleas and municipal "
              "dockets are not here); word-prefix phrase match on the party's recorded name; "
              "amicus-only appearances are left out",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
CAP = 1000
FIRST_DAY = _dt.date(1980, 1, 1)
log = logging.getLogger("state_courts.oh")

# Statewide phrases queried after the named associations (the driver appends
# them; --no-sweeps leaves them out). Words are prefixes, so "condo" is also
# "condominium" and "owners assoc" is also "Owners' Association".
SWEEPS = (
    "condo", "homeowner", "home owner", "owners assoc", "owners assn",
    "property owners", "unit owners", "community assoc", "community assn",
    "master assoc",
)

_ASSN = r"(?:ASSOCIATION|ASSOCIATIO|ASSOCIATED|ASSOC|ASSN)"
# What makes a party a community association in a sweep. Townhomes, villas,
# lake and civic associations are too often apartments, businesses or
# advocacy groups to take on the word alone.
_HOA_MARK = re.compile(
    r"\bCONDOMINI?UMS?\b|\bCONDOS?\b|\bHOME ?OWNERS?\b|\bUNIT OW[A-Z]*NERS?\b|"
    rf"\bOWNERS? {_ASSN}\b|\b(?:COMMUNITY|MASTER) {_ASSN}\b")
# Insurers, trusts and ad-hoc groups that wear the words (has_business_form
# already drops LLCs and a business activity after the association word).
_NOT_COMMUNITY = re.compile(
    r"\b(?:INSURANCE|INS|MUTUAL|MUT|TRUST|TRUSTEE|PRESERVATION|MOBILE|"
    r"CITIZENS|TENANTS|TAXPAYERS|SEEKING|AFFECTED|NETWORK|INSTITUTE|MANAGERS?|"
    r"MANAGEMENT|REALTY)\b|\b(?:COUNTY|CTY) PROPERTY OWNERS\b")
# Words that do not name a community on their own ("Property Owners").
_FILLER = re.compile(
    rf"^(?:INC|INCORPORATED|CORP|CORPORATION|THE|A|AN|OF|FOR|AND|AT|CONDOMINI?UMS?|CONDOS?|"
    rf"{_ASSN}|HOME|HOMEOWNERS?|PROPERTY|UNIT|LOT|OW[A-Z]*NERS?|COMMUNITY|MASTER|"
    r"NO|[IVX]+|\d+)$")
# Officers and boards sued in the association's name.
_OFFICER_HEAD = re.compile(
    r"^(?:.*?,\s*(?:President|Vice President|Treasurer|Secretary|Trustee|Officer)s?,\s*|"
    r"(?:The\s+)?(?:Officers|Trustees|Directors|Members)\s+of\s+(?:the\s+)?|"
    r"(?:The\s+)?Board\s+of\s+(?:Directors|Managers|Trustees)\s+of\s+(?:the\s+)?)", re.I)
_BOARD_TAIL = re.compile(r"[\s,]+Board\s+of\s+(?:Directors|Managers|Trustees)\s*$", re.I)
_ET_AL = re.compile(r"[\s,]+et\.? al\.?\s*$", re.I)
_SUFFIX = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD|CO)\b[.]?[\s,.]*)+$", re.I)
# Where a name's distinctive core ends (first association word).
_MARK = re.compile(
    r"\b(?:CONDOMINI?UMS?|CONDOS?|HOME ?OWNERS?|PROPERTY OWNERS?|PROP OWNERS?|UNIT OWNERS?|"
    r"LAND OWNERS?|LOT OWNERS?|OWNERS?|COMMUNITY|MASTER|RESIDENTS?|CIVIC|NEIGHBORHOOD|"
    r"TOWNHOMES?|TOWNHOUSES?|ASSOCIATIONS?|ASSOCIATON|ASSOCATION|ASSOC|ASSN|HOA|POA|COA)\b.*$", re.I)
_PARTY_ROLES = ("appellant", "appellee", "relator", "respondent", "petitioner",
                "intervenor", "intervening")


def case_url(case_number: str) -> str:
    year, _, num = clean(case_number).partition("-")
    return f"{BASE}#/caseinfo/{year}/{num}"


def _flat(name: str) -> str:
    """Upper-case words with apostrophes closed up ("Owner's" -> OWNERS) and
    other punctuation as spaces."""
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", re.sub(r"['’]", "", (name or "").upper())).split())


def party_name(party: str) -> str:
    """The association inside a party as the clerk wrote it: 'et al.' and
    officer / board wrappers removed ("Hazel Smith, President, Lost Hollow
    Property Owners Association Board of Directors" -> the association)."""
    n = _ET_AL.sub("", clean(party))
    return _BOARD_TAIL.sub("", _OFFICER_HEAD.sub("", n)).strip(" ,")


def core_of(name: str) -> str:
    """Distinctive core of an association name — the words before the first
    association word, entity suffix and leading article dropped."""
    n = _SUFFIX.sub("", party_name(name)).strip(" ,.")
    n = re.sub(r"^(?:THE|A)\s+", "", n, flags=re.I)
    return _MARK.sub("", n).strip(" ,.-&")


def query_for(name: str) -> str:
    """The phrase to search for one of our association names: its core
    ("Wellington Hills"); for a one-word core, the core plus the next word
    ("Zoar Community"); the whole name when it has no core ("Community
    Associations Institute")."""
    n = re.sub(r"^(?:THE|A)\s+", "", _SUFFIX.sub("", clean(name)).strip(" ,."), flags=re.I)
    core = core_of(name)
    words = n.split()
    if len(core.split()) >= 2:
        return core
    if core and len(words) >= 2 and words[0].upper() == core.upper():
        return " ".join(words[:2])
    return n


def phrases_for(name: str) -> list[str]:
    """query_for(name) and its '&' / 'and' twin — the clerk writes "Hills
    and Dales" where the IRS roster writes "HILLS & DALES", and the search
    matches words, not meaning."""
    q = query_for(name)
    if "&" in q:
        twin = " ".join(q.replace("&", " and ").split())
    else:
        twin = re.sub(r"\s+and\s+", " & ", q, flags=re.I)
    return [x for x in dict.fromkeys((q, twin)) if x]


def _lead(name: str) -> str:
    """First two words of a name, article dropped, "Home Owners" closed up
    and plurals levelled — what tells "Edgewater Homeowners" from
    "Edgewater Condominium" when the core is a single word."""
    flat = re.sub(r"\bHOME OWNER", "HOMEOWNER", re.sub(r"^(?:THE|A) ", "", _flat(party_name(name))))
    return " ".join(w.rstrip("S") for w in flat.split()[:2])


def same_community(party: str, queried: str) -> bool:
    """True when a returned party is the queried association: an association
    word and the same distinctive core ("Hills and Dales Owners Association"
    for "HILLS & DALES HOME OWNERS ASSOCIATION"; a one-word core must also
    share the word after it), or the same whole name when there is no core.
    Never a business form (LLC, insurance, management…)."""
    p = party_name(party)
    if not p or has_business_form(p):
        return False
    pc, qc = normalize(core_of(p)), normalize(core_of(queried))
    if not qc:
        return normalize(p) == normalize(queried)
    if pc != qc or not _MARK.search(p):
        return False
    return len(qc.split()) >= 2 or _lead(p) == _lead(queried)


def community_name(party: str) -> str:
    """The association a sweep hit names, or '' when the party is not a
    community association: no association form, nothing distinctive left
    once the form words go ("Property Owners"), a business or lender wearing
    the words ("Auto-Owners Insurance", "East Bank Condominiums II, LLC"), or
    a person ("Pat Condo")."""
    name = party_name(party)
    flat = _flat(name)
    if not name or not _HOA_MARK.search(flat):
        return ""
    if has_business_form(name) or _NOT_COMMUNITY.search(flat):
        return ""
    if not any(not _FILLER.match(w) for w in flat.split()):
        return ""
    words = flat.split()
    if len(words) == 2 and words[1] in ("CONDO", "CONDOS"):
        return ""
    return name


def _text(s: str | None) -> str:
    return clean(html.unescape(re.sub(r"<[^>]+>", "", s or "")))


def _county(v: str | None) -> str:
    c = clean(v)
    return "" if c.lower() in ("", "none", "(none)") else c


def shape(detail: dict, queried: str, sweep: bool) -> dict | None:
    """One GetCaseDetails answer -> a docket record naming the association(s)
    among its parties, or None when no party qualifies."""
    info = detail.get("CaseInfo") or {}
    number = clean(info.get("CaseNumber"))
    if not number:
        return None
    assocs: list[str] = []
    roles: list[str] = []
    for p in detail.get("Parties") or []:
        role = clean(p.get("Type")).lower()
        if not role.startswith(_PARTY_ROLES):
            continue                                  # amicus, alias, non-party
        raw = clean(p.get("Name"))
        name = community_name(raw) if sweep else (party_name(raw) if same_community(raw, queried) else "")
        if not name:
            continue
        if name not in assocs:
            assocs.append(name)
        if role not in roles:
            roles.append(role)
    if not assocs:
        return None
    closing = next((d for d in detail.get("DecisionItems") or [] if d.get("DisposesCase")), None)
    juris = detail.get("CaseJurisdiction") or {}
    rec = record(
        key=KEY, state=STATE,
        case_name=re.sub(r"\s*[\r\n]+\s*", " ", info.get("Caption") or ""),
        court=COURT, docket_number=number, date_filed=(info.get("DateFiled") or "")[:10],
        date_terminated=((closing or {}).get("ReleaseDate") or "")[:10],
        nature_of_suit=info.get("CaseType") or "", status=info.get("Status") or "",
        associations=assocs, url=case_url(number), case_id=number, queried=queried)
    rec["level"] = LEVEL
    rec["association_role"] = roles
    county = _county(juris.get("County"))
    if county:
        rec["county"] = county
    lower = _county(juris.get("Name"))
    if lower:
        rec["lower_court"] = lower
        nums = [clean(n.get("Number")) for n in juris.get("PriorCaseNumbers") or [] if clean(n.get("Number"))]
        if nums:
            rec["lower_court_case"] = nums
    if closing:
        rec["disposition"] = _text(closing.get("Description"))[:300]
    return rec


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": BASE,
                               "X-Requested-With": "XMLHttpRequest"})
        self.token = ""
        self.capped: list[str] = []               # "phrase day" windows still at the cap
        self._hits: dict[str, list[dict]] = {}    # phrase -> search rows
        self._details: dict[str, dict | None] = {}

    # -- transport --------------------------------------------------------------------
    def _token(self) -> str:
        if not self.token:
            r = self.s.get(SCRIPT, timeout=60)
            r.raise_for_status()
            m = re.search(r'"X-CSRF-TOKEN":"([^"]+)"', r.text)
            if not m:
                raise RuntimeError("no X-CSRF-TOKEN in the docket's site.min.js")
            self.token = m.group(1)
        return self.token

    def _call(self, **form):
        """One Ajax.ashx call -> decoded JSON (a list, a dict or a string
        such as "Too many results"). An empty body means the token was
        refused: re-read it once."""
        for attempt in (1, 2):
            time.sleep(self.pace)
            r = self.s.post(API, data=form, headers={"X-CSRF-TOKEN": self._token()}, timeout=120)
            if r.status_code == 429:
                raise RateLimited(int(r.headers.get("Retry-After") or 600))
            r.raise_for_status()
            if r.text.strip():
                return r.json()
            self.token = ""
        raise PermissionError("the Ohio docket answered with an empty body twice — its token "
                              "or access rules changed")

    def _search(self, phrase: str, d0: _dt.date, d1: _dt.date, depth: int = 0) -> list[dict]:
        """Every search row for `phrase` filed in [d0, d1], splitting the
        window in two whenever a response hits the 1,000-row cap."""
        rows = self._call(action="CaseSearch", paramPartyEntityName=phrase,
                          paramCaseFiledFrom=d0.strftime("%m-%d-%Y"),
                          paramCaseFiledTo=d1.strftime("%m-%d-%Y"))
        if not isinstance(rows, list):
            raise RuntimeError(f"CaseSearch answered {str(rows)[:80]!r}")
        if len(rows) < CAP or d0 >= d1 or depth > 24:
            if len(rows) >= CAP:
                self.capped.append(f"{phrase} {d0.isoformat()}")
                log.warning("  %r still %d rows on %s — some cases that day are missed", phrase, CAP, d0)
            return rows
        mid = d0 + (d1 - d0) // 2
        return (self._search(phrase, d0, mid, depth + 1)
                + self._search(phrase, mid + _dt.timedelta(days=1), d1, depth + 1))

    def _detail(self, number: str) -> dict | None:
        if number not in self._details:
            year, _, num = number.partition("-")
            d = self._call(action="GetCaseDetails", paramCaseYear=year, paramCaseNumber=num)
            self._details[number] = d if isinstance(d, dict) else None   # "Sealed", "Too many results"
        return self._details[number]

    # -- one query ----------------------------------------------------------------------
    def search(self, name: str) -> list[dict]:
        sweep = name.lower() in SWEEPS
        numbers: list[str] = []
        for phrase in ([name] if sweep else phrases_for(name)):
            key = phrase.upper()
            if key not in self._hits:
                self._hits[key] = self._search(phrase, FIRST_DAY, _dt.date.today())
            for row in self._hits[key]:
                number = clean(row.get("CaseNumber"))
                if number and number not in numbers:
                    numbers.append(number)
        out = []
        for number in numbers:
            detail = self._detail(number)
            rec = shape(detail, name, sweep) if detail else None
            if rec:
                out.append(rec)
        return out
