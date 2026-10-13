"""NYC condominium-board liens from ACRIS, on NYC Open Data.

A New York condominium board collecting unpaid common charges records a
"Lien of Common Charges" (document type LOCC) with the City Register — the
NYC counterpart of a Florida claim of lien — and a TOLCC when it is
terminated. ACRIS is published as three joinable Socrata datasets: master
(one row per document), parties, and legals (borough/block/lot plus the
street address, so NYC liens, unlike Broward's, carry property addresses).
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

import requests

from hoaspy.lib.records import Lien

log = logging.getLogger(__name__)

CATALOG_URL = "https://data.cityofnewyork.us"
MASTER_URL = f"{CATALOG_URL}/resource/bnx9-e6tj.json"
PARTIES_URL = f"{CATALOG_URL}/resource/636b-3b5g.json"
LEGALS_URL = f"{CATALOG_URL}/resource/8h5j-fqxa.json"
SOURCE_PAGE = "https://data.cityofnewyork.us/City-Government/ACRIS-Real-Property-Master/bnx9-e6tj"
SOURCE = "nyc-acris"
PAGE_SIZE = 5000
BATCH_SIZE = 100                # document ids per parties / legals query

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}
DOC_LABELS = {"LOCC": "lien_of_common_charges", "TOLCC": "termination_of_common_charges_lien"}

# The filing party is usually "BOARD OF MANAGERS OF THE XYZ CONDOMINIUM".
FILER_RE = re.compile(
    r"(BOARD OF MANAGERS|BOARD OF DIRECTORS|HOMEOWNER|CONDOMINIUM|\bCONDO\b|"
    r"OWNERS ASSOCIATION|COMMUNITY ASSOCIATION|\bHOA\b)", re.I)
BOARD_PREFIX_RE = re.compile(r"^(THE\s+)?BOARD OF (MANAGERS|DIRECTORS) OF(\s+THE)?\s+", re.I)


def socrata_rows(session: requests.Session, url: str, params: dict, timeout: float = 90.0) -> list[dict]:
    """One Socrata query, waiting out a 429 up to five times."""
    for attempt in range(5):
        response = session.get(url, params=params, timeout=timeout)
        if response.status_code == 429:
            wait = 10 * (attempt + 1)
            log.warning("socrata 429 — sleeping %ds", wait)
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"still rate limited after retries: {url}")


def fetch_master(session: requests.Session) -> list[dict]:
    """Every LOCC and TOLCC document in the master dataset."""
    out, offset = [], 0
    while True:
        rows = socrata_rows(session, MASTER_URL, {
            "$where": "doc_type in('LOCC','TOLCC')", "$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"})
        out.extend(rows)
        offset += len(rows)
        log.info("master: %d documents", offset)
        if len(rows) < PAGE_SIZE:
            return out
        time.sleep(0.3)


def fetch_joined(session: requests.Session, url: str, doc_ids: list[str], label: str) -> dict[str, list[dict]]:
    """{document id: rows} of the parties or legals dataset for the given documents."""
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for start in range(0, len(doc_ids), BATCH_SIZE):
        batch = doc_ids[start:start + BATCH_SIZE]
        ids = ",".join(f"'{doc_id}'" for doc_id in batch)
        for row in socrata_rows(session, url, {"$where": f"document_id in({ids})", "$limit": 50000}):
            by_doc[row["document_id"]].append(row)
        if (start // BATCH_SIZE) % 20 == 0:
            log.info("%s: %d/%d documents joined", label, start + len(batch), len(doc_ids))
        time.sleep(0.25)
    return by_doc


def to_record(master: dict, parties: list[dict], legals: list[dict], retrieved_at: str = "") -> Lien | None:
    """One master row with its parties and legals as a lien record; None
    when the recording date is unusable."""
    names = [party.get("name", "").strip() for party in parties if party.get("name")]
    filer = next((name for name in names if FILER_RE.search(name)), "")
    association = BOARD_PREFIX_RE.sub("", filer).strip() if filer else ""
    respondents = [name for name in names if name != filer]

    address = parcel = property_borough = ""
    for legal in legals:
        parcel = f"{legal.get('borough', '')}-{legal.get('block', '')}-{legal.get('lot', '')}"
        # The legals carry the PROPERTY borough; the master's recorded_borough
        # is merely where the document was filed.
        property_borough = legal.get("borough", "")
        address = " ".join(part for part in (legal.get("street_number"), legal.get("street_name"),
                                             legal.get("unit") and f"unit {legal['unit']}") if part)
        if address:
            break

    recorded = (master.get("recorded_datetime") or master.get("document_date") or "")[:10]
    year = int(recorded[:4]) if recorded[:4].isdigit() else None
    if not year:
        return None
    amount = master.get("document_amt") or ""
    return Lien(
        doc_id=master["document_id"], doc_type=master["doc_type"],
        doc_type_label=DOC_LABELS.get(master["doc_type"], master["doc_type"]),
        recorded_date=f"{recorded[5:7]}/{recorded[8:10]}/{recorded[:4]}", recorded_ymd=recorded.replace("-", ""),
        year=year, amount=amount if amount not in ("0", "0.00") else "", case_number=master.get("crfn", ""),
        state="NY", county=BOROUGHS.get(property_borough or master.get("recorded_borough", ""), "New York City"),
        source=SOURCE, source_page=SOURCE_PAGE, association=association, filers=[filer] if filer else [],
        respondents=respondents, n_parties=len(names), legal_description=parcel, parcel_id=parcel,
        property_address=address, retrieved_at=retrieved_at)


def fetch(session: requests.Session, retrieved_at: str = "") -> list[Lien]:
    """Every common-charges lien with an identifiable association."""
    master = fetch_master(session)
    doc_ids = [row["document_id"] for row in master]
    parties = fetch_joined(session, PARTIES_URL, doc_ids, "parties")
    legals = fetch_joined(session, LEGALS_URL, doc_ids, "legals")
    records = []
    for row in master:
        record = to_record(row, parties.get(row["document_id"], []), legals.get(row["document_id"], []), retrieved_at)
        if record:
            records.append(record)
    kept = [record for record in records if record.association]
    log.info("acris: %d documents, %d with an identifiable association", len(records), len(kept))
    return kept
