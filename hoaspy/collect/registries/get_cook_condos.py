"""Cook County, IL condominium buildings from the Assessor's open data.

    python -m hoaspy cook-condos
    python -m hoaspy cook-condos --limit 500    # smoke test

Illinois has no association registry, and the Cook County recorder's index
names an association only on the documents it is a party to (the
cook-liens command reads those). The Assessor, though, classifies every
residential condominium unit (class 299) and publishes the parcels with
coordinates, a unit-characteristics table and a parcel-address table.
Grouping the unit PINs by their 10-digit building PIN gives every condo
building in the county:

    pabr-t5kh  Parcel Universe (current year)        class='299' -> pin, pin10, zip,
                                                     lat/lon, municipality, township
    3r7i-mrz4  Residential Condominium Unit           per pin10: year built, building
               Characteristics                        PIN count, non-unit (parking/common) PINs
    3723-97qp  Parcel Addresses                       the street address of one unit per
                                                     building (the unit designator is stripped)

The data carries no association names, so each building is listed as
"<street address> Condominium": an inventory of where condominium
associations exist, not of who runs them. Owner and mailing names on the
address table are never read.

Output: records/cook_condos.jsonl in the registry shape plus `pin10`,
`year_built`, `lat` and `lon`; coverage.json collected["condo_buildings"]
for IL. The parcel list is cached for a week under .cache/cook_condos/.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

from hoaspy import ROOT
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, write_rows
from hoaspy.lib.records import Association

OUTPUT_PATH = ROOT / "records" / "cook_condos.jsonl"
CACHE_DIR = ROOT / ".cache" / "cook_condos"
PARCELS_CACHE = CACHE_DIR / "units.json"
CACHE_DAYS = 7
CATALOG_URL = "https://datacatalog.cookcountyil.gov"
PARCELS_URL = CATALOG_URL + "/resource/pabr-t5kh.json"           # Assessor - Parcel Universe
CHARACTERISTICS_URL = CATALOG_URL + "/resource/3r7i-mrz4.json"   # Assessor - Residential Condo Unit Characteristics
ADDRESSES_URL = CATALOG_URL + "/resource/3723-97qp.json"         # Assessor - Parcel Addresses
SOURCE_PAGE = CATALOG_URL + "/d/3r7i-mrz4"
SOURCE = "Cook County Assessor — residential condominium buildings (class 299)"
CONDO_CLASS = "299"
PAGE_SIZE = 50000
ADDRESS_BATCH = 250

STREET_SUFFIXES = {"ST": "St", "AVE": "Ave", "BLVD": "Blvd", "DR": "Dr", "RD": "Rd", "LN": "Ln", "CT": "Ct",
                   "PL": "Pl", "TER": "Ter", "PKWY": "Pkwy", "HWY": "Hwy", "CIR": "Cir", "TRL": "Trl",
                   "WAY": "Way", "SQ": "Sq", "N": "N", "S": "S", "E": "E", "W": "W", "NE": "NE", "NW": "NW",
                   "SE": "SE", "SW": "SW"}
HOUSE_NUMBER_RE = re.compile(r"^(\d+[A-Z]?(?:-\d+)?)\s+(.*)$")
UNIT_KEYWORD_RE = re.compile(r"(?:UNIT|APT|APARTMENT|STE|SUITE|#|NO\.?)")
UNIT_TOKEN_RE = re.compile(r"#?[A-Z]{1,2}|PH\w*|GAR\w*|BSMT")     # A, PH, GAR, BSMT, #B
ORDINAL_RE = re.compile(r"\d+(?:ST|ND|RD|TH)")
MUNICIPALITY_PREFIX_RE = re.compile(r"^(?:CITY|VILLAGE|TOWN) OF ")

log = logging.getLogger(__name__)


# ---- addresses ----------------------------------------------------------------

def is_unit_designator(words: list[str]) -> bool:
    """True when the last word is a unit designator: anything with a digit
    (2-E, A-10, 10AB, P2-65, 1FRON), a one- or two-letter token (A, PH), or
    whatever follows a unit keyword (UNIT 5A, APT 3107)."""
    last = words[-1]
    after_keyword = len(words) > 1 and UNIT_KEYWORD_RE.fullmatch(words[-2]) is not None
    return after_keyword or re.search(r"\d", last) is not None or UNIT_TOKEN_RE.fullmatch(last) is not None


def strip_unit(words: list[str]) -> list[str]:
    """`words` without the trailing unit designator and its keyword. The
    street's own last word is never taken: a street suffix stops it."""
    words = list(words)
    while len(words) > 1 and words[-1] not in STREET_SUFFIXES and is_unit_designator(words):
        words.pop()
        if words and UNIT_KEYWORD_RE.fullmatch(words[-1]):
            words.pop()
    return words


