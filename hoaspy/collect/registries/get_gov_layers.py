"""Local-government HOA and condominium inventories — county and city
ArcGIS layers, Socrata datasets, CSV/XLSX downloads — for states with no
statewide registry.

    python -m hoaspy gov-layers                   # every layer in gov_layers/
    python -m hoaspy gov-layers --state MA MI     # some states
    python -m hoaspy gov-layers --list

One YAML per state in gov_layers/<ST>.yml (a list of layers) maps each
layer to the registry row shape:

    - kind: arcgis                      # arcgis | socrata | csv
      source: "MassGIS statewide parcels — condominium trusts (owner name)"
      page: "https://..."               # the page a person would open (source_url)
      url: "https://.../FeatureServer/0"  # layer root (arcgis) / resource (socrata) / file (csv)
      dbf: ownership/ownership.dbf        # csv kind: a .zip of a shapefile -> rows of this .dbf (default: the first)
      where: "OWNER1 LIKE '%CONDOMINIUM TRUST%'"   # arcgis/socrata server-side filter
      # (arcgis: a list of filters is fetched one after another, for services
      #  whose per-query timeout forces partitioning, e.g. by OBJECTID range)
      fields: {name: OWNER1, address: SITE_ADDR, city: CITY, zip: ZIP, units: UNITS,
               record_id: LOC_ID, status: TYPE, manager: MGMT, county: COUNTY}
      constants: {county: New Castle, status: "maintenance corporation"}
      name_filter: true                 # keep only association-looking names (default false)
      name_regex: "^(.*?) CONDOMINIUM"  # optional: keep/extract group 1 of a match
      exclude_name: "BANK|MORTGAGE"     # optional: drop matching names
      group_by_name: true               # collapse parcel rows to one per name (default true)

Names are upper-cased; rows sharing a normalized name within one state are
merged (the first row wins, `parcels` counts the duplicates). A column
whose name says phone or e-mail is refused.

Output: records/gov_layers.jsonl in the registry shape plus `layer_kind`,
`parcels` and `also_listed_by` (`source` is the layer's label); coverage.json
collected["local_inventory"] per state, marked partial — a county or city
inventory says nothing about the rest of the state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import re
import struct
import time
import zipfile
from collections import Counter

import requests
import yaml

from hoaspy import ROOT
from hoaspy.collect.registries import arcgis
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, replace_rows
from hoaspy.lib.names import REGISTRY_NAME_RE
from hoaspy.lib.records import Association

CONFIG_DIR = ROOT / "gov_layers"
OUTPUT_PATH = ROOT / "records" / "gov_layers.jsonl"
CACHE_DIR = ROOT / ".cache" / "gov_layers"
SOCRATA_PAGE_SIZE = 5000
MIN_NAME_LENGTH = 3

FORBIDDEN_COLUMN_RE = re.compile(r"phone|tel\b|email|e_mail|mail_addr|cell|fax", re.I)
NAME_NOISE_RE = re.compile(
    r"\b(INC|THE|A|AN|OF|LLC|CORP|CORPORATION|TRUST|TRUSTEES?|ASSN|ASSOC|ASSOCIATION|"
    r"HOMEOWNERS|OWNERS|HOA|COMMUNITY|CONDOMINIUM|CONDO)\b")
ADDRESS_LINE_RE = re.compile(r"\d|\bP\.? ?O\.? BOX\b|\bSTE\b|\bSUITE\b|\bUNIT\b|\bAPT\b|\bFLOOR\b|\bBLDG\b", re.I)
# The output columns a layer's `fields` map onto.
MAPPED_COLUMNS = ("record_id", "status", "status_detail", "recorded_date", "address", "city", "county", "zip",
                  "units", "manager_name")
# Columns a duplicate row may fill in on the row that was kept.
FILLED_FROM_DUPLICATES = ("address", "city", "zip", "units", "manager_name", "record_id")
FILLED_ACROSS_LAYERS = ("address", "city", "zip", "units", "manager_name", "county")

log = logging.getLogger(__name__)


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def unit_count(value) -> int | None:
    try:
        count = int(float(value))
        return count if count > 0 else None
    except (TypeError, ValueError):
        return None


def name_key(name: str) -> str:
    """The name with punctuation and the association words taken out."""
    key = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    return " ".join(NAME_NOISE_RE.sub(" ", key).split())


# ---- fetchers ---------------------------------------------------------------

def fetch_arcgis(session: requests.Session, cfg: dict, pace: float) -> list[dict]:
    """The layer's features; a list-valued `where` is fetched filter by filter."""
    filters = cfg.get("where") or "1=1"
    if isinstance(filters, str):
        filters = [filters]
    out: list[dict] = []
    for where in filters:
        out += arcgis.query_features(
            session, cfg["url"], where=where, out_fields=cfg.get("out_fields", "*"),
            order_by=(cfg.get("oid") or "OBJECTID") if cfg.get("order", True) else None,
            page_size=int(cfg.get("page_size") or arcgis.DEFAULT_PAGE_SIZE), pace=pace)
        if len(filters) > 1:
            log.debug("%s: %d rows after filter %r", cfg["source"][:40], len(out), where[:60])
            time.sleep(pace)
    return out


