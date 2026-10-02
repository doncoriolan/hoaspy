#!/usr/bin/env python
"""Wisconsin DFI Homeowners' Association registry (Wis. Stat. 710.085).

    ./venv/bin/python -m hoaspy.collect.registries.get_wi_dfi                    # full sweep (~2.5 h, resumable)
    ./venv/bin/python -m hoaspy.collect.registries.get_wi_dfi --fresh            # ignore the checkpoint
    ./venv/bin/python -m hoaspy.collect.registries.get_wi_dfi --county Dane Brown
    ./venv/bin/python -m hoaspy.collect.registries.get_wi_dfi --limit 40         # smoke test

Since 2022 every Wisconsin homeowners' association must file a public notice
with the Department of Financial Institutions (name, HOA number, municipality
and county of the planned community, management company). Condominiums under
ch. 703 are not covered, and associations that never filed are absent.

DFI publishes only a search page,
https://apps.dfi.wi.gov/apps/HomeOwnersAssociation/Search, driven by two
required GET parameters: `Query` (word-prefix match on the name, or an exact
HOA number) and `MunicipalityCounty` (word-prefix match on the "Village of
Windsor - Dane" string). A page holds at most 500 rows and the details page
returns HTTP 500, so the listing is all there is. The sweep therefore asks
each of the 72 counties for every word prefix a..z / 0..9, dedupes on the HOA
number, and splits any page that hits the cap by appending one more
character. One request per second (never < 0.7 s) — bursts come back as empty
pages, which are retried.

Output: records/state_registries.jsonl (WI rows replaced) in the registry
shape build_site.py folds in, plus coverage.json collected["hoa_registry"].
Checkpoint: .cache/wi/dfi_counties.jsonl (one finished county per line) — a
killed sweep resumes at the next county; `--fresh` starts over.
The management-company column is kept only when it looks like a company;
personal names are dropped. No addresses, phones or e-mails exist here.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlencode

import requests

from hoaspy.collect.registries.get_states import REGS, update_coverage, write_merged

from hoaspy import ROOT
CKPT = ROOT / ".cache" / "wi" / "dfi_counties.jsonl"
BASE = "https://apps.dfi.wi.gov/apps/HomeOwnersAssociation/Search"
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
SOURCE = "Wisconsin DFI — Homeowners' Association registry (Wis. Stat. 710.085)"
PAGE_CAP = 500
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

log = logging.getLogger("wi")

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
MUNI_PREFIX_RE = re.compile(r"^(?:CITY|VILLAGE|TOWN|TOWNSHIP)\s+OF\s+", re.I)
MUNI_SUFFIX_RE = re.compile(r"\s+(?:CITY|VILLAGE|TOWN|TOWNSHIP)$", re.I)
COUNTY_RE = re.compile(r"^(?:COUNTY\s+OF\s+)?(.*?)(?:\s+COUNTY)?$", re.I)


def clean(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def parse_rows(page: str) -> list[dict]:
    """Result-table rows -> [{number, name, place, manager}] (header rows skipped)."""
    rows = []
    for tr in ROW_RE.findall(page):
        m = NUMBER_RE.search(tr)
        cells = [clean(c) for c in CELL_RE.findall(tr)]
        if not m or len(cells) < 3:
            continue
        rows.append({"number": m.group(1), "name": cells[0], "place": cells[2],
                     "manager": cells[3] if len(cells) > 3 else ""})
    return rows


def split_place(place: str) -> tuple[str, str]:
    """'Village of Windsor - Dane' -> ('Windsor', 'Dane'); unparsable -> ('', '')."""
    if " - " not in place:
        return "", ""
    muni, county = place.rsplit(" - ", 1)
    muni = re.split(r"\s+(?:and|&|/)\s+", muni.strip(), 1)[0]     # "Town of A and Town of B"
    muni = MUNI_SUFFIX_RE.sub("", MUNI_PREFIX_RE.sub("", muni.strip())).strip()
    county = (COUNTY_RE.match(county.strip()) or [None, county])[1].strip()
    return muni.title(), county.title()


def company(manager: str) -> str:
    """Keep the management-company cell only when it reads as a company."""
    m = " ".join((manager or "").split())
    if not m or NOT_COMPANY_RE.search(m) or not COMPANY_RE.search(m):
        return ""
    return m


def to_record(row: dict) -> dict:
    city, county = split_place(row["place"])
    return {
        "state": "WI",
        "source": SOURCE,
        "source_url": BASE + "?" + urlencode({"Query": row["number"],
                                              "MunicipalityCounty": county or row["place"]}),
        "record_id": row["number"],
        "name": row["name"],
        "status": "Registered — HOA public notice",
        "status_detail": f"planned community in {row['place']}" if row["place"] else "",
        "recorded_date": "",
        "address": "",
        "city": city,
        "county": county,
        "zip": "",
        "units": None,
        "manager_name": company(row["manager"]),
    }


class Client:
    def __init__(self, pace: float):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.pace = max(pace, 0.7)
        self._last = 0.0
        self.requests = 0

    def search(self, query: str, place: str) -> list[dict]:
        """One search page; empty pages that carry no 'no records' notice are
        the site's burst throttling and are retried with backoff."""
        for attempt in range(5):
            wait = self._last + self.pace - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(BASE, params={"Query": query, "MunicipalityCounty": place},
                               timeout=90)
                self._last = time.monotonic()
                self.requests += 1
                r.raise_for_status()
            except requests.RequestException as exc:
                self._last = time.monotonic()
                log.warning("%s/%s: %s (retry %d)", query, place, exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue
            rows = parse_rows(r.text)
            if rows or NO_RESULTS in r.text:
                return rows
            time.sleep(5 * (attempt + 1))
        log.warning("%s/%s: still empty after retries — treated as no rows", query, place)
        return []


def load_ckpt(path: Path = CKPT) -> tuple[set[str], dict[str, dict]]:
    """Finished counties and the rows they yielded, one JSON line per county."""
    done: set[str] = set()
    found: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                done.add(d["county"])
                for row in d["rows"]:
                    found.setdefault(row["number"], row)
    return done, found


def save_ckpt(county: str, rows: list[dict], path: Path = CKPT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps({"county": county, "rows": rows}) + "\n")


def sweep(client: Client, counties: list[str], limit: int | None = None,
          ckpt: Path | None = CKPT) -> dict[str, dict]:
    done, found = load_ckpt(ckpt) if ckpt else (set(), {})
    if done:
        log.info("resuming: %d counties done, %d HOAs so far", len(done), len(found))

    def run(query: str, county: str, county_rows: dict[str, dict]) -> None:
        rows = client.search(query, county)
        for row in rows:
            county_rows.setdefault(row["number"], row)
        if len(rows) >= PAGE_CAP:                       # split the capped page
            for ch in PREFIXES:
                run(query + ch, county, county_rows)

    for county in counties:
        if county in done:
            continue
        county_rows: dict[str, dict] = {}
        for ch in PREFIXES:
            run(ch, county, county_rows)
            if limit and len(found) + len(county_rows) >= limit:
                found.update(county_rows)
                return found
        new = [r for n, r in county_rows.items() if n not in found]
        found.update(county_rows)
        if ckpt:
            save_ckpt(county, list(county_rows.values()), ckpt)
        log.info("%s: %d new (%d total, %d requests)", county, len(new), len(found),
                 client.requests)
    return found


def robots_allow() -> bool:
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get("https://apps.dfi.wi.gov/robots.txt", timeout=30,
                         headers={"User-Agent": USER_AGENT})
        if r.status_code != 200 or "<html" in r.text[:500].lower():
            return True                                 # no robots file published
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, BASE)
    except requests.RequestException:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", nargs="+", help="limit the sweep to these counties")
    ap.add_argument("--limit", type=int, help="stop after this many HOAs (smoke test)")
    ap.add_argument("--fresh", action="store_true", help="ignore the county checkpoint")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds between requests (>= 0.7)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not robots_allow():
        log.error("robots.txt disallows %s — not sweeping", BASE)
        return 2
    counties = args.county or COUNTIES
    client = Client(args.pace)
    partial = bool(args.limit or args.county)
    if args.fresh and CKPT.exists():
        CKPT.unlink()
    found = sweep(client, counties, args.limit, None if partial else CKPT)
    rows = [to_record(r) for r in found.values()]
    rows.sort(key=lambda r: r["record_id"])
    if not rows:
        log.error("no HOAs found — leaving records untouched")
        return 1
    if partial:
        log.info("partial sweep (%d rows) — not written", len(rows))
        for r in rows[:5]:
            print(r)
        return 0
    total = write_merged(REGS, rows, {"WI"})
    update_coverage("WI", "hoa_registry", {
        "source": SOURCE,
        "records": len(rows),
        "url": BASE,
        "note": "HOAs only (no condos), filings since 2022",
    })
    with_mgr = sum(1 for r in rows if r["manager_name"])
    log.info("%s: %d rows total (%d WI; %d with a management company; %d requests)",
             REGS.name, total, len(rows), with_mgr, client.requests)
    print(f"  WI: {len(rows):,} registered HOAs (DFI, Wis. Stat. 710.085)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