def pretty_word(word: str) -> str:
    """'DR' -> 'Dr', '46TH' -> '46th', 'LAKE' -> 'Lake'."""
    if word in STREET_SUFFIXES:
        return STREET_SUFFIXES[word]
    return word.lower() if ORDINAL_RE.fullmatch(word) else word.title()


def street_of(address: str) -> str:
    """'850 N LAKE SHORE DR 412' -> '850 N Lake Shore Dr': the house number
    and street with the unit designator stripped; '' when the text does not
    start with a house number."""
    squashed = " ".join((address or "").upper().split())
    match = HOUSE_NUMBER_RE.match(squashed)
    if not match:
        return ""
    number, rest = match.groups()
    words = strip_unit(rest.split())
    return f"{number} {' '.join(pretty_word(word) for word in words)}".strip()


def municipality_city(municipality: str, township: str) -> str:
    """'VILLAGE OF BARRINGTON' -> 'Barrington'; the township when the parcel has no municipality."""
    name = MUNICIPALITY_PREFIX_RE.sub("", (municipality or "").upper()).strip()
    return (name or township or "").title()


# ---- buildings ----------------------------------------------------------------

@dataclass
class Building:
    """The class-299 unit parcels that share one 10-digit building PIN."""

    pin10: str
    pins: list[str] = field(default_factory=list)
    latitudes: list[float] = field(default_factory=list)
    longitudes: list[float] = field(default_factory=list)
    zips: collections.Counter = field(default_factory=collections.Counter)
    municipalities: collections.Counter = field(default_factory=collections.Counter)
    township: str = ""


def group_units(units: list[dict]) -> dict[str, Building]:
    """{pin10: building} from the class-299 parcel rows."""
    buildings: dict[str, Building] = {}
    for unit in units:
        pin10 = unit.get("pin10") or (unit.get("pin") or "")[:10]
        if not pin10:
            continue
        building = buildings.setdefault(pin10, Building(pin10))
        building.pins.append(unit.get("pin", ""))
        try:
            building.latitudes.append(float(unit["lat"]))
            building.longitudes.append(float(unit["lon"]))
        except (KeyError, TypeError, ValueError):
            pass
        if unit.get("zip_code"):
            building.zips[str(unit["zip_code"])[:5]] += 1
        if unit.get("cook_municipality_name"):
            building.municipalities[unit["cook_municipality_name"]] += 1
        building.township = building.township or unit.get("township_name", "")
    return buildings


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def to_record(building: Building, characteristics: dict | None, address: dict | None,
              retrieved_at: str = "") -> Association | None:
    """The registry record for one building, or None when no street address
    is known for it (a building with no name cannot be listed)."""
    address = address or {}
    characteristics = characteristics or {}
    street = street_of(address.get("prop_address_full", ""))
    if not street:
        return None
    most_common_municipality = building.municipalities.most_common(1)[0][0] if building.municipalities else ""
    city = (address.get("prop_address_city_name") or "").title() \
        or municipality_city(most_common_municipality, building.township)
    zip_code = (address.get("prop_address_zipcode_1") or "")[:5] \
        or (building.zips.most_common(1)[0][0] if building.zips else "")
    unit_pins = len(building.pins)
    non_unit_pins = int(float(characteristics.get("max_char_building_non_units") or 0))
    year_built = characteristics.get("max_char_yrblt")
    year_built = str(int(float(year_built))) if year_built else ""
    detail = []
    if year_built:
        detail.append(f"built {year_built}")
    detail.append(f"{unit_pins} unit PIN{'s' if unit_pins != 1 else ''}"
                  + (f" ({non_unit_pins} parking/common)" if non_unit_pins else ""))
    return Association(
        state="IL", source=SOURCE, source_url=SOURCE_PAGE, record_id=building.pin10,
        name=f"{street} Condominium", status="condominium building (Assessor class 299)",
        status_detail="; ".join(detail), address=street, city=city, county="Cook", zip=zip_code,
        units=max(unit_pins - non_unit_pins, 1) if unit_pins else None,
        registration_type="county condo-building inventory", retrieved_at=retrieved_at,
        extras={"pin10": building.pin10, "year_built": year_built,
                "lat": mean(building.latitudes), "lon": mean(building.longitudes)})


# ---- fetching -----------------------------------------------------------------

