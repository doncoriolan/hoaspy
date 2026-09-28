"""Florida non-profit corporations from the Sunbiz quarterly extract.

The Division of Corporations publishes its whole registry as fixed-width
quarterly files (dos.fl.gov -> Sunbiz -> data downloads). The non-profit file
covers every Florida not-for-profit corporation — which is what an HOA or
condo association legally is — including corporate status, principal and
mailing address, registered agent, and up to six named officers.

Two roles here:

  * standalone records for corporations whose names look like community
    associations (the only statewide list of *HOAs proper*, since DBPR
    registers condos but barely touches HOAs), and
  * a name-keyed index over ALL non-profits, so associations known from other
    sources (DBPR, county lien indexes) can be joined to their corporate
    status and officers even when the name filter would have missed them.

Record layout: 1,440-char fixed width, verified empirically against the data
(officer blocks repeat every 128 chars from offset 668).

The quarterly file contains ACTIVE corporations only (measured: every status
byte is 'A'). So dissolution is detected by absence: an association that is
registered with DBPR or actively filing liens but has no active corporation
here has likely been administratively dissolved — a real compliance flag, but
one to phrase as "no active corporation found" since name matching can miss.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("records.sunbiz")

SOURCE_PAGE = "https://dos.fl.gov/sunbiz/other-services/data-downloads/quarterly-data/"
SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"

# (start, length) into the 1440-char record.
_F = {
    "corp_number": (0, 12),
    "name": (12, 192),
    "status": (204, 1),
    "filing_type": (205, 15),
    "addr1": (220, 42),
    "addr2": (262, 42),
    "city": (304, 28),
    "state": (332, 2),
    "zip": (334, 10),
    "file_date": (472, 8),     # MMDDYYYY
    "last_trx_date": (495, 8),
    "ra_name": (544, 42),
    "ra_addr": (587, 42),
    "ra_city": (629, 28),
}

_OFF_START = 668
_OFF_STRIDE = 128  # title(4) type(1) name(42) addr(42) city(28) state(2) zip(9)

# Community-association-looking names. Anchored at both ends where the term
# could be a prefix of something else (VILLA vs VILLAGE is the classic trap).
ASSOCIATION_NAME_RE = re.compile(
    r"\b(HOMEOWNERS?|HOME OWNERS?|CONDOMINIUM|CONDO\b|PROPERTY OWNERS?|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|RESIDENTS ASSOCIATION|"
    r"TOWNHOMES?\b|TOWNHOUSES?\b|VILLAS\b|HOA\b)", re.I)


def _get(line: str, key: str) -> str:
    start, length = _F[key]
    return line[start:start + length].strip()


def _date(raw: str) -> str:
    """MMDDYYYY -> YYYY-MM-DD, or '' when blank/garbage."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[4:]}-{raw[:2]}-{raw[2:4]}"
    return ""


def _officers(line: str) -> list[dict]:
    out = []
    for i in range(6):
        base = _OFF_START + i * _OFF_STRIDE
        title = line[base:base + 4].strip()
        name = " ".join(line[base + 5:base + 47].split())
        if not name:
            continue
        out.append({"title": title, "name": name})
    return out


def parse_all(cache_dir: Path) -> list[dict]:
    """Every non-profit corporation in the extract, lean normalised dicts."""
    shards = sorted(cache_dir.glob("npcordata*.txt"))
    if not shards:
        raise FileNotFoundError(f"no npcordata*.txt shards in {cache_dir}")

    out: list[dict] = []
    for path in shards:
        with path.open(encoding="latin-1") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                if len(line) < 700:
                    continue
                name = " ".join(_get(line, "name").split())
                if not name:
                    continue
                out.append({
                    "corp_number": _get(line, "corp_number"),
                    "name": name,
                    "corp_status": "active" if _get(line, "status") == "A" else "inactive",
                    "filing_type": _get(line, "filing_type"),
                    "address": " ".join(
                        p for p in (_get(line, "addr1"), _get(line, "addr2")) if p),
                    "city": _get(line, "city").title(),
                    "corp_state": _get(line, "state"),
                    "zip": _get(line, "zip")[:5],
                    "incorporated": _date(_get(line, "file_date")),
                    "last_activity": _date(_get(line, "last_trx_date")),
                    "registered_agent": " ".join(_get(line, "ra_name").split()),
                    "officers": _officers(line),
                })
    log.info("sunbiz: parsed %d non-profit corporations from %d shards",
             len(out), len(shards))
    return out


def associations_only(corps: list[dict]) -> list[dict]:
    return [c for c in corps if ASSOCIATION_NAME_RE.search(c["name"])]
