"""Texas HOA / POA management certificates from the TREC registry.

Texas Property Code ch. 209 requires property owners' associations to file
a management certificate with the county clerk and then with TREC, which
republishes the registry as a Socrata dataset: association name, county,
city, ZIP and a link to the recorded certificate PDF (which names the
management company and the association's mailing address).

Associations with fewer than 60 lots are exempt from filing, and TREC has
no enforcement authority over HOAs — it only hosts the database. This is a
registry, not a complaint record.

Source: https://data.texas.gov/dataset/TREC-HOA-Management-Certificates/8auc-hzdi
"""

from __future__ import annotations

import logging

import requests

from hoaspy.lib.contract import strip_contacts
from hoaspy.lib.records import Association

log = logging.getLogger(__name__)

DATASET_URL = "https://data.texas.gov/resource/8auc-hzdi.json"
SOURCE_PAGE = "https://hoa.texas.gov/management-certificates-search"
SOURCE = "trec-management-certificate"
PAGE_SIZE = 5000

# A two-letter value means the filer put the state in the county box: the
# row is kept and the county left empty.
BAD_COUNTIES = {"TX", "TEXAS", "N/A", "NA", "", "-"}


def fetch(session: requests.Session, timeout: float = 60.0, app_token: str | None = None,
          retrieved_at: str = "") -> list[Association]:
    """Every certificate in the dataset, shaped."""
    headers = {"X-App-Token": app_token} if app_token else {}
    out: list[Association] = []
    offset = 0
    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        response = session.get(DATASET_URL, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        for row in rows:
            county = (row.get("county") or "").strip()
            # A few filers typed an e-mail address where the name goes:
            # with it removed there is no name, so no row.
            name = strip_contacts((row.get("name") or "").strip())
            if not name:
                continue
            out.append(Association(
                state="TX", source=SOURCE, source_url=SOURCE_PAGE, name=name,
                county="" if county.upper() in BAD_COUNTIES else county,
                city=(row.get("city") or "").strip(), zip=(row.get("zip") or "").strip(),
                document_url=((row.get("certificate") or {}).get("url") or ""),   # the recorded certificate PDF
                retrieved_at=retrieved_at,
                extras={"type": (row.get("type") or "").strip().lower() or "poa", "source_region": "",
                        "file_number": "", "address_raw": "", "manager_id": "", "manager_address": "",
                        "manager_city": "", "manager_state": "", "manager_zip": ""}))
        offset += len(rows)
        log.info("fetched %d certificates", offset)
        if len(rows) < PAGE_SIZE:
            break
    return out
