"""Florida condominium associations from the DBPR public-records extracts.

The Division of Florida Condominiums, Timeshares and Mobile Homes publishes
its registry as five regional CSVs. Each row is one condominium project:
name, street address, county, unit count, registration status and the
managing entity. "Secondary Status" carries values like "Delinquent" — a
compliance flag attached to a named community.

These are *condominium* projects: Florida HOAs proper are only lightly
registered with DBPR, so condo and co-op associations are covered well and
non-condo HOAs barely at all.

Source: https://www2.myfloridalicense.com/condos-timeshares-mobile-homes/public-records/
"""

from __future__ import annotations

import csv
import io
import logging
import re

import requests

from hoaspy.lib.contract import strip_contacts
from hoaspy.lib.records import Association

log = logging.getLogger(__name__)

EXTRACTS_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts/"
SOURCE_PAGE = "https://www2.myfloridalicense.com/condos-timeshares-mobile-homes/public-records/"
SOURCE = "dbpr-condo-extract"

# The registry is split by region; together they are the whole state.
EXTRACTS = {
    "Condo_NF.csv": "North Florida",
    "condo_CE.csv": "Central Florida East",
    "Condo_CW.csv": "Central Florida West",
    "Condo_MD.csv": "Dade and Monroe",
    "condo_PB.csv": "Broward and Palm Beach",
}

# "1071 HIGHWAY A1A SOUTH, ST. AUGUSTINE, FL 32084" -> street / city / state / zip
ADDRESS_RE = re.compile(
    r"^(?P<street>.*),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$")


def parse_address(raw: str) -> dict:
    """{address, city, zip} from the combined address column. When it does
    not match, the raw text stays the address and the rest is empty rather
    than guessed."""
    match = ADDRESS_RE.match((raw or "").strip())
    if not match:
        return {"address": raw or "", "city": "", "zip": ""}
    return {"address": match.group("street").strip(), "city": match.group("city").strip().title(),
            "zip": match.group("zip")}


def whole_number(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_record(row: dict, region: str, url: str, retrieved_at: str = "") -> Association:
    """One extract row as a registry record."""

    def text(column: str) -> str:
        return (row.get(column) or "").strip()

    return Association(
        state="FL", source=SOURCE, source_url=url, record_id=text("Project Number"),
        name=text("Condo Name"), county=text("County"),
        **parse_address(row.get("Street City State Zip", "")),
        units=whole_number(row.get("Units")), recorded_date=text("Recorded Date"),
        status=text("Primary Status"), status_detail=text("Secondary Status"),
        manager_name=text("Managing Entity Name"), retrieved_at=retrieved_at,
        extras={
            "type": "condo", "source_region": region, "file_number": text("File Number"),
            "address_raw": text("Street City State Zip"), "manager_id": text("Managing Entity Number"),
            # Some managing entities type their phone number into the route line.
            "manager_address": strip_contacts(" ".join(
                part for part in (text("Managing Entity Route"), text("Managing Entity Street")) if part)),
            "manager_city": text("Managing Entity City"), "manager_state": text("Managing Entity State"),
            "manager_zip": text("Managing Entity Zip"),
        })


def fetch(session: requests.Session, timeout: float = 120.0, retrieved_at: str = "") -> list[Association]:
    """Every project of every regional extract, shaped. A region that cannot
    be fetched is logged and left out."""
    out: list[Association] = []
    for filename, region in EXTRACTS.items():
        url = EXTRACTS_URL + filename
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("could not fetch %s (%s): %s", filename, region, exc)
            continue
        rows = list(csv.DictReader(io.StringIO(response.content.decode("latin-1"))))   # Latin-1 in practice
        log.info("%s (%s): %d projects", filename, region, len(rows))
        out += [to_record(row, region, url, retrieved_at) for row in rows]
    return out
