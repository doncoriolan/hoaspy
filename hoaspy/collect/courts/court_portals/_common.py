"""What the portal adapters share: the court vocabulary of association
names, the party gate that tells an association from a business wearing
its words, date parsing, and `record()`, which builds a Case from a
portal's raw text."""

from __future__ import annotations

import re
from dataclasses import fields
from datetime import datetime

from hoaspy.lib.jsonl import now_iso
from hoaspy.lib.records import Case

# The association words as they appear in court captions; wider than the
# registry vocabulary (hoaspy/lib/names.py) by the POA and COA acronyms.
ASSOCIATION_RE = re.compile(
    r"(HOMEOWNER|HOME OWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNERS|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|OWNERS ASSOCIATION|"
    r"RESIDENTS ASSOCIATION|TOWNHOME|TOWNHOUSE|\bHOA\b|\bPOA\b|\bCOA\b)", re.I)

NOISE_RE = re.compile(r"[^A-Z0-9 ]+")
STOP_WORDS = {"INC", "INCORPORATED", "THE", "OF", "A", "AN", "AND", "CORP",
              "CORPORATION", "LLC", "LTD", "CO", "COMPANY"}


def clean(text: str | None) -> str:
    return " ".join((text or "").split())


def normalize(name: str) -> str:
    """The upper-case, punctuation-free, stop-word-free core of a party
    name, so 'Sunset Lakes HOA, Inc.' and 'SUNSET LAKES HOA INC' compare equal."""
    tokens = NOISE_RE.sub(" ", (name or "").upper()).split()
    return " ".join(token for token in tokens if token not in STOP_WORDS)


def name_matches(party: str, queried: str) -> bool:
    found, wanted = normalize(party), normalize(queried)
    return bool(found and wanted) and (found == wanted or wanted in found or found in wanted)


# An explicit association marker in a party name. Matched against both the
# raw text and normalize()'s output, where OF and CO are stop words — hence
# "BOARD (OF)? MANAGERS" and "COUNCIL (OF)? (UNIT|CO)? OWNERS" (Maryland's
# statutory condominium form, "Council of Unit Owners of X").
ASSOC_MARK_RE = re.compile(
    r"ASSOCIATION|\bASSOC\b|\bASSN\b|\bHOA\b|\bPOA\b|\bCOA\b|CONDOMINIUM|\bCONDOS?\b|"
    r"HOMEOWNERS?\b|HOME ?OWNERS?\b|PROPERTY OWNERS?\b|UNIT OWNERS?\b|TOWNHOMES?\b|"
    r"TOWNHOUSES?\b|COOPERATIVE|\bCO ?OP\b|BOARD (?:OF )?MANAGERS|BOARD (?:OF )?DIRECTORS|"
    r"COUNCIL (?:OF )?(?:UNIT |CO[- ]?)?OWNERS\b")
# Ownership forms that are never an association: LLCs and limited
# partnerships own rentals or develop ("Redstone Condo, LLC").
OWNER_FORMS_RE = re.compile(r"\b(?:LLC|L L C|LP|L P|LLP|L L P|LLLP|FEE OWNER|OWNER LP|ASSOCIATES)\b")
# Business activities. Decisive when they follow the last association word
# ("Homeowners Finance Co.", "Community Association Underwriters", "Homeowner
# Association Services") or when there is no association word; before it
# they are just part of a name ("Left Bank Condominium Association",
# "Financial Center Condominium Office Association").
ACTIVITY_RE = re.compile(
    r"\b(?:FINANCE|FINANCIAL|MORTGAGE|BANK|BANCORP|INSURANCE|INSURERS?|UNDERWRITERS|"
    r"INDEMNITY|ASSURANCE|SURETY|REALTY|MANAGEMENT|MANAGERS?|SERVICES|"
    r"LEASING|RENTALS?)\b")
# Kept for callers that only need the vocabulary.
BUSINESS_RE = re.compile(OWNER_FORMS_RE.pattern + "|" + ACTIVITY_RE.pattern + r"|\b(?:LTD|PLLC)\b")
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
# be the association: entity suffixes, association words, phases, sections.
ASSOC_TAIL_RE = re.compile(
    r"^(?:(?:INC|INCORPORATED|CORP|CORPORATION|ASSOCIATION|ASSOC|ASSN|CONDOMINIUM|"
    r"CONDOMINIUMS|CONDO|CONDOS|HOMEOWNERS?|HOME OWNERS?|PROPERTY OWNERS?|OWNERS|"
    r"COMMUNITY|MASTER|RESIDENTS?|TOWNHOMES?|TOWNHOUSES?|VILLAS?|HOA|POA|COA|A|AN|"
    r"THE|OF|AT|AND|PHASE|PH|SECTION|SEC|UNIT|BLDG|BUILDING|NO|NUMBER|PART|TRACT|"
    r"[IVX]+|\d+[A-Z]?)\s*)*$")
