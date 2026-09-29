"""Helpers shared by portal adapters."""
from __future__ import annotations

import re
from datetime import datetime, timezone

ASSOCIATION_RE = re.compile(
    r"(HOMEOWNER|HOME OWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNERS|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|OWNERS ASSOCIATION|"
    r"RESIDENTS ASSOCIATION|TOWNHOME|TOWNHOUSE|\bHOA\b|\bPOA\b|\bCOA\b)", re.I)

_NOISE = re.compile(r"[^A-Z0-9 ]+")
_STOP = {"INC", "INCORPORATED", "THE", "OF", "A", "AN", "AND", "CORP",
         "CORPORATION", "LLC", "LTD", "CO", "COMPANY"}


def clean(s: str | None) -> str:
    return " ".join((s or "").split())


def normalize(name: str) -> str:
    """Upper-case, punctuation-free, stop-word-free core of a party name so
    'Sunset Lakes HOA, Inc.' and 'SUNSET LAKES HOA INC' compare equal."""
    toks = _NOISE.sub(" ", (name or "").upper()).split()
    return " ".join(t for t in toks if t not in _STOP)


def name_matches(party: str, queried: str) -> bool:
    p, q = normalize(party), normalize(queried)
    return bool(p and q) and (p == q or q in p or p in q)


# An explicit association marker in a party name.
ASSOC_MARK_RE = re.compile(
    r"ASSOCIATION|\bASSOC\b|\bASSN\b|\bHOA\b|\bPOA\b|\bCOA\b|CONDOMINIUM|\bCONDOS?\b|"
    r"HOMEOWNERS?\b|HOME ?OWNERS?\b|PROPERTY OWNERS?\b|UNIT OWNERS?\b|TOWNHOMES?\b|"
    r"TOWNHOUSES?\b|COOPERATIVE|\bCO ?OP\b|BOARD OF MANAGERS|BOARD OF DIRECTORS")
# Business forms that outrank an association marker: a limited partnership
# or LLC that owns rentals, a lender, an insurer, a manager, an apartment
# operator ("Homeowners Finance Co.", "Park Plaza Assoc Ltd").
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L L C|LP|L P|LLP|LTD|PLLC|FINANCE|FINANCIAL|MORTGAGE|BANK|BANCORP|"
    r"INSURANCE|INSURERS?|UNDERWRITERS|INDEMNITY|ASSURANCE|SURETY|REALTY|MANAGEMENT|"
    r"MANAGERS?|SERVICES(?!\s+ASS(?:OCIATION|OC|N)\b)|ASSOCIATES|FEE OWNER|OWNER LP|"
    r"LEASING|RENTALS?)\b")


def has_business_form(name: str) -> bool:
    """A business form (LLC/LP/LTD, finance, insurance, management…) in a
    name, checked before stop words are stripped."""
    return bool(BUSINESS_RE.search(_raw(name)))
# Other business vocabulary, decisive only when no association marker is
# present ("Golden Lakes Medical Center", "Mark III Devel Corp").
TRADE_RE = re.compile(
    r"\b(?:MEDICAL|CHIROPRACTIC|CLINIC|HOSPITAL|HEALTH|DENTAL|PHARMACY|REAL ESTATE|"
    r"HOLDINGS?|INVESTMENTS?|INVESTORS|CAPITAL|DEVELOPMENT|DEVELOPERS?|DEVEL|BUILDERS?|"
    r"CONSTRUCTION|CONTRACTORS?|HOMES|PARTNERS(?:HIP)?|OWNERS?|APARTMENTS?|APTS|"
    r"RESTAURANT|CAFE|MARKET|"
    r"STORE|SERVICES?|SUPPLY|AUTO|MOTORS|LAW|ATTORNEYS?|ENTERPRISES?|INDUSTRIES|"
    r"TRUCKING|ROOFING|PLUMBING|ELECTRIC|PAINTING|LANDSCAPING|CLEANING|TITLE|ESCROW|"
    r"TRUST|CHURCH|MINISTRIES|SCHOOL|CENTER|CTR|PLAZA|MALL|HOTEL|MOTEL|INN|RESORT|"
    r"GOLF|CLUB|MARINA|PARK|PA|MD|DDS|DBA|D B A)\b")
