"""Broward County Official Records index, via the county's public SFTP.

Broward publishes its recorded-document index as pipe-delimited yearly exports
covering 1978 to present, plus a rolling 10 days of dailies. Credentials are
published by the Records, Taxes and Treasury Division for public use.

Three files per year, joined on the document id:

    CY<year>doc-rec.txt   one row per recorded document: id, date, type, amount
    CY<year>nme-rec.txt   one row per party:  id, name, role (D=direct/filer,
                          R=reverse/respondent), sequence
    CY<year>lgl-rec.txt   legal description and parcel id — SPARSE, see below

A Claim of Lien is document type `LIE`. Isolating *HOA* liens from construction,
tax and code-enforcement liens is done by matching the filing party against
association name patterns, since the index has no category field.

Coverage caveat measured on CY2025: lgl-rec carries a parcel id for 100% of
deeds but only 0.5% of liens. The index therefore identifies *who* filed
against *whom* and when, but not the property address. Recovering the address
means either buying the document image or joining the owner name through their
deed, which does carry a parcel id.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

import paramiko

log = logging.getLogger("records.broward")

HOST = "BCFTP.Broward.org"
PORT = 22
USER = "crpublic"
PASSWORD = "crpublic"
YEARLY_DIR = "OR_Yearly_Exports"
DAILY_DIR = "Official_Records_Download"
SOURCE_PAGE = "https://www.broward.org/RecordsTaxesTreasury/Records/Pages/IndexFiles-Completed.aspx"

# The lien lifecycle. Pulling the whole family lets you measure duration
# (lien -> satisfaction) and escalation (lien -> lis pendens), which is the
# part that individual documents can't tell you.
LIEN_TYPES = {
    "LIE": "claim_of_lien",
    "LIEX": "claim_of_lien_amended",
    "PALIE": "partial_lien",
    "SPALIE": "satisfaction_partial_lien",
    "NCL": "notice_of_contest_of_lien",
    "LP": "lis_pendens",
    "RST": "release_satisfaction",
    "CFJ": "certificate_final_judgment",
    "FJ": "final_judgment",
}

# Types worth keeping by default: the lien itself plus escalation. RST and FJ
# are huge and mostly unrelated to associations, so they're opt-in.
DEFAULT_TYPES = ["LIE", "LIEX", "PALIE", "SPALIE", "NCL", "LP"]

# An association filing in its own name. Deliberately broad — the false
# positives are easy to spot by eye, the false negatives are not.
ASSOCIATION_RE = re.compile(
    r"(HOMEOWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNER|MASTER ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|\bHOA\b|TOWNHOUSE|TOWNHOME|VILLAS? OF|"
    r"ASSOCIATION,? INC|ASSN,? INC|\bASSN\b)", re.I)


def connect() -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    transport = paramiko.Transport((HOST, PORT))
    transport.banner_timeout = 30
    transport.connect(username=USER, password=PASSWORD)
    return transport, paramiko.SFTPClient.from_transport(transport)


def available_years(sftp: paramiko.SFTPClient) -> list[int]:
    years = set()
    for name in sftp.listdir(YEARLY_DIR):
        if m := re.match(r"CY(\d{4})doc-rec\.txt$", name):
            years.add(int(m.group(1)))
    return sorted(years)


def download(sftp: paramiko.SFTPClient, remote: str, local: Path) -> Path:
    """Fetch one file, caching locally. Reads sequentially: the server refuses
    paramiko's parallel prefetch with 'insufficient resources'."""
    if local.exists() and local.stat().st_size > 0:
        log.debug("cached %s", local.name)
        return local

    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(local.suffix + ".part")
    with sftp.open(remote, "rb") as fr, tmp.open("wb") as fl:
        fr.prefetch = lambda *a, **k: None
        while chunk := fr.read(65536):
            fl.write(chunk)
    tmp.replace(local)
    log.info("downloaded %s (%.1f MB)", local.name, local.stat().st_size / 1024 / 1024)
    return local


def _rows(path: Path):
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line:
                yield line.split("|")


def parse_year(
    doc_path: Path, nme_path: Path, lgl_path: Path | None,
    keep_types: list[str], associations_only: bool = True,
) -> list[dict]:
    """Join the three files into one record per lien-family document."""
    wanted = set(keep_types)

    docs: dict[str, dict] = {}
    for p in _rows(doc_path):
        if len(p) < 6 or p[4].strip() not in wanted:
            continue
        docs[p[0]] = {
            "doc_id": p[0],
            "doc_type": p[4].strip(),
            "doc_type_label": LIEN_TYPES.get(p[4].strip(), p[4].strip()),
            "recorded_date": p[2].strip(),
            "recorded_ymd": p[1].strip(),
            "amount": p[5].strip(),
            "case_number": p[18].strip() if len(p) > 18 else "",
        }
    if not docs:
        return []

    parties: dict[str, list] = defaultdict(list)
    for p in _rows(nme_path):
        if len(p) > 2 and p[0] in docs:
            parties[p[0]].append({"name": p[1].strip(), "role": p[2].strip()})

    legals: dict[str, dict] = {}
    if lgl_path and lgl_path.exists():
        for p in _rows(lgl_path):
            if len(p) > 2 and p[0] in docs and p[0] not in legals:
                legals[p[0]] = {"legal_description": p[1].strip(), "parcel_id": p[2].strip()}

    out = []
    for doc_id, rec in docs.items():
        people = parties.get(doc_id, [])
        filers = [x["name"] for x in people if x["role"] == "D"]
        respondents = [x["name"] for x in people if x["role"] == "R"]

        association = next((n for n in filers if ASSOCIATION_RE.search(n)), "")
        if not association:
            # Some counties index the association on the reverse side instead.
            association = next((n for n in respondents if ASSOCIATION_RE.search(n)), "")
        if associations_only and not association:
            continue

        rec.update({
            "state": "FL",
            "county": "Broward",
            "source": "broward-or-yearly-export",
            "source_page": SOURCE_PAGE,
            "association": association,
            "filers": filers,
            "respondents": respondents,
            "n_parties": len(people),
            **legals.get(doc_id, {"legal_description": "", "parcel_id": ""}),
        })
        out.append(rec)

    return out


def fetch_years(
    years: list[int], cache_dir: Path, keep_types: list[str] | None = None,
    associations_only: bool = True, want_legal: bool = True,
) -> list[dict]:
    keep_types = keep_types or DEFAULT_TYPES
    transport, sftp = connect()
    records: list[dict] = []
    try:
        have = set(available_years(sftp))
        for year in years:
            if year not in have:
                log.warning("no yearly export for %d (available %d-%d)",
                            year, min(have), max(have))
                continue
            doc = download(sftp, f"{YEARLY_DIR}/CY{year}doc-rec.txt",
                           cache_dir / f"CY{year}doc-rec.txt")
            nme = download(sftp, f"{YEARLY_DIR}/CY{year}nme-rec.txt",
                           cache_dir / f"CY{year}nme-rec.txt")
            lgl = None
            if want_legal:
                try:
                    lgl = download(sftp, f"{YEARLY_DIR}/CY{year}lgl-rec.txt",
                                   cache_dir / f"CY{year}lgl-rec.txt")
                except IOError:
                    log.debug("no lgl-rec for %d", year)

            year_records = parse_year(doc, nme, lgl, keep_types, associations_only)
            for r in year_records:
                r["year"] = year
            log.info("%d: %d association lien-family documents", year, len(year_records))
            records.extend(year_records)
    finally:
        transport.close()
    return records
