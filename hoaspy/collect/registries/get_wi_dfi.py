"""Wisconsin DFI Homeowners' Association registry (Wis. Stat. 710.085).

    python -m hoaspy wi-dfi                      # full sweep (~2.5 h, resumable)
    python -m hoaspy wi-dfi --fresh              # ignore the checkpoint
    python -m hoaspy wi-dfi --county Dane Brown
    python -m hoaspy wi-dfi --limit 40           # smoke test

Since 2022 every Wisconsin homeowners' association must file a public
notice with the Department of Financial Institutions: name, HOA number,
municipality and county of the planned community, management company.
Condominiums under ch. 703 are not covered, and associations that never
filed are absent.

DFI publishes only a search page, driven by two required GET parameters:
`Query` (a word-prefix match on the name, or an exact HOA number) and
`MunicipalityCounty` (a word-prefix match on the "Village of Windsor -
Dane" string). A page holds at most 500 rows and the details page answers
HTTP 500, so the listing is all there is. The sweep asks each of the 72
counties for every word prefix a..z / 0..9, de-duplicates on the HOA
number, and splits a page that hits the cap by appending one more
character. One request per second, never under 0.7 s: bursts come back as
empty pages, which are retried.

The management-company column is kept only when it reads as a company;
a personal name there is dropped. The listing carries no addresses, phone
numbers or e-mail addresses.

Output: Wisconsin's rows in records/state_registries.jsonl (replaced on
each full run) and coverage.json collected["hoa_registry"]. Checkpoint:
.cache/wi/dfi_counties.jsonl, one finished county per line — a killed
sweep resumes at the next county; `--fresh` starts over.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlencode

import requests

from hoaspy import ROOT
from hoaspy.collect.registries import REGISTRIES_PATH
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, replace_rows
from hoaspy.lib.records import Association

CHECKPOINT_PATH = ROOT / ".cache" / "wi" / "dfi_counties.jsonl"
REGISTRY_URL = "https://apps.dfi.wi.gov/apps/HomeOwnersAssociation/Search"
ROBOTS_URL = "https://apps.dfi.wi.gov/robots.txt"
SOURCE = "Wisconsin DFI — Homeowners' Association registry (Wis. Stat. 710.085)"
PAGE_CAP = 500
MIN_PACE = 0.7
PREFIXES = "abcdefghijklmnopqrstuvwxyz0123456789"
COUNTIES = [
    "Adams", "Ashland", "Barron", "Bayfield", "Brown", "Buffalo", "Burnett", "Calumet",
    "Chippewa", "Clark", "Columbia", "Crawford", "Dane", "Dodge", "Door", "Douglas", "Dunn",
    "Eau Claire", "Florence", "Fond du Lac", "Forest", "Grant", "Green", "Green Lake", "Iowa",
    "Iron", "Jackson", "Jefferson", "Juneau", "Kenosha", "Kewaunee", "La Crosse", "Lafayette",
    "Langlade", "Lincoln", "Manitowoc", "Marathon", "Marinette", "Marquette", "Menominee",
    "Milwaukee", "Monroe", "Oconto", "Oneida", "Outagamie", "Ozaukee", "Pepin", "Pierce", "Polk",
    "Portage", "Price", "Racine", "Richland", "Rock", "Rusk", "Sauk", "Sawyer", "Shawano",
    "Sheboygan", "St. Croix", "Taylor", "Trempealeau", "Vernon", "Vilas", "Walworth",
    "Washington", "Waukesha", "Waupaca", "Waushara", "Winnebago", "Wood",
]

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
NUMBER_RE = re.compile(r"AssociationNumber=(HOA\d+)")
NO_RESULTS = "returned no records"
COMPANY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|CO|CORP|COMPANY|MANAGEMENT|MANAGMENT|MGMT|PROPERT\w*|REALTY|"
    r"REAL ESTATE|GROUP|SERVICES|SOLUTIONS|ASSOCIATES|ASSOCIATION|ASSN|HOMEOWNERS|OWNERS|"
    r"BUILDERS|ENTERPRISES|PARTNERS|LTD|RENTALS|HOMES|DEVELOPMENT|BOARD|DIRECTORS|"
    r"ARCHITECTURE|HABITAT|BANKER|SPECIALISTS|ADMINISTRATION|ASSET|CONDOMINIUM)\b", re.I)
NOT_COMPANY_RE = re.compile(r"EMPLOYED|C/O|^(N/?A|NONE|NOT APPLICABLE|SELF)\.?$", re.I)
MUNICIPALITY_PREFIX_RE = re.compile(r"^(?:CITY|VILLAGE|TOWN|TOWNSHIP)\s+OF\s+", re.I)
MUNICIPALITY_SUFFIX_RE = re.compile(r"\s+(?:CITY|VILLAGE|TOWN|TOWNSHIP)$", re.I)
COUNTY_RE = re.compile(r"^(?:COUNTY\s+OF\s+)?(.*?)(?:\s+COUNTY)?$", re.I)

log = logging.getLogger(__name__)


def html_text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_rows(page: str) -> list[dict]:
    """The result table's rows: [{number, name, place, manager}], header rows skipped."""
    rows = []
    for row_html in ROW_RE.findall(page):
        match = NUMBER_RE.search(row_html)
        cells = [html_text(cell) for cell in CELL_RE.findall(row_html)]
        if not match or len(cells) < 3:
            continue
        rows.append({"number": match.group(1), "name": cells[0], "place": cells[2],
                     "manager": cells[3] if len(cells) > 3 else ""})
    return rows


