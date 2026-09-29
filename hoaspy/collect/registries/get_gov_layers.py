#!/usr/bin/env python
"""Local-government HOA/condo inventories (county/city ArcGIS layers, Socrata
datasets, CSV/XLSX downloads) for states with no statewide registry.

    ./venv/bin/python -m hoaspy.collect.registries.get_gov_layers                 # every layer in gov_layers.yml
    ./venv/bin/python -m hoaspy.collect.registries.get_gov_layers --state MA MI   # some states
    ./venv/bin/python -m hoaspy.collect.registries.get_gov_layers --list

One YAML per state in `gov_layers/<ST>.yml` (a list of layers) maps each
layer to the registry row shape:

    - kind: arcgis                      # arcgis | socrata | csv
      source: "MassGIS statewide parcels — condominium trusts (owner name)"
      page: "https://..."               # human page (source_url)
      url: "https://.../FeatureServer/0"  # layer root (arcgis) / resource (socrata) / file (csv)
      dbf: ownership/ownership.dbf        # csv kind: .zip of a shapefile -> rows of this .dbf (default first)
      where: "OWNER1 LIKE '%CONDOMINIUM TRUST%'"   # arcgis/socrata server-side filter
      # (arcgis: a list of filters is fetched one after another — for services
      #  whose per-query timeout forces partitioning, e.g. by OBJECTID range)
      fields: {name: OWNER1, address: SITE_ADDR, city: CITY, zip: ZIP, units: UNITS,
               record_id: LOC_ID, status: TYPE, manager: MGMT, county: COUNTY}
      constants: {county: New Castle, status: "maintenance corporation"}
      name_filter: true                 # keep only association-looking names (default false)
      name_regex: "^(.*?) CONDOMINIUM"  # optional: keep/extract group 1 of a match
      exclude_name: "BANK|MORTGAGE"     # optional: drop matching names
      group_by_name: true               # collapse parcel rows to one per name (default true)

Names are upper-cased; rows sharing a normalized name within one state are
merged (first row wins, `parcels` counts the duplicates). Phone/e-mail
columns are never mapped. Output: records/gov_layers.jsonl (registry shape,
`source` = the layer label) — build_site.py folds it like a registry but
marks the state `local_inventory` (county/city inventory, not a statewide
registry); coverage.json collected["local_inventory"] per state.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

import requests
import yaml

from hoaspy.collect.registries.get_states import ASSOC_RE, update_coverage, write_merged

from hoaspy import ROOT
CONFIG_DIR = ROOT / "gov_layers"
OUT = ROOT / "records" / "gov_layers.jsonl"
CACHE = ROOT / ".cache" / "gov_layers"
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
FORBIDDEN_FIELDS = re.compile(r"phone|tel\b|email|e_mail|mail_addr|cell|fax", re.I)

log = logging.getLogger("gov")


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())


def _units(v):
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _norm(name: str) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    n = re.sub(r"\b(INC|THE|A|AN|OF|LLC|CORP|CORPORATION|TRUST|TRUSTEES?|ASSN|ASSOC|ASSOCIATION|"
               r"HOMEOWNERS|OWNERS|HOA|COMMUNITY|CONDOMINIUM|CONDO)\b", " ", n)
    return " ".join(n.split())


# ---- fetchers -------------------------------------------------------------

def fetch_arcgis(session: requests.Session, cfg: dict, pace: float) -> list[dict]:
    base = cfg["url"].rstrip("/") + "/query"
    wheres = cfg.get("where") or "1=1"
    if isinstance(wheres, str):
        wheres = [wheres]
    out: list[dict] = []
    size = int(cfg.get("page_size") or 1000)
    oid = cfg.get("oid") or "OBJECTID"
    for where in wheres:
        offset = 0
        while True:
            params = {"where": where, "outFields": cfg.get("out_fields", "*"), "returnGeometry": "false",
                      "resultOffset": offset, "resultRecordCount": size, "f": "json"}
            if cfg.get("order", True):
                params["orderByFields"] = oid
            r = session.get(base, params=params, timeout=180)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(f"arcgis error: {d['error']}")
            feats = [f.get("attributes", f) for f in d.get("features", [])]
            out.extend(feats)
            offset += len(feats)
            if not feats or (len(feats) < size and not d.get("exceededTransferLimit")):
                break
            time.sleep(pace)
        if len(wheres) > 1:
            log.debug("%s: %d rows after filter %r", cfg["source"][:40], len(out), where[:60])
            time.sleep(pace)
    return out


def fetch_socrata(session: requests.Session, cfg: dict, pace: float) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        params = {"$limit": 5000, "$offset": offset}
        if cfg.get("where"):
            params["$where"] = cfg["where"]
        r = session.get(cfg["url"], params=params, timeout=180)
        r.raise_for_status()
        rows = r.json()
        out.extend(rows)
        offset += len(rows)
        if len(rows) < 5000:
            break
        time.sleep(pace)
    return out


def fetch_csv(session: requests.Session, cfg: dict, pace: float) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    import hashlib
    local = CACHE / (hashlib.sha1(cfg["url"].encode()).hexdigest()[:10] + "_"
                     + re.sub(r"[^A-Za-z0-9._-]", "_", cfg["url"].split("/")[-1] or "file"))
    if not local.exists():
        r = session.get(cfg["url"], timeout=300)
        r.raise_for_status()
        local.write_bytes(r.content)
    content = local.read_bytes()
    if content[:2] == b"PK" and (cfg.get("dbf") or local.suffix.lower() == ".zip"):
        return _read_zip_dbf(content, cfg.get("dbf"))
    if cfg.get("xlsx") or local.suffix.lower() in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        ws = wb[cfg["sheet"]] if cfg.get("sheet") else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        for _ in range(int(cfg.get("skip_rows") or 0)):
            next(it)
        header = [_s(h) for h in next(it)]
        return [{h: v for h, v in zip(header, row)} for row in it]
    text = content.decode(cfg.get("encoding", "utf-8-sig"), "replace")
    return list(csv.DictReader(io.StringIO(text), delimiter=cfg.get("delimiter", ",")))


def _read_zip_dbf(content: bytes, member: str | None) -> list[dict]:
    """Shapefile/DBF zip (e.g. county assessor downloads): rows of the first (or
    named) .dbf member, character/numeric fields as strings."""
    import struct
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(content))
    name = member or next(n for n in zf.namelist() if n.lower().endswith(".dbf"))
    b = zf.read(name)
    n_rec, hdr_len, rec_len = struct.unpack("<xxxxIHH", b[:12])
    fields, p = [], 32
    while b[p] != 0x0D:
        fields.append((b[p:p + 11].split(b"\0")[0].decode("ascii"), b[p + 16]))
        p += 32
    out = []
    for i in range(n_rec):
        rec = b[hdr_len + i * rec_len: hdr_len + (i + 1) * rec_len]
        if rec[:1] == b"*":
            continue
        row, o = {}, 1
        for fname, ln in fields:
            row[fname] = rec[o:o + ln].decode("latin-1").strip()
            o += ln
        out.append(row)
    return out


FETCH = {"arcgis": fetch_arcgis, "socrata": fetch_socrata, "csv": fetch_csv}


# ---- rows -----------------------------------------------------------------

def _get(row: dict, spec) -> str:
    if not spec:
        return ""
    if isinstance(spec, list):
        return " ".join(_s(row.get(c)) for c in spec).strip()
    return _s(row.get(spec))


def to_rows(state: str, cfg: dict, raw: list[dict]) -> list[dict]:
    fields = cfg.get("fields") or {}
    for k, col in fields.items():
        cols = col if isinstance(col, list) else [col]
        for c in cols:
            if FORBIDDEN_FIELDS.search(str(c)):
                raise ValueError(f"{state} {cfg['source']}: refusing to map contact column {c!r}")
    consts = cfg.get("constants") or {}
    name_filter = bool(cfg.get("name_filter"))
    group = cfg.get("group_by_name", True)
    exclude = re.compile(cfg["exclude_name"], re.I) if cfg.get("exclude_name") else None
    merged: "OrderedDict[str, dict]" = OrderedDict()
    n_in = 0
    for a in raw:
        name = _get(a, fields.get("name")).upper()
        if cfg.get("name_regex"):
            m = re.search(cfg["name_regex"], name)
            if not m:
                continue
            name = (m.group(1) if m.groups() else m.group(0)).strip()
        if not name or len(name) < 3:
            continue
        if name_filter and not ASSOC_RE.search(name):
            continue
        if exclude and exclude.search(name):
            continue
        n_in += 1
        key = _norm(name) if group else f"{_norm(name)}|{n_in}"
        if key in merged:
            merged[key]["parcels"] = merged[key].get("parcels", 1) + 1
            for k in ("address", "city", "zip", "units", "manager_name", "record_id"):
                if not merged[key].get(k):
                    v = _row_field(a, fields, consts, k)
                    merged[key][k] = _units(v) if k == "units" else v
            continue
        merged[key] = {
            "state": state,
            "source": cfg["source"],
            "source_url": cfg.get("page") or cfg["url"],
            "record_id": _row_field(a, fields, consts, "record_id"),
            "name": name,
            "status": _row_field(a, fields, consts, "status"),
            "status_detail": _row_field(a, fields, consts, "status_detail"),
            "recorded_date": _row_field(a, fields, consts, "recorded_date")[:10],
            "address": _row_field(a, fields, consts, "address"),
            "city": clean_city(_row_field(a, fields, consts, "city")).title(),
            "county": _row_field(a, fields, consts, "county").title().replace(" County", ""),
            "zip": _row_field(a, fields, consts, "zip")[:5],
            "units": _units(_row_field(a, fields, consts, "units")),
            "manager_name": _row_field(a, fields, consts, "manager_name").title(),
            "layer_kind": cfg.get("kind"),
        }
    rows = list(merged.values())
    log.info("%s %s: %d raw -> %d kept -> %d associations", state, cfg["source"][:60],
             len(raw), n_in, len(rows))
    return rows


_ADDRESS_LINE = re.compile(r"\d|\bP\.? ?O\.? BOX\b|\bSTE\b|\bSUITE\b|\bUNIT\b|\bAPT\b|\bFLOOR\b|\bBLDG\b", re.I)


def clean_city(city: str) -> str:
    """A city field that carries a street line or PO box (the NC OneMap
    mailing-city column does for ~2k parcels) is dropped rather than saved."""
    c = " ".join((city or "").split())
    return "" if _ADDRESS_LINE.search(c) else c


def _row_field(a: dict, fields: dict, consts: dict, key: str) -> str:
    col = fields.get({"manager_name": "manager"}.get(key, key)) or fields.get(key)
    v = _get(a, col) if col else ""
    if not v and key in consts:
        v = _s(consts[key])
    if key == "units":
        return v
    return v


# ---- driver ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", nargs="+", help="limit to these state codes")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pace", type=float, default=0.5)
    ap.add_argument("--no-upload", action="store_true", help="(always; kept for parity)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    config: dict[str, list] = {}
    for f in sorted(CONFIG_DIR.glob("*.yml")):
        config[f.stem.upper()] = yaml.safe_load(f.read_text()) or []
    if args.list:
        for st, layers in sorted(config.items()):
            for l in layers or []:
                print(f"{st}  {l['kind']:8} {l['source']}")
        return 0

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    fresh: list[dict] = []
    states_done: set[str] = set()
    failures: list[str] = []
    per_state: dict[str, list[dict]] = {}
    for state, layers in sorted(config.items()):
        if args.state and state not in {s.upper() for s in args.state}:
            continue
        state_rows: list[dict] = []
        for cfg in layers or []:
            label = f"{state} {cfg['kind']} {cfg['source'][:50]}"
            try:
                raw = FETCH[cfg["kind"]](session, cfg, args.pace)
                rows = to_rows(state, cfg, raw)
            except Exception as exc:
                log.error("%s FAILED: %s: %s", label, type(exc).__name__, exc)
                failures.append(label)
                continue
            per_state.setdefault(state, []).append({"source": cfg["source"], "records": len(rows),
                                                    "url": cfg.get("page") or cfg["url"]})
            state_rows.extend(rows)
            time.sleep(args.pace)
        if state_rows:
            # cross-layer merge within the state on normalized name
            seen: dict[str, dict] = {}
            for r in state_rows:
                # Same name in two different cities is two associations
                # (Tulsa's and OKC's "Country Oaks II HOA"); merge only when
                # a city is missing on one side or both agree.
                k = _norm(r["name"])
                city = (r.get("city") or "").upper()
                if k in seen and city and (seen[k].get("city") or "").upper() not in ("", city):
                    k = f"{k}|{city}"
                if k in seen:
                    seen[k].setdefault("also_listed_by", [])
                    if r["source"] != seen[k]["source"] and r["source"] not in seen[k]["also_listed_by"]:
                        seen[k]["also_listed_by"].append(r["source"])
                    for f in ("address", "city", "zip", "units", "manager_name", "county"):
                        if not seen[k].get(f) and r.get(f):
                            seen[k][f] = r[f]
                    continue
                seen[k] = r
            state_rows = list(seen.values())
            fresh.extend(state_rows)
            states_done.add(state)
            update_coverage(state, "local_inventory", {
                "source": "; ".join(f"{l['source']} ({l['records']})" for l in per_state[state]),
                "records": len(state_rows),
                "url": per_state[state][0]["url"],
                "coverage": "partial",
                "note": "county/city inventories, not a statewide registry — absence means nothing",
            })

    if states_done:
        total = write_merged(OUT, fresh, states_done)
        log.info("%s: %d rows total (%d fresh from %s)", OUT.name, total, len(fresh),
                 ", ".join(sorted(states_done)))
    for st, n in sorted(Counter(r["state"] for r in fresh).items()):
        print(f"  {st}: {n:,} associations from local-government layers")
    if failures:
        print("FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
