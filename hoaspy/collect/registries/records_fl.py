"""Florida condominium associations from the DBPR public-records extracts.

The Division of Florida Condominiums, Timeshares and Mobile Homes publishes its
registry as five regional CSVs. Each row is one condominium project: name, full
street address, county, unit count, registration status, and the managing
entity. `Secondary Status` is the interesting field — it carries values like
"Delinquent", which is a compliance flag attached to a named community.

Scope caveat: these are *condominium* projects. Florida HOAs proper are only
lightly registered with DBPR, so this covers condo/co-op associations well and
non-condo HOAs barely at all.

Source: https://www2.myfloridalicense.com/condos-timeshares-mobile-homes/public-records/
"""

from __future__ import annotations

import csv
import io
import logging
import re

import requests

log = logging.getLogger("records.fl")

BASE = "https://www2.myfloridalicense.com/sto/file_download/extracts/"
SOURCE_PAGE = ("https://www2.myfloridalicense.com/condos-timeshares-mobile-homes/"
               "public-records/")

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
    r"^(?P<street>.*),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


def parse_address(raw: str) -> dict:
    """Split the combined address field. Returns empty parts when it doesn't
    match rather than guessing — the raw string is always kept."""
    m = ADDRESS_RE.match((raw or "").strip())
    if not m:
        return {"address": raw or "", "city": "", "zip": ""}
    return {
        "address": m.group("street").strip(),
        "city": m.group("city").strip().title(),
        "zip": m.group("zip"),
    }


def _int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def fetch(session: requests.Session, timeout: float = 120.0) -> list[dict]:
    """Download every regional extract and normalise it."""
    out: list[dict] = []

    for filename, region in EXTRACTS.items():
        url = BASE + filename
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("could not fetch %s (%s): %s", filename, region, exc)
            continue

        # These files are Latin-1 in practice, not UTF-8.
        text = resp.content.decode("latin-1")
        rows = list(csv.DictReader(io.StringIO(text)))
        log.info("%s (%s): %d projects", filename, region, len(rows))

        for row in rows:
            parts = parse_address(row.get("Street City State Zip", ""))
            out.append({
                "state": "FL",
                "source": "dbpr-condo-extract",
                "source_region": region,
                "source_url": url,
                "record_id": (row.get("Project Number") or "").strip(),
                "file_number": (row.get("File Number") or "").strip(),
                "name": (row.get("Condo Name") or "").strip(),
                "type": "condo",
                "county": (row.get("County") or "").strip(),
                **parts,
                "address_raw": (row.get("Street City State Zip") or "").strip(),
                "units": _int(row.get("Units")),
                "recorded_date": (row.get("Recorded Date") or "").strip(),
                "status": (row.get("Primary Status") or "").strip(),
                "status_detail": (row.get("Secondary Status") or "").strip(),
                "manager_id": (row.get("Managing Entity Number") or "").strip(),
                "manager_name": (row.get("Managing Entity Name") or "").strip(),
                "manager_address": " ".join(
                    p for p in ((row.get("Managing Entity Route") or "").strip(),
                                (row.get("Managing Entity Street") or "").strip()) if p
                ),
                "manager_city": (row.get("Managing Entity City") or "").strip(),
                "manager_state": (row.get("Managing Entity State") or "").strip(),
                "manager_zip": (row.get("Managing Entity Zip") or "").strip(),
                "document_url": "",
            })

    return out