def split_place(place: str) -> tuple[str, str]:
    """'Village of Windsor - Dane' -> ('Windsor', 'Dane'); unparsable -> ('', '')."""
    if " - " not in place:
        return "", ""
    municipality, county = place.rsplit(" - ", 1)
    # "Town of A and Town of B": the first
    municipality = re.split(r"\s+(?:and|&|/)\s+", municipality.strip(), maxsplit=1)[0]
    municipality = MUNICIPALITY_SUFFIX_RE.sub("", MUNICIPALITY_PREFIX_RE.sub("", municipality.strip())).strip()
    county = (COUNTY_RE.match(county.strip()) or [None, county])[1].strip()
    return municipality.title(), county.title()


def company(manager: str) -> str:
    """The management-company cell, kept only when it reads as a company."""
    name = " ".join((manager or "").split())
    if not name or NOT_COMPANY_RE.search(name) or not COMPANY_RE.search(name):
        return ""
    return name


def to_record(row: dict, retrieved_at: str = "") -> Association:
    city, county = split_place(row["place"])
    lookup = urlencode({"Query": row["number"], "MunicipalityCounty": county or row["place"]})
    return Association(
        state="WI", source=SOURCE, source_url=f"{REGISTRY_URL}?{lookup}",
        record_id=row["number"], name=row["name"], status="Registered — HOA public notice",
        status_detail=f"planned community in {row['place']}" if row["place"] else "",
        city=city, county=county, manager_name=company(row["manager"]), retrieved_at=retrieved_at)


class Client:
    """One paced session against the search page."""

    def __init__(self, pace: float):
        self.session = http.session()
        self.pace = max(pace, MIN_PACE)
        self._last = 0.0
        self.requests = 0

    def search(self, query: str, place: str) -> list[dict]:
        """One search page's rows. An empty page without the "no records"
        notice is the site's burst throttling: it is retried with backoff."""
        for attempt in range(5):
            wait = self._last + self.pace - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(REGISTRY_URL, params={"Query": query, "MunicipalityCounty": place},
                                            timeout=90)
                self._last = time.monotonic()
                self.requests += 1
                response.raise_for_status()
            except requests.RequestException as exc:
                self._last = time.monotonic()
                log.warning("%s/%s: %s (retry %d)", query, place, exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue
            rows = parse_rows(response.text)
            if rows or NO_RESULTS in response.text:
                return rows
            time.sleep(5 * (attempt + 1))
        log.warning("%s/%s: still empty after retries — treated as no rows", query, place)
        return []


def load_checkpoint(path: Path = CHECKPOINT_PATH) -> tuple[set[str], dict[str, dict]]:
    """(finished counties, {HOA number: row} they yielded), one JSON line per county."""
    done: set[str] = set()
    found: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                done.add(entry["county"])
                for row in entry["rows"]:
                    found.setdefault(row["number"], row)
    return done, found


def save_checkpoint(county: str, rows: list[dict], path: Path = CHECKPOINT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"county": county, "rows": rows}) + "\n")