# What may follow the association's core name in a court caption and still
# be the association: entity suffixes, association words, phases/sections.
_ASSOC_TAIL = re.compile(
    r"^(?:(?:INC|INCORPORATED|CORP|CORPORATION|ASSOCIATION|ASSOC|ASSN|CONDOMINIUM|"
    r"CONDOMINIUMS|CONDO|CONDOS|HOMEOWNERS?|HOME OWNERS?|PROPERTY OWNERS?|OWNERS|"
    r"COMMUNITY|MASTER|RESIDENTS?|TOWNHOMES?|TOWNHOUSES?|VILLAS?|HOA|POA|COA|A|AN|"
    r"THE|OF|AT|AND|PHASE|PH|SECTION|SEC|UNIT|BLDG|BUILDING|NO|NUMBER|PART|TRACT|"
    r"[IVX]+|\d+[A-Z]?)\s*)*$")


_CARE_OF = re.compile(r"\b(?:C/O|C O|IN CARE OF)\b.*$")


def _raw(name: str) -> str:
    """Upper-cased, punctuation-free party text with any 'c/o Manager LLC'
    tail removed — business forms are checked here, before normalize()
    drops LLC/LTD as stop words."""
    return _CARE_OF.sub("", _NOISE.sub(" ", (name or "").upper())).strip()


def looks_like_association(name: str) -> bool:
    """An association marker with no overriding business form."""
    raw = _raw(name)
    p = normalize(raw)
    return bool(p) and bool(ASSOC_MARK_RE.search(p)) and not BUSINESS_RE.search(raw)


def party_is_association(party: str, queried_core: str) -> bool:
    """True when a court-caption party that carries the queried core name is
    plausibly the association itself rather than a business sharing its
    leading words. An association marker wins unless a business form
    (LLC/LP/LTD, finance, insurance, management, apartments…) is also there;
    without a marker the party must be the core alone or the core plus
    entity/association suffixes, with no trade vocabulary."""
    raw = _raw(party)
    p, q = normalize(raw), normalize(queried_core)
    if not p or not q or q not in p:
        return False
    if BUSINESS_RE.search(raw):
        return False
    if ASSOC_MARK_RE.search(p):
        return True
    i = p.find(q)
    head, tail = p[:i].strip(), p[i + len(q):].strip()
    if head and not re.fullmatch(r"(?:THE|A|AN)\s*", head):
        return False
    if TRADE_RE.search(tail):
        return False
    return bool(_ASSOC_TAIL.match(tail))


def iso_date(s: str | None) -> str:
    """'03/21/2025' | '2025-03-21' | '21-Mar-2025' -> '2025-03-21'; else ''."""
    s = clean(s)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%m/%d/%y", "%B %d, %Y",
                "%Y-%m-%dT%H:%M:%S", "%m-%d-%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 6 if "%B" in fmt else len(s)], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(*, key: str, state: str, case_name: str, court: str,
           docket_number: str, date_filed: str = "", date_terminated: str = "",
           nature_of_suit: str = "", status: str = "", associations: list[str],
           url: str, case_id: str = "", queried: str) -> dict:
    return {
        "case_name": clean(case_name), "court": clean(court),
        "docket_number": clean(docket_number), "date_filed": iso_date(date_filed),
        "date_terminated": iso_date(date_terminated),
        "nature_of_suit": clean(nature_of_suit), "cause": clean(status),
        "state": state, "associations": [clean(a) for a in associations],
        "url": url, "case_data_id": clean(case_id) or clean(docket_number),
        "source": key, "queries": [queried], "retrieved_at": now_iso(),
    }
