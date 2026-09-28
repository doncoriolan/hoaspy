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
