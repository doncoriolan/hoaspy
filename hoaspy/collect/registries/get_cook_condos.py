#!/usr/bin/env python
"""Cook County, IL condominium buildings from the Assessor's open data.

    ./venv/bin/python -m hoaspy.collect.registries.get_cook_condos
    ./venv/bin/python -m hoaspy.collect.registries.get_cook_condos --limit 500   # smoke test

Illinois has no association registry, and the Cook County recorder's index
names an association only on the documents it is a party to (get_cook_liens
reads those), but the Assessor classifies every residential condominium
unit (class 299) and publishes the parcels with coordinates, plus a unit
characteristics table and a parcel-address table. Grouping the unit PINs by
their 10-digit building PIN gives every condo building in the county:

    pabr-t5kh  Assessor - Parcel Universe (current year)    class='299' -> pin, pin10,
               zip, lat/lon, municipality, township
    3r7i-mrz4  Assessor - Residential Condominium Unit Characteristics -> per pin10:
               year built, building PIN count, non-unit (parking/common) PINs, sq ft
    3723-97qp  Assessor - Parcel Addresses -> the street address of one unit per
               building (the unit designator is stripped)

The dataset carries no association names, so each building is listed as
"<street address> Condominium" — an inventory of where condominium
associations exist, not who runs them. Owner and mailing names on the
address table are never read. Output: records/cook_condos.jsonl (registry
shape + `lat`, `lon`, `pin10`, `year_built`), folded by build_site as a
county inventory (`local_inventory` marking); coverage.json
collected["condo_buildings"] for IL.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from hoaspy import ROOT
from hoaspy.collect.registries.get_states import update_coverage

OUT = ROOT / "records" / "cook_condos.jsonl"
CACHE = ROOT / ".cache" / "cook_condos"
DOMAIN = "https://datacatalog.cookcountyil.gov"
UNIVERSE = DOMAIN + "/resource/pabr-t5kh.json"
CHARS = DOMAIN + "/resource/3r7i-mrz4.json"
ADDRESSES = DOMAIN + "/resource/3723-97qp.json"
PAGE = DOMAIN + "/d/3r7i-mrz4"
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
SOURCE = "Cook County Assessor — residential condominium buildings (class 299)"
PAGE_SIZE = 50000

log = logging.getLogger("cook")

UNIT_RE = re.compile(r"\s+(?:UNIT|APT|APARTMENT|STE|SUITE|#|NO\.?|PH|GAR|P|G|B|BSMT)?\s*#?\s*"
                     r"([A-Z]?\d+[A-Z]?|[A-Z]{1,2}\d*|PH\d*|\d+[A-Z]?-\d+[A-Z]?)\s*$", re.I)
SUFFIX = {"ST": "St", "AVE": "Ave", "BLVD": "Blvd", "DR": "Dr", "RD": "Rd", "LN": "Ln", "CT": "Ct",
          "PL": "Pl", "TER": "Ter", "PKWY": "Pkwy", "HWY": "Hwy", "CIR": "Cir", "TRL": "Trl",
          "WAY": "Way", "SQ": "Sq", "N": "N", "S": "S", "E": "E", "W": "W", "NE": "NE", "NW": "NW",
          "SE": "SE", "SW": "SW"}


def street_of(addr: str) -> str:
    """'850 N LAKE SHORE DR 412' -> '850 N Lake Shore Dr' (unit stripped)."""
    a = " ".join((addr or "").upper().split())
    m = re.match(r"^(\d+[A-Z]?(?:-\d+)?)\s+(.*)$", a)
    if not m:
        return ""
    num, rest = m.groups()
    # strip a trailing unit designator, but never the street's own last word
    words = rest.split()
    while len(words) > 1:
        last = words[-1]
        if last in SUFFIX:
            break
        # a unit designator: anything with a digit (2-E, A-10, 10AB, P2-65, 1FRON),
        # a one/two-letter token (A, PH), or a unit keyword before it
        if re.fullmatch(r"(?:UNIT|APT|APARTMENT|STE|SUITE|#|NO\.?)", words[-2] if len(words) > 1 else "") \
                or re.search(r"\d", last) or re.fullmatch(r"#?[A-Z]{1,2}|PH\w*|GAR\w*|BSMT", last):
            words.pop()
            if words and re.fullmatch(r"(?:UNIT|APT|APARTMENT|STE|SUITE|#|NO\.?)", words[-1]):
                words.pop()
            continue
        break
    pretty = [SUFFIX.get(w, w.lower() if re.fullmatch(r"\d+(?:ST|ND|RD|TH)", w) else w.title()) for w in words]
    return f"{num} {' '.join(pretty)}".strip()


def fetch_all(session: requests.Session, url: str, params: dict, pace: float) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        p = {**params, "$limit": PAGE_SIZE, "$offset": offset}
        for attempt in range(4):
            try:
                r = session.get(url, params=p, timeout=300)
                r.raise_for_status()
                page = r.json()
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


def group_units(units: list[dict]) -> dict[str, dict]:
    """{pin10: building} from class-299 parcel rows."""
    b: dict[str, dict] = {}
    for u in units:
        p10 = u.get("pin10") or (u.get("pin") or "")[:10]
        if not p10:
            continue
        g = b.setdefault(p10, {"pin10": p10, "pins": [], "lat": [], "lon": [], "zip": collections.Counter(),
                               "muni": collections.Counter(), "township": ""})
        g["pins"].append(u.get("pin", ""))
        try:
            g["lat"].append(float(u["lat"]))
            g["lon"].append(float(u["lon"]))
        except (KeyError, TypeError, ValueError):
            pass
        if u.get("zip_code"):
            g["zip"][str(u["zip_code"])[:5]] += 1
        if u.get("cook_municipality_name"):
            g["muni"][u["cook_municipality_name"]] += 1
        g["township"] = g["township"] or u.get("township_name", "")
    return b


def muni_city(muni: str, township: str) -> str:
    m = re.sub(r"^(?:CITY|VILLAGE|TOWN) OF ", "", (muni or "").upper()).strip()
    return (m or township or "").title()


def to_row(g: dict, chars: dict | None, address: dict | None) -> dict | None:
    street = street_of((address or {}).get("prop_address_full", ""))
    if not street:
        return None
    city = ((address or {}).get("prop_address_city_name") or "").title() or muni_city(
        g["muni"].most_common(1)[0][0] if g["muni"] else "", g["township"])
    zc = ((address or {}).get("prop_address_zipcode_1") or "")[:5] or (
        g["zip"].most_common(1)[0][0] if g["zip"] else "")
    n_units = len(g["pins"])
    non_units = int(float((chars or {}).get("max_char_building_non_units") or 0))
    yr = (chars or {}).get("max_char_yrblt")
    yr = str(int(float(yr))) if yr else ""
    bits = []
    if yr:
        bits.append(f"built {yr}")
    bits.append(f"{n_units} unit PIN{'s' if n_units != 1 else ''}" +
                (f" ({non_units} parking/common)" if non_units else ""))
    return {
        "state": "IL", "source": SOURCE, "source_url": PAGE,
        "record_id": g["pin10"], "pin10": g["pin10"],
        "name": f"{street} Condominium", "dba": "",
        "status": "condominium building (Assessor class 299)",
        "status_detail": "; ".join(bits), "recorded_date": "",
        "address": street, "city": city, "county": "Cook", "zip": zc,
        "units": max(n_units - non_units, 1) if n_units else None,
        "manager_name": "", "officers": [],
        "registration_type": "county condo-building inventory",
        "year_built": yr,
        "lat": round(sum(g["lat"]) / len(g["lat"]), 6) if g["lat"] else None,
        "lon": round(sum(g["lon"]) / len(g["lon"]), 6) if g["lon"] else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only this many buildings (smoke test)")
    ap.add_argument("--pace", type=float, default=0.5)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    started = time.time()
    CACHE.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    units_p = CACHE / "units.json"
    if units_p.exists() and time.time() - units_p.stat().st_mtime < 7 * 86400:
        units = json.loads(units_p.read_text())
    else:
        units = fetch_all(s, UNIVERSE, {
            "$select": "pin,pin10,zip_code,lat,lon,cook_municipality_name,township_name",
            "$where": "class='299'", "$order": "pin"}, args.pace)
        units_p.write_text(json.dumps(units))
    buildings = group_units(units)
    log.info("%d condo unit PINs in %d buildings", len(units), len(buildings))

    chars_rows = fetch_all(s, CHARS, {
        "$select": "pin10,max(char_yrblt),max(char_building_pins),max(char_building_non_units),max(char_building_sf)",
        "$where": f"year='{datetime.now().year}.0' OR year='{datetime.now().year}'",
        "$group": "pin10", "$order": "pin10"}, args.pace)
    chars = {r["pin10"]: r for r in chars_rows}
    log.info("characteristics for %d buildings", len(chars))

    keys = sorted(buildings)
    if args.limit:
        keys = keys[:args.limit]
    first_pin = {min(buildings[k]["pins"]): k for k in keys}
    addr: dict[str, dict] = {}
    pins = sorted(first_pin)
    for i in range(0, len(pins), 250):
        chunk = pins[i:i + 250]
        rows = fetch_all(s, ADDRESSES, {
            "$select": "pin,prop_address_full,prop_address_city_name,prop_address_zipcode_1,year",
            "$where": "pin in (" + ",".join(f"'{p}'" for p in chunk) + ")",
            "$order": "year DESC"}, args.pace)
        for r in rows:
            addr.setdefault(r["pin"], r)             # newest year first
        if (i // 250) % 10 == 0:
            log.info("addresses: %d/%d buildings", min(i + 250, len(pins)), len(pins))
        time.sleep(args.pace)

    out_rows = []
    for pin, k in first_pin.items():
        row = to_row(buildings[k], chars.get(k), addr.get(pin))
        if row:
            out_rows.append(row)
    out_rows.sort(key=lambda r: r["name"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT)
    update_coverage("IL", "condo_buildings", {
        "source": SOURCE, "url": PAGE, "records": len(out_rows),
        "unit_pins": len(units), "coverage": "partial",
        "note": ("Every Assessor class-299 condominium building in Cook County, named by street "
                 "address (the data carries no association names): where condo associations "
                 "exist, not who runs them. Unit count = unit PINs minus parking/common PINs."),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 1),
    })
    log.info("wrote %d buildings -> %s", len(out_rows), OUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