def fetch_socrata(session: requests.Session, cfg: dict, pace: float) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        params = {"$limit": SOCRATA_PAGE_SIZE, "$offset": offset}
        if cfg.get("where"):
            params["$where"] = cfg["where"]
        response = session.get(cfg["url"], params=params, timeout=180)
        response.raise_for_status()
        rows = response.json()
        out.extend(rows)
        offset += len(rows)
        if len(rows) < SOCRATA_PAGE_SIZE:
            return out
        time.sleep(pace)


def fetch_csv(session: requests.Session, cfg: dict, pace: float) -> list[dict]:
    """A downloaded file (cached under .cache/gov_layers/): the rows of a
    shapefile zip's .dbf, an XLSX sheet, or delimited text."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / (hashlib.sha1(cfg["url"].encode()).hexdigest()[:10] + "_"
                         + re.sub(r"[^A-Za-z0-9._-]", "_", cfg["url"].split("/")[-1] or "file"))
    if not local.exists():
        response = session.get(cfg["url"], timeout=300)
        response.raise_for_status()
        local.write_bytes(response.content)
    content = local.read_bytes()
    if content[:2] == b"PK" and (cfg.get("dbf") or local.suffix.lower() == ".zip"):
        return read_zip_dbf(content, cfg.get("dbf"))
    if cfg.get("xlsx") or local.suffix.lower() in (".xlsx", ".xls"):
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        sheet = workbook[cfg["sheet"]] if cfg.get("sheet") else workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        for _ in range(int(cfg.get("skip_rows") or 0)):
            next(rows)
        header = [text(cell) for cell in next(rows)]
        return [dict(zip(header, row, strict=False)) for row in rows]
    decoded = content.decode(cfg.get("encoding", "utf-8-sig"), "replace")
    return list(csv.DictReader(io.StringIO(decoded), delimiter=cfg.get("delimiter", ",")))


def read_zip_dbf(content: bytes, member: str | None) -> list[dict]:
    """The records of a shapefile zip's .dbf (the first, or the named
    member), character and numeric fields as stripped strings, deleted
    records skipped."""
    archive = zipfile.ZipFile(io.BytesIO(content))
    name = member or next(name for name in archive.namelist() if name.lower().endswith(".dbf"))
    data = archive.read(name)
    record_count, header_length, record_length = struct.unpack("<xxxxIHH", data[:12])
    fields, position = [], 32
    while data[position] != 0x0D:
        fields.append((data[position:position + 11].split(b"\0")[0].decode("ascii"), data[position + 16]))
        position += 32
    out = []
    for index in range(record_count):
        record = data[header_length + index * record_length: header_length + (index + 1) * record_length]
        if record[:1] == b"*":
            continue
        row, offset = {}, 1
        for field_name, length in fields:
            row[field_name] = record[offset:offset + length].decode("latin-1").strip()
            offset += length
        out.append(row)
    return out


FETCHERS = {"arcgis": fetch_arcgis, "socrata": fetch_socrata, "csv": fetch_csv}


# ---- rows -------------------------------------------------------------------

def column_text(row: dict, spec) -> str:
    """The text of a mapped column: a column name, or a list of columns joined with spaces."""
    if not spec:
        return ""
    if isinstance(spec, list):
        return " ".join(text(row.get(column)) for column in spec).strip()
    return text(row.get(spec))


def mapped_value(row: dict, fields: dict, constants: dict, key: str) -> str:
    """The value of one output column: the mapped source column, else the configured constant."""
    column = fields.get({"manager_name": "manager"}.get(key, key)) or fields.get(key)
    value = column_text(row, column) if column else ""
    if not value and key in constants:
        value = text(constants[key])
    return value


def clean_city(city: str) -> str:
    """'' for a city column that holds a street line or a PO box (the NC
    OneMap mailing-city column does, for ~2k parcels)."""
    city = " ".join((city or "").split())
    return "" if ADDRESS_LINE_RE.search(city) else city


def to_rows(state: str, cfg: dict, raw: list[dict], retrieved_at: str = "") -> list[Association]:
    """The layer's rows as registry records, one per association name."""
    fields = cfg.get("fields") or {}
    for column in fields.values():
        for name in (column if isinstance(column, list) else [column]):
            if FORBIDDEN_COLUMN_RE.search(str(name)):
                raise ValueError(f"{state} {cfg['source']}: refusing to map contact column {name!r}")
    constants = cfg.get("constants") or {}
    name_filter = bool(cfg.get("name_filter"))
    group = cfg.get("group_by_name", True)
    exclude = re.compile(cfg["exclude_name"], re.I) if cfg.get("exclude_name") else None
    merged: dict[str, Association] = {}
    kept = 0
    for row in raw:
        name = column_text(row, fields.get("name")).upper()
        if cfg.get("name_regex"):
            match = re.search(cfg["name_regex"], name)
            if not match:
                continue
            name = (match.group(1) if match.groups() else match.group(0)).strip()
        if not name or len(name) < MIN_NAME_LENGTH:
            continue
        if name_filter and not REGISTRY_NAME_RE.search(name):
            continue
        if exclude and exclude.search(name):
            continue
        kept += 1
        key = name_key(name) if group else f"{name_key(name)}|{kept}"
        value = {column: mapped_value(row, fields, constants, column) for column in MAPPED_COLUMNS}
        if key in merged:
            found = merged[key]
            found.extras["parcels"] += 1
            for column in FILLED_FROM_DUPLICATES:
                if not getattr(found, column):
                    setattr(found, column, unit_count(value[column]) if column == "units" else value[column])
            continue
        merged[key] = Association(
            state=state, source=cfg["source"], source_url=cfg.get("page") or cfg["url"],
            record_id=value["record_id"], name=name, status=value["status"], status_detail=value["status_detail"],
            recorded_date=value["recorded_date"][:10], address=value["address"],
            city=clean_city(value["city"]).title(), county=value["county"].title().replace(" County", ""),
            zip=value["zip"][:5], units=unit_count(value["units"]), manager_name=value["manager_name"].title(),
            retrieved_at=retrieved_at,
            extras={"layer_kind": cfg.get("kind"), "parcels": 1, "also_listed_by": []})
    records = list(merged.values())
    log.info("%s %s: %d raw -> %d kept -> %d associations", state, cfg["source"][:60], len(raw), kept, len(records))
    return records


