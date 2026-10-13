"""Broward County, FL official-records index, over the county's public SFTP.

Broward publishes its recorded-document index as pipe-delimited yearly
exports covering 1978 to the present, plus a rolling ten days of dailies.
The credentials are published by the Records, Taxes and Treasury Division
for public use. Three files per year, joined on the document id:

    CY<year>doc-rec.txt   one row per recorded document: id, date, type, amount
    CY<year>nme-rec.txt   one row per party: id, name, role (D direct/filer,
                          R reverse/respondent), sequence
    CY<year>lgl-rec.txt   legal description and parcel id — sparse, see below

A claim of lien is document type LIE. The index has no category column, so
an association's lien is told from construction, tax and code-enforcement
liens by matching the filing party against the association vocabulary.

Measured on CY2025: lgl-rec carries a parcel id for 100% of deeds but 0.5%
of liens. The index therefore says who filed against whom and when, but
not the property address.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import paramiko

from hoaspy.lib.names import LIEN_PARTY_RE
from hoaspy.lib.records import Lien

log = logging.getLogger(__name__)

SFTP_HOST = "BCFTP.Broward.org"
SFTP_PORT = 22
SFTP_USER = "crpublic"
SFTP_PASSWORD = "crpublic"
YEARLY_DIR = "OR_Yearly_Exports"
DAILY_DIR = "Official_Records_Download"
SOURCE_PAGE = "https://www.broward.org/RecordsTaxesTreasury/Records/Pages/IndexFiles-Completed.aspx"
SOURCE = "broward-or-yearly-export"

# The lien lifecycle. Pulling the whole family lets a reader measure
# duration (lien -> satisfaction) and escalation (lien -> lis pendens).
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
# Worth keeping by default: the lien itself plus escalation. RST and FJ are
# huge and mostly unrelated to associations, so they are opt-in.
DEFAULT_TYPES = ["LIE", "LIEX", "PALIE", "SPALIE", "NCL", "LP"]
YEARLY_FILE_RE = re.compile(r"CY(\d{4})doc-rec\.txt$")


def connect() -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.banner_timeout = 30
    transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
    return transport, paramiko.SFTPClient.from_transport(transport)


def available_years(sftp: paramiko.SFTPClient) -> list[int]:
    years = set()
    for name in sftp.listdir(YEARLY_DIR):
        if match := YEARLY_FILE_RE.match(name):
            years.add(int(match.group(1)))
    return sorted(years)


def download(sftp: paramiko.SFTPClient, remote: str, local: Path) -> Path:
    """Fetch one file, keeping a local copy. Read sequentially: the server
    refuses paramiko's parallel prefetch with 'insufficient resources'."""
    if local.exists() and local.stat().st_size > 0:
        log.debug("cached %s", local.name)
        return local
    local.parent.mkdir(parents=True, exist_ok=True)
    partial = local.with_suffix(local.suffix + ".part")
    with sftp.open(remote, "rb") as source, partial.open("wb") as target:
        source.prefetch = lambda *args, **kwargs: None
        while chunk := source.read(65536):
            target.write(chunk)
    partial.replace(local)
    log.info("downloaded %s (%.1f MB)", local.name, local.stat().st_size / 1024 / 1024)
    return local


def pipe_rows(path: Path) -> Iterator[list[str]]:
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line:
                yield line.split("|")


def parse_year(doc_path: Path, nme_path: Path, lgl_path: Path | None, keep_types: list[str],
               associations_only: bool = True, year: int = 0, retrieved_at: str = "") -> list[Lien]:
    """One record per lien-family document of a year, the three files joined."""
    wanted = set(keep_types)
    documents: dict[str, dict] = {}
    for cells in pipe_rows(doc_path):
        if len(cells) < 6 or cells[4].strip() not in wanted:
            continue
        documents[cells[0]] = {
            "doc_id": cells[0], "doc_type": cells[4].strip(),
            "doc_type_label": LIEN_TYPES.get(cells[4].strip(), cells[4].strip()),
            "recorded_date": cells[2].strip(), "recorded_ymd": cells[1].strip(), "amount": cells[5].strip(),
            "case_number": cells[18].strip() if len(cells) > 18 else "",
        }
    if not documents:
        return []

    parties: dict[str, list] = defaultdict(list)
    for cells in pipe_rows(nme_path):
        if len(cells) > 2 and cells[0] in documents:
            parties[cells[0]].append({"name": cells[1].strip(), "role": cells[2].strip()})

    legals: dict[str, dict] = {}
    if lgl_path and lgl_path.exists():
        for cells in pipe_rows(lgl_path):
            if len(cells) > 2 and cells[0] in documents and cells[0] not in legals:
                legals[cells[0]] = {"legal_description": cells[1].strip(), "parcel_id": cells[2].strip()}

    out = []
    for doc_id, document in documents.items():
        people = parties.get(doc_id, [])
        filers = [party["name"] for party in people if party["role"] == "D"]
        respondents = [party["name"] for party in people if party["role"] == "R"]
        association = next((name for name in filers if LIEN_PARTY_RE.search(name)), "")
        if not association:
            # Some documents index the association on the reverse side instead.
            association = next((name for name in respondents if LIEN_PARTY_RE.search(name)), "")
        if associations_only and not association:
            continue
        out.append(Lien(
            **document, year=year, state="FL", county="Broward", source=SOURCE, source_page=SOURCE_PAGE,
            association=association, filers=filers, respondents=respondents, n_parties=len(people),
            **legals.get(doc_id, {"legal_description": "", "parcel_id": ""}), retrieved_at=retrieved_at))
    return out


def fetch_years(years: list[int], cache_dir: Path, keep_types: list[str] | None = None,
                associations_only: bool = True, want_legal: bool = True, retrieved_at: str = "") -> list[Lien]:
    """The lien-family records of every requested year that has an export."""
    keep_types = keep_types or DEFAULT_TYPES
    transport, sftp = connect()
    records: list[Lien] = []
    try:
        have = set(available_years(sftp))
        for year in years:
            if year not in have:
                log.warning("no yearly export for %d (available %d-%d)", year, min(have), max(have))
                continue
            doc = download(sftp, f"{YEARLY_DIR}/CY{year}doc-rec.txt", cache_dir / f"CY{year}doc-rec.txt")
            nme = download(sftp, f"{YEARLY_DIR}/CY{year}nme-rec.txt", cache_dir / f"CY{year}nme-rec.txt")
            lgl = None
            if want_legal:
                try:
                    lgl = download(sftp, f"{YEARLY_DIR}/CY{year}lgl-rec.txt", cache_dir / f"CY{year}lgl-rec.txt")
                except OSError:
                    log.debug("no lgl-rec for %d", year)
            found = parse_year(doc, nme, lgl, keep_types, associations_only, year=year, retrieved_at=retrieved_at)
            log.info("%d: %d association lien-family documents", year, len(found))
            records.extend(found)
    finally:
        transport.close()
    return records
