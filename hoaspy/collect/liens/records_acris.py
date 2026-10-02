"""NYC condo-board liens from ACRIS on NYC Open Data.

A New York condominium board collecting unpaid common charges records a
"Lien of Common Charges" (doc type LOCC) with the City Register — the NYC
analogue of a Florida Claim of Lien — and a TOLCC when it is terminated.
ACRIS is published as three joinable Socrata datasets: master (one row per
document), parties, and legals (borough/block/lot + street address — so NYC
liens, unlike Broward's, carry property addresses).

Output rows use the same schema as the Broward collector so the site build
ingests both identically.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

import requests

log = logging.getLogger("records.acris")

DOMAIN = "https://data.cityofnewyork.us"
MASTER = f"{DOMAIN}/resource/bnx9-e6tj.json"
PARTIES = f"{DOMAIN}/resource/636b-3b5g.json"
LEGALS = f"{DOMAIN}/resource/8h5j-fqxa.json"
SOURCE_PAGE = "https://data.cityofnewyork.us/City-Government/ACRIS-Real-Property-Master/bnx9-e6tj"

BOROUGH = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
           "4": "Queens", "5": "Staten Island"}

DOC_LABEL = {"LOCC": "lien_of_common_charges",
             "TOLCC": "termination_of_common_charges_lien"}

# The filing party is usually "BOARD OF MANAGERS OF THE XYZ CONDOMINIUM".
FILER_RE = re.compile(
    r"(BOARD OF MANAGERS|BOARD OF DIRECTORS|HOMEOWNER|CONDOMINIUM|\bCONDO\b|"
    r"OWNERS ASSOCIATION|COMMUNITY ASSOCIATION|\bHOA\b)", re.I)
BOARD_PREFIX = re.compile(
    r"^(THE\s+)?BOARD OF (MANAGERS|DIRECTORS) OF(\s+THE)?\s+", re.I)

PAGE = 5000
BATCH = 100  # document_ids per parties/legals query


def _rows(session: requests.Session, url: str, params: dict,
          timeout: float = 90.0) -> list[dict]:
    for attempt in range(5):
        resp = session.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            log.warning("socrata 429 — sleeping %ds", wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"still rate limited after retries: {url}")


def fetch_master(session: requests.Session) -> list[dict]:
    out, offset = [], 0
    while True:
        rows = _rows(session, MASTER, {
            "$where": "doc_type in('LOCC','TOLCC')",
            "$limit": PAGE, "$offset": offset, "$order": ":id"})
        out.extend(rows)
        offset += len(rows)
        log.info("master: %d documents", offset)
        if len(rows) < PAGE:
            return out
        time.sleep(0.3)


def fetch_joined(session: requests.Session, url: str, doc_ids: list[str],
                 label: str) -> dict[str, list[dict]]:
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(doc_ids), BATCH):
        chunk = doc_ids[i:i + BATCH]
        ids = ",".join(f"'{d}'" for d in chunk)
        rows = _rows(session, url, {
            "$where": f"document_id in({ids})", "$limit": 50000})
        for r in rows:
            by_doc[r["document_id"]].append(r)
        if (i // BATCH) % 20 == 0:
            log.info("%s: %d/%d documents joined", label, i + len(chunk), len(doc_ids))
        time.sleep(0.25)
    return by_doc


def fetch(session: requests.Session) -> list[dict]:
    master = fetch_master(session)
    doc_ids = [m["document_id"] for m in master]
    parties = fetch_joined(session, PARTIES, doc_ids, "parties")
    legals = fetch_joined(session, LEGALS, doc_ids, "legals")

    out = []
    for m in master:
        did = m["document_id"]
        people = parties.get(did, [])
        names = [p.get("name", "").strip() for p in people if p.get("name")]
        filer_raw = next((n for n in names if FILER_RE.search(n)), "")
        association = BOARD_PREFIX.sub("", filer_raw).strip() if filer_raw else ""
        respondents = [n for n in names if n != filer_raw]

        addr = ""
        parcel = ""
        prop_boro = ""
        for l in legals.get(did, []):
            parcel = f"{l.get('borough','')}-{l.get('block','')}-{l.get('lot','')}"
            # The legals carry the PROPERTY borough; the master's
            # recorded_borough is merely where the document was filed.
            prop_boro = l.get("borough", "")
            addr = " ".join(x for x in (l.get("street_number"), l.get("street_name"),
                                        l.get("unit") and f"unit {l['unit']}") if x)
            if addr:
                break

        recorded = (m.get("recorded_datetime") or m.get("document_date") or "")[:10]
        year = int(recorded[:4]) if recorded[:4].isdigit() else None
        if not year:
            continue
        amount = m.get("document_amt") or ""
        out.append({
            "doc_id": did,
            "doc_type": m["doc_type"],
            "doc_type_label": DOC_LABEL.get(m["doc_type"], m["doc_type"]),
            "recorded_date": f"{recorded[5:7]}/{recorded[8:10]}/{recorded[:4]}",
            "recorded_ymd": recorded.replace("-", ""),
            "year": year,
            "amount": amount if amount not in ("0", "0.00") else "",
            "case_number": m.get("crfn", ""),
            "state": "NY",
            "county": BOROUGH.get(prop_boro or m.get("recorded_borough", ""),
                                  "New York City"),
            "source": "nyc-acris",
            "source_page": SOURCE_PAGE,
            "association": association,
            "filers": [filer_raw] if filer_raw else [],
            "respondents": respondents,
            "n_parties": len(names),
            "legal_description": parcel,
            "parcel_id": parcel,
            "property_address": addr,
        })
    kept = [r for r in out if r["association"]]
    log.info("acris: %d documents, %d with an identifiable association",
             len(out), len(kept))
    return kept