def merge_state(records: list[Association]) -> list[Association]:
    """One record per association across a state's layers, on normalized
    name. The same name in two cities is two associations (Tulsa's and
    OKC's "Country Oaks II HOA"): rows merge only when a city is missing on
    one side or both agree."""
    seen: dict[str, Association] = {}
    for record in records:
        key = name_key(record.name)
        city = (record.city or "").upper()
        if key in seen and city and (seen[key].city or "").upper() not in ("", city):
            key = f"{key}|{city}"
        if key in seen:
            found = seen[key]
            if record.source != found.source and record.source not in found.extras["also_listed_by"]:
                found.extras["also_listed_by"].append(record.source)
            for column in FILLED_ACROSS_LAYERS:
                if not getattr(found, column) and getattr(record, column):
                    setattr(found, column, getattr(record, column))
            continue
        seen[key] = record
    return list(seen.values())


# ---- the command ------------------------------------------------------------

def load_config() -> dict[str, list]:
    return {path.stem.upper(): yaml.safe_load(path.read_text()) or [] for path in sorted(CONFIG_DIR.glob("*.yml"))}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", nargs="+", help="limit to these state codes")
    parser.add_argument("--list", action="store_true", help="list the configured layers and exit")
    parser.add_argument("--pace", type=float, default=0.5)


def run(args: argparse.Namespace) -> int:
    config = load_config()
    if args.list:
        for state, layers in sorted(config.items()):
            for layer in layers or []:
                print(f"{state}  {layer['kind']:8} {layer['source']}")
        return 0

    session = http.session()
    retrieved_at = now_iso()
    fresh: list[Association] = []
    states_done: set[str] = set()
    failures: list[str] = []
    for state, layers in sorted(config.items()):
        if args.state and state not in {code.upper() for code in args.state}:
            continue
        state_records: list[Association] = []
        fetched: list[dict] = []
        for cfg in layers or []:
            label = f"{state} {cfg['kind']} {cfg['source'][:50]}"
            try:
                raw = FETCHERS[cfg["kind"]](session, cfg, args.pace)
                records = to_rows(state, cfg, raw, retrieved_at)
            except Exception as exc:
                log.error("%s FAILED: %s: %s", label, type(exc).__name__, exc)
                failures.append(label)
                continue
            fetched.append({"source": cfg["source"], "records": len(records), "url": cfg.get("page") or cfg["url"]})
            state_records.extend(records)
            time.sleep(args.pace)
        if not state_records:
            continue
        state_records = merge_state(state_records)
        fresh.extend(state_records)
        states_done.add(state)
        update_coverage(state, "local_inventory", {
            "source": "; ".join(f"{layer['source']} ({layer['records']})" for layer in fetched),
            "records": len(state_records), "url": fetched[0]["url"], "coverage": "partial",
            "note": "county/city inventories, not a statewide registry — absence means nothing"})

    if states_done:
        total = replace_rows(OUTPUT_PATH, (record.as_row() for record in fresh), states_done)
        log.info("%s: %d rows total (%d fresh from %s)", OUTPUT_PATH.name, total, len(fresh),
                 ", ".join(sorted(states_done)))
    for state, count in sorted(Counter(record.state for record in fresh).items()):
        print(f"  {state}: {count:,} associations from local-government layers")
    if failures:
        print("FAILED: " + "; ".join(failures))
        return 1
    return 0