def fetch_all(session: requests.Session, url: str, params: dict, pace: float) -> list[dict]:
    """Every row of a Socrata resource, paged; a failed page is retried four times."""
    rows: list[dict] = []
    offset = 0
    while True:
        page_params = {**params, "$limit": PAGE_SIZE, "$offset": offset}
        for attempt in range(4):
            try:
                response = session.get(url, params=page_params, timeout=300)
                response.raise_for_status()
                page = response.json()
                break
            except (requests.RequestException, ValueError) as exc:
                log.warning("%s offset %d: %s (attempt %d)", url.rsplit("/", 1)[1], offset, exc, attempt + 1)
                time.sleep(10 * (attempt + 1))
        else:
            raise RuntimeError(f"gave up on {url}")
        rows.extend(page)
        log.info("%s: %d rows", url.rsplit("/", 1)[1], len(rows))
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE
        time.sleep(pace)


def fetch_units(session: requests.Session, pace: float) -> list[dict]:
    """The class-299 parcels, from the week-old cache when there is one."""
    if PARCELS_CACHE.exists() and time.time() - PARCELS_CACHE.stat().st_mtime < CACHE_DAYS * 86400:
        return json.loads(PARCELS_CACHE.read_text())
    units = fetch_all(session, PARCELS_URL, {
        "$select": "pin,pin10,zip_code,lat,lon,cook_municipality_name,township_name",
        "$where": f"class='{CONDO_CLASS}'", "$order": "pin"}, pace)
    PARCELS_CACHE.write_text(json.dumps(units))
    return units


def fetch_characteristics(session: requests.Session, pace: float) -> dict[str, dict]:
    """{pin10: this year's characteristics row}."""
    year = datetime.now().year
    rows = fetch_all(session, CHARACTERISTICS_URL, {
        "$select": "pin10,max(char_yrblt),max(char_building_pins),max(char_building_non_units),max(char_building_sf)",
        "$where": f"year='{year}.0' OR year='{year}'", "$group": "pin10", "$order": "pin10"}, pace)
    return {row["pin10"]: row for row in rows}


def fetch_addresses(session: requests.Session, pins: list[str], pace: float) -> dict[str, dict]:
    """{pin: the newest address row} for the given unit PINs, in batches."""
    addresses: dict[str, dict] = {}
    pins = sorted(pins)
    for start in range(0, len(pins), ADDRESS_BATCH):
        batch = pins[start:start + ADDRESS_BATCH]
        rows = fetch_all(session, ADDRESSES_URL, {
            "$select": "pin,prop_address_full,prop_address_city_name,prop_address_zipcode_1,year",
            "$where": "pin in (" + ",".join(f"'{pin}'" for pin in batch) + ")",
            "$order": "year DESC"}, pace)
        for row in rows:
            addresses.setdefault(row["pin"], row)             # newest year first
        if (start // ADDRESS_BATCH) % 10 == 0:
            log.info("addresses: %d/%d buildings", min(start + ADDRESS_BATCH, len(pins)), len(pins))
        time.sleep(pace)
    return addresses


# ---- the command --------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=0, help="only this many buildings (smoke test)")
    parser.add_argument("--pace", type=float, default=0.5)


def run(args: argparse.Namespace) -> int:
    started = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    session = http.session()

    units = fetch_units(session, args.pace)
    buildings = group_units(units)
    log.info("%d condo unit PINs in %d buildings", len(units), len(buildings))
    characteristics = fetch_characteristics(session, args.pace)
    log.info("characteristics for %d buildings", len(characteristics))

    keys = sorted(buildings)
    if args.limit:
        keys = keys[:args.limit]
    first_pin = {min(buildings[key].pins): key for key in keys}
    addresses = fetch_addresses(session, list(first_pin), args.pace)

    retrieved_at = now_iso()
    records = []
    for pin, key in first_pin.items():
        record = to_record(buildings[key], characteristics.get(key), addresses.get(pin), retrieved_at)
        if record:
            records.append(record)
    records.sort(key=lambda record: record.name)
    write_rows(OUTPUT_PATH, (record.as_row() for record in records))
    update_coverage("IL", "condo_buildings", {
        "source": SOURCE, "url": SOURCE_PAGE, "records": len(records), "unit_pins": len(units),
        "coverage": "partial",
        "note": ("Every Assessor class-299 condominium building in Cook County, named by street "
                 "address (the data carries no association names): where condo associations "
                 "exist, not who runs them. Unit count = unit PINs minus parking/common PINs."),
        "retrieved_at": retrieved_at, "duration_seconds": round(time.time() - started, 1),
    })
    log.info("wrote %d buildings -> %s", len(records), OUTPUT_PATH.relative_to(ROOT))
    return 0