CARE_OF_RE = re.compile(r"\b(?:C/O|C O|IN CARE OF|D B A|DBA|A K A|AKA|T D B A|TDBA)\b.*$")

DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%m/%d/%y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S", "%m-%d-%Y")
CASE_COLUMNS = {column.name for column in fields(Case)}


def raw_party(name: str) -> str:
    """Upper-cased, punctuation-free party text with any 'c/o Manager LLC'
    tail removed — business forms are checked here, before normalize()
    drops LLC and LTD as stop words."""
    return CARE_OF_RE.sub("", NOISE_RE.sub(" ", (name or "").upper())).strip()


def has_business_form(name: str) -> bool:
    """True when the name is a business rather than an association: an
    owner form (LLC, LP), Ltd or PLLC without the full word Association, or
    a business activity after the last association word (or with none).
    'Board of Managers' (NYC condominiums) is an association word, not managers."""
    raw = raw_party(name).replace("BOARD OF MANAGERS", "BOARD")
    if not raw:
        return False
    if OWNER_FORMS_RE.search(raw):
        return True
    if re.search(r"\b(?:LTD|PLLC)\b", raw) and not re.search(r"\bASSOCIATION\b|\bASSN\b", raw):
        return True
    last_marker = max((match.end() for match in ASSOC_MARK_RE.finditer(raw)), default=-1)
    return any(match.start() >= last_marker for match in ACTIVITY_RE.finditer(raw))


def looks_like_association(name: str) -> bool:
    """An association marker with no overriding business form."""
    party = normalize(raw_party(name))
    return bool(party) and bool(ASSOC_MARK_RE.search(party)) and not has_business_form(name)


def party_is_association(party: str, queried_core: str) -> bool:
    """True when a court-caption party that carries the queried core name is
    plausibly the association itself rather than a business sharing its
    leading words. An association marker wins unless a business form (LLC,
    LP, LTD, finance, insurance, management, apartments …) is also there;
    without a marker the party must be the core alone or the core plus
    entity and association suffixes, with no trade vocabulary."""
    raw = raw_party(party)
    found, wanted = normalize(raw), normalize(queried_core)
    if not found or not wanted or wanted not in found:
        return False
    if has_business_form(party):
        return False
    if ASSOC_MARK_RE.search(found):
        return True
    at = found.find(wanted)
    head, tail = found[:at].strip(), found[at + len(wanted):].strip()
    if head and not re.fullmatch(r"(?:THE|A|AN)\s*", head):
        return False
    if TRADE_RE.search(tail):
        return False
    return bool(ASSOC_TAIL_RE.match(tail))


def iso_date(text: str | None) -> str:
    """'03/21/2025' | '2025-03-21' | '21-Mar-2025' -> '2025-03-21'; '' when it is none of them."""
    text = clean(text)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text[:len(fmt) + 6 if "%B" in fmt else len(text)], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    return match.group(0) if match else ""


def record(*, key: str, state: str, case_name: str, court: str, docket_number: str, date_filed: str = "",
           date_terminated: str = "", nature_of_suit: str = "", status: str = "", associations: list[str],
           url: str, case_id: str = "", queried: str, **columns) -> Case:
    """A Case from a portal's raw text: whitespace squashed, dates as
    YYYY-MM-DD, `case_data_id` the portal's own key or else the docket
    number, `queries` the name that found it. Further keyword arguments are
    Case columns (association_role, county, level) or, for anything the
    Case does not name, extras."""
    shared = {name: value for name, value in columns.items() if name in CASE_COLUMNS}
    extras = {name: value for name, value in columns.items() if name not in CASE_COLUMNS}
    return Case(
        case_name=clean(case_name), court=clean(court), docket_number=clean(docket_number),
        date_filed=iso_date(date_filed), date_terminated=iso_date(date_terminated),
        nature_of_suit=clean(nature_of_suit), cause=clean(status), state=state,
        associations=[clean(name) for name in associations], url=url,
        case_data_id=clean(case_id) or clean(docket_number), source=key, queries=[queried],
        retrieved_at=now_iso(), extras=extras, **shared)
