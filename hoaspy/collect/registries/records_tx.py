"""Texas HOA/POA management certificates from the TREC registry.

Texas Property Code ch. 209 requires property owners' associations to file a
management certificate with the county clerk and then electronically with TREC.
TREC republishes the registry as a Socrata dataset, so the whole state comes
down in one paged API call: association name, county, city, zip, and a link to
the recorded certificate PDF (which names the management company and the
association's mailing address).

Two limits worth knowing: associations with fewer than 60 lots are exempt from
filing, and TREC has no enforcement authority over HOAs — it only hosts the
database. So this is a registry, not a complaint record.

Source: https://data.texas.gov/dataset/TREC-HOA-Management-Certificates/8auc-hzdi
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("records.tx")

ENDPOINT = "https://data.texas.gov/resource/8auc-hzdi.json"
SOURCE_PAGE = "https://hoa.texas.gov/management-certificates-search"
PAGE_SIZE = 5000

# Texas has 254 counties; a two-letter value means the filer put the state in
# the county box. Keep the row, flag the field as unusable.
BAD_COUNTIES = {"TX", "TEXAS", "N/A", "NA", "", "-"}


def fetch(session: requests.Session, timeout: float = 60.0,
          app_token: str | None = None) -> list[dict]:
    """Page through the Socrata dataset and normalise it."""
    headers = {"X-App-Token": app_token} if app_token else {}
    out: list[dict] = []
    offset = 0

    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        resp = session.get(ENDPOINT, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break

        for row in rows:
            county = (row.get("county") or "").strip()
            out.append({
                "state": "TX",
                "source": "trec-management-certificate",
                "source_region": "",
                "source_url": SOURCE_PAGE,
                "record_id": "",
                "file_number": "",
                "name": (row.get("name") or "").strip(),
                "type": (row.get("type") or "").strip().lower() or "poa",
                "county": "" if county.upper() in BAD_COUNTIES else county,
                "address": "",
                "city": (row.get("city") or "").strip(),
                "zip": (row.get("zip") or "").strip(),
                "address_raw": "",
                "units": None,
                "recorded_date": "",
                "status": "",
                "status_detail": "",
                "manager_id": "",
                "manager_name": "",
                "manager_address": "",
                "manager_city": "",
                "manager_state": "",
                "manager_zip": "",
                # The recorded certificate PDF; it names the management company.
                "document_url": ((row.get("certificate") or {}).get("url") or ""),
            })

        offset += len(rows)
        log.info("fetched %d/%s certificates", offset, "?")
        if len(rows) < PAGE_SIZE:
            break

    return out
