#!/usr/bin/env python
"""Nevada common-interest communities from local-government GIS layers.

    ./venv/bin/python -m hoaspy.collect.registries.get_nv_hoa

NRED (the state regulator) registers every association but publishes no
roster, and its lookup app demands an image captcha per query.
Three governments do publish their HOA inventories as public ArcGIS
REST layers, and Clark County's carries NRED's own schema (association
type, unit count, Secretary of State file number, mailing address):

    Clark County   AdminServ/Clark_County_HOA/FeatureServer/0   (~1,000 named)
    Henderson      public/HOAs/MapServer/1 (HOAs) and /0 (masters)
    Las Vegas      CommunityServices/CLV_NeighAreas/MapServer/0 (HOA/master types)

Contact phone numbers and any personal names on the layers are NOT stored;
management-company names are. Rows are merged across layers on the SoS
file number when both sides have one, else on the normalized name.

Output: records/state_registries.jsonl (NV rows replaced), registry shape;
coverage.json collected["hoa_registry"] for NV, noting the county-level
scope (Washoe/Reno/Carson City publish no layer).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

import requests

from hoaspy.collect.registries.get_states import REGS, update_coverage, write_merged

from hoaspy import ROOT
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
log = logging.getLogger("nv")

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
        "fields": "OBJECTID,NAME,AKA,MASTER_ASSOCIATION,MASTER,INACTIVE,GATED,AGE_RESTRICTED,NUM_UNITS,FILE_,SOURCE,DATE_UPDATED,MANAGEMENT_COMPANY,MANAGER_CITY",
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

ASSN_TYPE = {"REG": "registered association", "SUB": "sub-association",
             "MSTR": "master association", "SAM": "sub-association (master-managed)"}
LV_TYPE = {"HOA": "homeowners association", "MA": "master association",
           "LMA": "master association (large)", "MA/H*": "master association / HOA",
           "LMA/*": "master association (large)"}


def _s(v) -> str:
    return " ".join(str(v).split()) if v not in (None, "") else ""


def _units(v):
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _year(ms) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


def fetch_layer(session: requests.Session, spec: dict, pace: float) -> list[dict]:
    base = spec["url"] + "/query"
    out: list[dict] = []
    offset = 0
    while True:
        r = session.get(base, params={
            "where": spec["where"], "outFields": spec["fields"], "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 1000,
            "f": "json"}, timeout=120)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"arcgis error: {d['error']}")
        feats = [f["attributes"] for f in d.get("features", [])]
        out.extend(feats)
        offset += len(feats)
        if len(feats) < 1000 and not d.get("exceededTransferLimit"):
            break
        time.sleep(pace)
    return out


def clark_record(a: dict, spec: dict) -> dict | None:
    name = _s(a.get("Name")).upper()
    if not name:
        return None
    addr1, addr2 = _s(a.get("Address1")), _s(a.get("Address2"))
    manager = ""
    if addr1.upper().startswith("C/O"):
        manager = addr1[3:].strip(" :").title()
        addr1 = ""
    return {
        "state": "NV", "source": spec["source"], "source_url": spec["url"],
        "record_id": _s(a.get("SOS_")) or f"clark-{a.get('OBJECTID')}",
        "sos_file_number": _s(a.get("SOS_")),
        "name": name,
        "status": ASSN_TYPE.get(_s(a.get("Assn_Type")), _s(a.get("Assn_Type"))),
        "status_detail": _s(a.get("NOTES")).title() if _s(a.get("NOTES")) else "",
        "recorded_date": "",
        "address": ", ".join(x for x in (addr1, addr2) if x),
        "city": _s(a.get("City")).title(), "county": "Clark",
        "zip": _s(a.get("Zip_Code"))[:5],
        "units": _units(a.get("F__of_Units")),
        "manager_name": manager,
        "gated": _s(a.get("GATED")).upper() == "Y" or None,
    }


def henderson_record(a: dict, spec: dict, master: bool) -> dict | None:
    name = _s(a.get("NAME")).upper()
    if not name:
        return None
    inactive = _s(a.get("INACTIVE")).upper() == "Y"
    parent = _s(a.get("MASTER_ASSOCIATION"))
    if master or parent.upper() == "MASTER ASSOCIATION":
        parent = ""
    return {
        "state": "NV", "source": spec["source"], "source_url": spec["url"],
        "record_id": _s(a.get("FILE_")) or f"henderson-{'m' if master else 'h'}-{a.get('OBJECTID')}",
        "sos_file_number": _s(a.get("FILE_")),
        "name": name,
        "dba": _s(a.get("AKA")),
        "status": ("inactive" if inactive else "active") + (" master association" if master else " association"),
        "status_detail": f"sub-association of {parent.title()}" if parent else "",
        "recorded_date": _year(a.get("DATE_UPDATED")),
        "address": "", "city": "Henderson", "county": "Clark", "zip": "",
        "units": _units(a.get("NUM_UNITS")),
        "manager_name": _s(a.get("MANAGEMENT_COMPANY")).title(),
        "gated": _s(a.get("GATED")).upper() == "Y" or None,
    }


def las_vegas_record(a: dict, spec: dict) -> dict | None:
    name = _s(a.get("ASSOC_NAME")).upper()
    if not name:
        return None
    ntype = _s(a.get("NTYPE"))
    return {
        "state": "NV", "source": spec["source"], "source_url": spec["url"],
        "record_id": _s(a.get("CORP_NO")) or f"lasvegas-{a.get('ASSOC_ID') or a.get('OBJECTID')}",
        "sos_file_number": "",
        "corp_number": _s(a.get("CORP_NO")),
        "name": name,
        "status": ("registered " if _s(a.get("REGISTERED")).upper() == "Y" else "")
                  + LV_TYPE.get(ntype, ntype),
        "status_detail": f"city record updated {_s(a.get('UPDATED'))}" if _s(a.get("UPDATED")) else "",
        "recorded_date": "",
        "address": "", "city": _s(a.get("CITY1")).title() or "Las Vegas", "county": "Clark",
        "zip": _s(a.get("ZIP1"))[:5],
        "units": None, "manager_name": "",
        "gated": _s(a.get("GATED")).upper() == "Y" or None,
    }


def _norm(name: str) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    n = re.sub(r"\b(INC|THE|A|AN|OF|LLC|ASSN|ASSOC|ASSOCIATION|HOMEOWNERS|OWNERS|HOA|COMMUNITY)\b", " ", n)
    return " ".join(n.split())


def merge(rows: list[dict]) -> list[dict]:
    """One row per association: same SoS file number, else same normalized
    name. The richer row (units, manager, address) wins; the other's source
    is kept in `also_listed_by`."""
    by_sos: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    out: list[dict] = []
    for r in rows:
        key_sos = r.get("sos_file_number") or ""
        key_name = _norm(r["name"])
        hit = by_sos.get(key_sos) if key_sos else None
        if hit is None and key_name:
            hit = by_name.get(key_name)
        if hit is None:
            out.append(r)
            if key_sos:
                by_sos[key_sos] = r
            if key_name:
                by_name.setdefault(key_name, r)
            continue
        for k in ("units", "manager_name", "address", "zip", "dba", "sos_file_number", "corp_number"):
            if not hit.get(k) and r.get(k):
                hit[k] = r[k]
        hit.setdefault("also_listed_by", [])
        if r["source"] not in hit["also_listed_by"] and r["source"] != hit["source"]:
            hit["also_listed_by"].append(r["source"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pace", type=float, default=0.5)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    rows: list[dict] = []
    per_layer = {}
    for key, spec in LAYERS.items():
        feats = fetch_layer(session, spec, args.pace)
        conv = {"clark": lambda a: clark_record(a, spec),
                "henderson": lambda a: henderson_record(a, spec, False),
                "henderson_master": lambda a: henderson_record(a, spec, True),
                "las_vegas": lambda a: las_vegas_record(a, spec)}[key]
        recs = [r for r in (conv(a) for a in feats) if r]
        per_layer[key] = len(recs)
        rows.extend(recs)
        log.info("%s: %d features -> %d associations", key, len(feats), len(recs))
        time.sleep(args.pace)

    merged = merge(rows)
    total = write_merged(REGS, merged, {"NV"})
    update_coverage("NV", "hoa_registry", {
        "source": "Clark County / Henderson / Las Vegas GIS HOA layers (NRED-registered associations)",
        "records": len(merged), "url": LAYERS["clark"]["url"],
        "coverage": "partial",
        "note": ("Clark County only (" + ", ".join(f"{k} {n}" for k, n in per_layer.items())
                 + f"; {len(rows)} rows merged to {len(merged)}). NRED registers ~3,700 statewide "
                 "but publishes no roster; Washoe/Carson/Douglas (~700) have no public layer"),
    })
    log.info("state_registries.jsonl: %d rows total, %d NV", total, len(merged))
    print(f"  NV: {len(merged):,} registry records ({len(rows)} layer rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