def sweep(client: Client, counties: list[str], limit: int | None = None,
          checkpoint: Path | None = CHECKPOINT_PATH) -> dict[str, dict]:
    """Every HOA in `counties`, {HOA number: row}; a county is read from the
    checkpoint when an earlier run finished it."""
    done, found = load_checkpoint(checkpoint) if checkpoint else (set(), {})
    if done:
        log.info("resuming: %d counties done, %d HOAs so far", len(done), len(found))

    def search_prefix(query: str, county: str, county_rows: dict[str, dict]) -> None:
        rows = client.search(query, county)
        for row in rows:
            county_rows.setdefault(row["number"], row)
        if len(rows) >= PAGE_CAP:                       # split the capped page
            for char in PREFIXES:
                search_prefix(query + char, county, county_rows)

    for county in counties:
        if county in done:
            continue
        county_rows: dict[str, dict] = {}
        for char in PREFIXES:
            search_prefix(char, county, county_rows)
            if limit and len(found) + len(county_rows) >= limit:
                found.update(county_rows)
                return found
        new = [row for number, row in county_rows.items() if number not in found]
        found.update(county_rows)
        if checkpoint:
            save_checkpoint(county, list(county_rows.values()), checkpoint)
        log.info("%s: %d new (%d total, %d requests)", county, len(new), len(found), client.requests)
    return found


def robots_allow() -> bool:
    """Whether the site's robots.txt lets this client fetch the search page
    (a missing or HTML robots file counts as no rule)."""
    parser = urllib.robotparser.RobotFileParser()
    try:
        response = requests.get(ROBOTS_URL, timeout=30, headers={"User-Agent": http.USER_AGENT})
        if response.status_code != 200 or "<html" in response.text[:500].lower():
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(http.USER_AGENT, REGISTRY_URL)
    except requests.RequestException:
        return True


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--county", nargs="+", help="limit the sweep to these counties")
    parser.add_argument("--limit", type=int, help="stop after this many HOAs (smoke test)")
    parser.add_argument("--fresh", action="store_true", help="ignore the county checkpoint")
    parser.add_argument("--pace", type=float, default=1.0, help=f"seconds between requests (>= {MIN_PACE})")


def run(args: argparse.Namespace) -> int:
    if not robots_allow():
        log.error("robots.txt disallows %s — not sweeping", REGISTRY_URL)
        return 2
    counties = args.county or COUNTIES
    client = Client(args.pace)
    partial = bool(args.limit or args.county)
    if args.fresh and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
    found = sweep(client, counties, args.limit, None if partial else CHECKPOINT_PATH)
    retrieved_at = now_iso()
    records = sorted((to_record(row, retrieved_at) for row in found.values()), key=lambda record: record.record_id)
    if not records:
        log.error("no HOAs found — leaving records untouched")
        return 1
    if partial:
        log.info("partial sweep (%d rows) — not written", len(records))
        for record in records[:5]:
            print(record.as_row())
        return 0
    total = replace_rows(REGISTRIES_PATH, (record.as_row() for record in records), {"WI"})
    update_coverage("WI", "hoa_registry", {
        "source": SOURCE, "records": len(records), "url": REGISTRY_URL,
        "note": "HOAs only (no condos), filings since 2022"})
    with_manager = sum(1 for record in records if record.manager_name)
    log.info("%s: %d rows total (%d WI; %d with a management company; %d requests)",
             REGISTRIES_PATH.name, total, len(records), with_manager, client.requests)
    print(f"  WI: {len(records):,} registered HOAs (DFI, Wis. Stat. 710.085)")
    return 0
