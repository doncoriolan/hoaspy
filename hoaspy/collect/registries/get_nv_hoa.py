"""Nevada common-interest communities from local-government GIS layers.

    python -m hoaspy nv-hoa

NRED (the state regulator) registers every association but publishes no
roster, and its lookup app demands an image captcha per query. Three
governments publish their HOA inventories as public ArcGIS layers, and
Clark County's carries NRED's own schema (association type, unit count,
Secretary of State file number, mailing address):

    Clark County   AdminServ/Clark_County_HOA/FeatureServer/0   (~1,000 named)
    Henderson      public/HOAs/MapServer/1 (HOAs) and /0 (master associations)
    Las Vegas      CommunityServices/CLV_NeighAreas/MapServer/0 (HOA and master types)

Phone numbers and personal names on the layers are never read; management
company names are. Rows are merged across layers on the SoS file number
when both sides have one, else on the normalized name.

Output: Nevada's rows in records/state_registries.jsonl (replaced on each
run) in the registry shape plus `sos_file_number`, `corp_number`, `gated`
and `also_listed_by`; coverage.json collected["hoa_registry"] for NV,
noting the county-level scope (Washoe, Reno and Carson City publish no
layer).
"""

from __future__ import annotations

import argparse
import logging
import re
import time

from hoaspy.collect.registries import REGISTRIES_PATH, arcgis
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, replace_rows
from hoaspy.lib.records import Association

log = logging.getLogger(__name__)

LAYERS = {
    "clark": {
        "source": "Clark County NV GIS — HOA inventory (NRED registration schema)",
        "url": "https://maps.clarkcountynv.gov/arcgis/rest/services/AdminServ/Clark_County_HOA/FeatureServer/0",
        "where": "Name IS NOT NULL",
        "fields": "OBJECTID,Name,Assn_Type,F__of_Units,SOS_,GATED,Address1,Address2,City,State,Zip_Code,County,NOTES",
    },
    "henderson": {
        "source": "City of Henderson NV GIS — homeowners associations",
        "url": "https://maps.cityofhenderson.com/arcgis/rest/services/public/HOAs/MapServer/1",
        "where": "1=1",
        "fields": "OBJECTID,NAME,AKA,MASTER_ASSOCIATION,MASTER,INACTIVE,GATED,AGE_RESTRICTED,NUM_UNITS,FILE_,"
                  "SOURCE,DATE_UPDATED,MANAGEMENT_COMPANY,MANAGER_CITY",
    },
    "henderson_master": {
        "source": "City of Henderson NV GIS — master associations",
        "url": "https://maps.cityofhenderson.com/arcgis/rest/services/public/HOAs/MapServer/0",
        "where": "1=1",
        "fields": "OBJECTID,NAME,AKA,MASTER_ASSOCIATION,MANAGEMENT_COMPANY,MANAGER_CITY,AGE_RESTRICTED",
    },
    "las_vegas": {
        "source": "City of Las Vegas GIS — registered neighborhood associations (HOA/master types)",
        "url": "https://mapdata.lasvegasnevada.gov/clvgis/rest/services/CommunityServices/CLV_NeighAreas/MapServer/0",
        "where": "NTYPE IN ('HOA','MA','LMA','MA/H*','LMA/*')",
        "fields": "OBJECTID,ASSOC_ID,ASSOC_NAME,NTYPE,REGISTERED,CORP_NO,GATED,AGE_RESTRICTED,CITY1,ZIP1,UPDATED",
    },
}

CLARK_TYPES = {"REG": "registered association", "SUB": "sub-association",
               "MSTR": "master association", "SAM": "sub-association (master-managed)"}
LAS_VEGAS_TYPES = {"HOA": "homeowners association", "MA": "master association",
                   "LMA": "master association (large)", "MA/H*": "master association / HOA",
                   "LMA/*": "master association (large)"}
# Columns a duplicate row may fill in on the row that was kept.
FILLED_FROM_DUPLICATES = ("units", "manager_name", "address", "zip", "dba")
NAME_NOISE_RE = re.compile(r"\b(INC|THE|A|AN|OF|LLC|ASSN|ASSOC|ASSOCIATION|HOMEOWNERS|OWNERS|HOA|COMMUNITY)\b")


def text(value) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def unit_count(value) -> int | None:
    try:
        count = int(float(value))
        return count if count > 0 else None
    except (TypeError, ValueError):
        return None


