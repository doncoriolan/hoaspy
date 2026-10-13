"""Florida non-profit corporations from the Sunbiz quarterly extract.

The Division of Corporations publishes its whole registry as fixed-width
quarterly files (dos.fl.gov -> Sunbiz -> data downloads). The non-profit
file covers every Florida not-for-profit corporation — which is what an
HOA or condo association legally is — with its status, principal and
mailing address, registered agent and up to six named officers.

This module is a parser the site build reads directly, not a collector
that writes a file of its own: it returns plain dicts with the corporate
columns (`corp_number`, `name`, `corp_status`, `filing_type`, `address`,
`city`, `corp_state`, `zip`, `incorporated`, `last_activity`,
`registered_agent`, `officers`). Two uses: standalone records for the
corporations whose names read as community associations (the only
statewide list of Florida HOAs proper), and a name-keyed index over all
non-profits, so associations known from other sources can be joined to
their corporate status and officers.

Record layout: 1,440 characters, fixed width, verified against the data
(the officer blocks repeat every 128 characters from offset 668). The file
holds ACTIVE corporations only (every status byte is "A"), so dissolution
is detected by absence — and phrased as "no active corporation found",
since name matching can miss.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hoaspy.lib.names import CORPORATION_NAME_RE

log = logging.getLogger(__name__)

SOURCE_PAGE = "https://dos.fl.gov/sunbiz/other-services/data-downloads/quarterly-data/"
SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"

# (start, length) of each field in the 1,440-character record.
FIELD_SPANS = {
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
OFFICER_BLOCK_START = 668
OFFICER_BLOCK_STRIDE = 128      # title(4) type(1) name(42) addr(42) city(28) state(2) zip(9)
OFFICER_BLOCKS = 6
MIN_RECORD_LENGTH = 700


def field_text(line: str, key: str) -> str:
    start, length = FIELD_SPANS[key]
    return line[start:start + length].strip()


def iso_date(raw: str) -> str:
    """MMDDYYYY -> YYYY-MM-DD; '' when blank or not a date."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[4:]}-{raw[:2]}-{raw[2:4]}"
    return ""


def officers(line: str) -> list[dict]:
    """The named officers of one record: [{"title", "name"}, …]."""
    out = []
    for block in range(OFFICER_BLOCKS):
        start = OFFICER_BLOCK_START + block * OFFICER_BLOCK_STRIDE
        title = line[start:start + 4].strip()
        name = " ".join(line[start + 5:start + 47].split())
        if name:
            out.append({"title": title, "name": name})
    return out


def parse_all(cache_dir: Path) -> list[dict]:
    """Every non-profit corporation in the extract's shards (npcordata*.txt)."""
    shards = sorted(cache_dir.glob("npcordata*.txt"))
    if not shards:
        raise FileNotFoundError(f"no npcordata*.txt shards in {cache_dir}")
    out: list[dict] = []
    for path in shards:
        with path.open(encoding="latin-1") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                if len(line) < MIN_RECORD_LENGTH:
                    continue
                name = " ".join(field_text(line, "name").split())
                if not name:
                    continue
                out.append({
                    "corp_number": field_text(line, "corp_number"),
                    "name": name,
                    "corp_status": "active" if field_text(line, "status") == "A" else "inactive",
                    "filing_type": field_text(line, "filing_type"),
                    "address": " ".join(part for part in (field_text(line, "addr1"), field_text(line, "addr2"))
                                        if part),
                    "city": field_text(line, "city").title(),
                    "corp_state": field_text(line, "state"),
                    "zip": field_text(line, "zip")[:5],
                    "incorporated": iso_date(field_text(line, "file_date")),
                    "last_activity": iso_date(field_text(line, "last_trx_date")),
                    "registered_agent": " ".join(field_text(line, "ra_name").split()),
                    "officers": officers(line),
                })
    log.info("sunbiz: parsed %d non-profit corporations from %d shards", len(out), len(shards))
    return out


def associations_only(corporations: list[dict]) -> list[dict]:
    """The corporations whose names read as community associations."""
    return [corp for corp in corporations if CORPORATION_NAME_RE.search(corp["name"])]