def epoch_day(milliseconds) -> str:
    """An ArcGIS date (milliseconds since the epoch) as YYYY-MM-DD; '' when it is not one."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(milliseconds) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


def is_gated(value) -> bool | None:
    return text(value).upper() == "Y" or None


def clark_record(attributes: dict, layer: dict, retrieved_at: str = "") -> Association | None:
    name = text(attributes.get("Name")).upper()
    if not name:
        return None
    address1, address2 = text(attributes.get("Address1")), text(attributes.get("Address2"))
    manager = ""
    if address1.upper().startswith("C/O"):
        manager = address1[3:].strip(" :").title()
        address1 = ""
    notes = text(attributes.get("NOTES"))
    return Association(
        state="NV", source=layer["source"], source_url=layer["url"],
        record_id=text(attributes.get("SOS_")) or f"clark-{attributes.get('OBJECTID')}", name=name,
        status=CLARK_TYPES.get(text(attributes.get("Assn_Type")), text(attributes.get("Assn_Type"))),
        status_detail=notes.title() if notes else "",
        address=", ".join(part for part in (address1, address2) if part),
        city=text(attributes.get("City")).title(), county="Clark", zip=text(attributes.get("Zip_Code"))[:5],
        units=unit_count(attributes.get("F__of_Units")), manager_name=manager, retrieved_at=retrieved_at,
        extras={"sos_file_number": text(attributes.get("SOS_")), "corp_number": "",
                "gated": is_gated(attributes.get("GATED"))})


def henderson_record(attributes: dict, layer: dict, master: bool, retrieved_at: str = "") -> Association | None:
    name = text(attributes.get("NAME")).upper()
    if not name:
        return None
    inactive = text(attributes.get("INACTIVE")).upper() == "Y"
    parent = text(attributes.get("MASTER_ASSOCIATION"))
    if master or parent.upper() == "MASTER ASSOCIATION":
        parent = ""
    return Association(
        state="NV", source=layer["source"], source_url=layer["url"],
        record_id=text(attributes.get("FILE_")) or f"henderson-{'m' if master else 'h'}-{attributes.get('OBJECTID')}",
        name=name, dba=text(attributes.get("AKA")),
        status=("inactive" if inactive else "active") + (" master association" if master else " association"),
        status_detail=f"sub-association of {parent.title()}" if parent else "",
        recorded_date=epoch_day(attributes.get("DATE_UPDATED")), city="Henderson", county="Clark",
        units=unit_count(attributes.get("NUM_UNITS")),
        manager_name=text(attributes.get("MANAGEMENT_COMPANY")).title(), retrieved_at=retrieved_at,
        extras={"sos_file_number": text(attributes.get("FILE_")), "corp_number": "",
                "gated": is_gated(attributes.get("GATED"))})


def las_vegas_record(attributes: dict, layer: dict, retrieved_at: str = "") -> Association | None:
    name = text(attributes.get("ASSOC_NAME")).upper()
    if not name:
        return None
    kind = text(attributes.get("NTYPE"))
    updated = text(attributes.get("UPDATED"))
    return Association(
        state="NV", source=layer["source"], source_url=layer["url"],
        record_id=(text(attributes.get("CORP_NO"))
                   or f"lasvegas-{attributes.get('ASSOC_ID') or attributes.get('OBJECTID')}"),
        name=name,
        status=("registered " if text(attributes.get("REGISTERED")).upper() == "Y" else "")
               + LAS_VEGAS_TYPES.get(kind, kind),
        status_detail=f"city record updated {updated}" if updated else "",
        city=text(attributes.get("CITY1")).title() or "Las Vegas", county="Clark",
        zip=text(attributes.get("ZIP1"))[:5], retrieved_at=retrieved_at,
        extras={"sos_file_number": "", "corp_number": text(attributes.get("CORP_NO")),
                "gated": is_gated(attributes.get("GATED"))})


RECORD_OF = {
    "clark": lambda attributes, layer, now: clark_record(attributes, layer, now),
    "henderson": lambda attributes, layer, now: henderson_record(attributes, layer, False, now),
    "henderson_master": lambda attributes, layer, now: henderson_record(attributes, layer, True, now),
    "las_vegas": lambda attributes, layer, now: las_vegas_record(attributes, layer, now),
}


def name_key(name: str) -> str:
    """The name with punctuation and the association words taken out."""
    key = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    return " ".join(NAME_NOISE_RE.sub(" ", key).split())


def merge(records: list[Association]) -> list[Association]:
    """One record per association: the same SoS file number, else the same
    normalized name. The first record wins; a later one fills in what it
    lacks, and its source goes into `also_listed_by`."""
    by_sos: dict[str, Association] = {}
    by_name: dict[str, Association] = {}
    out: list[Association] = []
    for record in records:
        sos_number = record.extras.get("sos_file_number") or ""
        key = name_key(record.name)
        found = by_sos.get(sos_number) if sos_number else None
        if found is None and key:
            found = by_name.get(key)
        if found is None:
            out.append(record)
            if sos_number:
                by_sos[sos_number] = record
            if key:
                by_name.setdefault(key, record)
            continue
        for column in FILLED_FROM_DUPLICATES:
            if not getattr(found, column) and getattr(record, column):
                setattr(found, column, getattr(record, column))
        for column in ("sos_file_number", "corp_number"):
            if not found.extras.get(column) and record.extras.get(column):
                found.extras[column] = record.extras[column]
        also = found.extras.setdefault("also_listed_by", [])
        if record.source not in also and record.source != found.source:
            also.append(record.source)
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pace", type=float, default=0.5)


def run(args: argparse.Namespace) -> int:
    session = http.session()
    retrieved_at = now_iso()
    records: list[Association] = []
    per_layer = {}
    for key, layer in LAYERS.items():
        features = arcgis.query_features(session, layer["url"], where=layer["where"], out_fields=layer["fields"],
                                         pace=args.pace, timeout=120)
        found = [record for record in (RECORD_OF[key](attributes, layer, retrieved_at) for attributes in features)
                 if record]
        per_layer[key] = len(found)
        records.extend(found)
        log.info("%s: %d features -> %d associations", key, len(features), len(found))
        time.sleep(args.pace)

    merged = merge(records)
    for record in merged:
        record.extras.setdefault("also_listed_by", [])
    total = replace_rows(REGISTRIES_PATH, (record.as_row() for record in merged), {"NV"})
    update_coverage("NV", "hoa_registry", {
        "source": "Clark County / Henderson / Las Vegas GIS HOA layers (NRED-registered associations)",
        "records": len(merged), "url": LAYERS["clark"]["url"], "coverage": "partial",
        "note": ("Clark County only (" + ", ".join(f"{key} {count}" for key, count in per_layer.items())
                 + f"; {len(records)} rows merged to {len(merged)}). NRED registers ~3,700 statewide "
                 "but publishes no roster; Washoe/Carson/Douglas (~700) have no public layer"),
    })
    log.info("%s: %d rows total, %d NV", REGISTRIES_PATH.name, total, len(merged))
    print(f"  NV: {len(merged):,} registry records ({len(records)} layer rows)")
    return 0
