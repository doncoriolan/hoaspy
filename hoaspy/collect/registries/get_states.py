"""State HOA registries and corporate rosters, for every state configured
in state_sources.yml.

    python -m hoaspy states                 # everything configured
    python -m hoaspy states --state CO NV   # some states

Source kinds (one list per state in state_sources.yml):

    socrata_corp      corporate registry on a Socrata portal        -> state_corps.jsonl
    socrata_registry  HOA registry on a Socrata portal              -> state_registries.jsonl
    csv_corp          direct CSV/TSV/XLSX download                  -> state_corps.jsonl
    csv_registry      direct CSV/TSV/XLSX download                  -> state_registries.jsonl
                      (`constants:` fills columns the file lacks, e.g. county)
    arcgis_corp       corporate registry on an ArcGIS FeatureServer -> state_corps.jsonl
    tecuity_corp      Tecuity business-search API (ID, ND)          -> state_corps.jsonl
                      (a search sweep, so its coverage is recorded as partial)

Corporate rows are kept only when the name reads as an association
(server-side for Socrata, locally for files); registry rows are kept whole,
the registry itself being the filter. A run replaces the rows of the states
it fetched and leaves the other states' rows in place, and records each
state's result in coverage.json. records/corp_coverage.json says per state
whether its corporate roster is complete ("all") or a partial sweep — what
decides whether a corporation missing from it means anything.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import requests
import yaml

from hoaspy import ROOT
from hoaspy.collect.registries import CORPORATIONS_PATH, RECORDS_DIR, REGISTRIES_PATH, records_socrata, records_tecuity
from hoaspy.lib import http
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import now_iso, replace_rows, write_json
from hoaspy.lib.names import REGISTRY_NAME_RE
from hoaspy.lib.records import Association, Officer

SOURCES_PATH = ROOT / "state_sources.yml"
CORP_COVERAGE_PATH = RECORDS_DIR / "corp_coverage.json"
ARCGIS_PAGE_SIZE = 2000

log = logging.getLogger(__name__)


# ---- reading a source's columns ------------------------------------------

def column_text(row: dict, spec) -> str:
    """The text of a mapped column. `spec` is a column name, a list of
    columns joined with spaces, or nothing."""
    if not spec:
        return ""
    if isinstance(spec, list):
        return " ".join(str(row.get(column) or "").strip() for column in spec).strip()
    return str(row.get(spec) or "").strip()


def unit_count(text: str) -> int | None:
    """A unit count as a number; None when the column is empty or not one."""
    digits = text.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def iso_date(text: str) -> str:
    """'3/3/2004', '7/20/1987 12:00:00 AM' or ISO-ish -> YYYY-MM-DD."""
    head = text.split()[0] if text and text.split() else ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            pass
    return head[:10]


def epoch_day(milliseconds) -> str:
    """An ArcGIS date (milliseconds since the epoch) as YYYY-MM-DD."""
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(milliseconds or "")[:10]


def file_rows(content: bytes, cfg: dict):
    """The rows of a downloaded file: the first sheet of an XLSX, else
    delimited text in the configured encoding and delimiter."""
    if cfg.get("xlsx"):
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        header = [str(cell or "").strip() for cell in next(rows)]
        for row in rows:
            yield {name: ("" if value is None else str(value)) for name, value in zip(header, row, strict=False)}
        return
    text = content.decode(cfg.get("encoding", "utf-8"), "replace")
    yield from csv.DictReader(io.StringIO(text), delimiter=cfg.get("delimiter", ","))


def load_officials(cfg: dict | None) -> dict[str, list[Officer]]:
    """Officers from a companion CSV keyed by the parent record id (Alaska
    DCCED's OfficialsDownload.csv): {record_id: [Officer, …]}. A missing
    file is not an error — the officers are simply left out."""
    if not cfg:
        return {}
    path = Path(cfg["local_cache"])
    if not path.exists():
        log.warning("officials file %s missing — no officers added", path)
        return {}
    out: dict[str, list[Officer]] = {}
    seen: set[tuple[str, str, str]] = set()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            record_id = (row.get(cfg["key"]) or "").strip()
            last = " ".join((row.get(cfg["last"]) or "").split())
            first = " ".join((row.get(cfg.get("first", "")) or "").split())
            title = " ".join((row.get(cfg.get("title", "")) or "").split())
            name = f"{first} {last}".strip()
            if not record_id or not name or (record_id, name.upper(), title) in seen:
                continue
            seen.add((record_id, name.upper(), title))
            out.setdefault(record_id, []).append(Officer(name=name, title=title))
    log.info("officials: %d officer rows for %d records from %s",
             sum(len(officers) for officers in out.values()), len(out), path)
    return out


# ---- the fetchers ----------------------------------------------------------

def fetch_csv(session: requests.Session | None, cfg: dict, state: str, registry: bool,
              retrieved_at: str = "") -> list[Association]:
    """Rows of a downloaded file (or local copies of it). Corporate rows
    (`registry=False`) are kept only when the name reads as an association."""
    if cfg.get("local_glob"):
        paths = sorted(ROOT.glob(cfg["local_glob"]))
        if not paths:
            raise FileNotFoundError(f"no files match {cfg['local_glob']}")
        contents = [path.read_bytes() for path in paths]
    else:
        local = cfg.get("local_cache")
        if local and Path(local).exists():
            contents = [Path(local).read_bytes()]
        else:
            response = session.get(cfg["url"], timeout=600)
            response.raise_for_status()
            if local:
                Path(local).write_bytes(response.content)
            contents = [response.content]
    if cfg.get("zip"):
        archive = zipfile.ZipFile(io.BytesIO(contents[0]))
        member = next(name for name in archive.namelist() if name.lower().endswith((".csv", ".txt")))
        contents = [archive.read(member)]

    fields = cfg["fields"]
    exclude_status = re.compile(cfg["exclude_status"], re.I) if cfg.get("exclude_status") else None
    officials = load_officials(cfg.get("officials"))
    out: list[Association] = []
    seen_ids: set[str] = set()
    for content in contents:
        for row in file_rows(content, cfg):
            for key, value in (cfg.get("constants") or {}).items():
                row.setdefault(key, value)                      # e.g. the county of a single-county list
            name = " ".join(column_text(row, fields.get("name")).split())
            if not name or (not registry and not REGISTRY_NAME_RE.search(name)):
                continue
            status = column_text(row, fields.get("status"))
            if exclude_status and exclude_status.search(status):
                continue
            record_id = column_text(row, fields.get("record_id"))
            if cfg.get("dedupe") and record_id:
                if record_id in seen_ids:
                    continue
                seen_ids.add(record_id)
            out.append(Association(
                state=state, source=cfg.get("source", cfg.get("url", "")),
                source_url=cfg.get("page", cfg.get("url", "")), record_id=record_id, name=name,
                corp_status=status, status=status,
                address=column_text(row, fields.get("address")),
                city=column_text(row, fields.get("city")).title(),
                county=column_text(row, fields.get("county")).title(),
                zip=column_text(row, fields.get("zip"))[:5],
                incorporated=iso_date(column_text(row, fields.get("incorporated"))),
                registered_agent=" ".join(column_text(row, fields.get("agent")).split()),
                entity_type=column_text(row, fields.get("type")),
                units=unit_count(column_text(row, fields.get("units"))),
                manager_name=column_text(row, fields.get("manager")),
                officers=officials.get(record_id, []), retrieved_at=retrieved_at))
    return out


def fetch_arcgis(session: requests.Session, cfg: dict, state: str, retrieved_at: str = "") -> list[Association]:
    """A corporate registry on an ArcGIS FeatureServer (the District of
    Columbia): the association words as a server-side filter, paged by
    result offset."""
    fields = cfg["fields"]
    patterns = ["HOMEOWNER", "CONDOMINIUM", "PROPERTY OWNERS", "OWNERS ASSOCIATION",
                "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION", "TOWNHOME", "TOWNHOUSE"]
    where = " OR ".join(f"UPPER({fields['name']}) LIKE '%{pattern}%'" for pattern in patterns)
    out: list[Association] = []
    offset = 0
    while True:
        response = session.get(cfg["url"], params={
            "where": where, "outFields": "*", "f": "json",
            "resultOffset": offset, "resultRecordCount": ARCGIS_PAGE_SIZE,
            "orderByFields": fields.get("record_id", "OBJECTID")}, timeout=120)
        response.raise_for_status()
        page = response.json()
        if "error" in page:
            raise RuntimeError(f"arcgis error: {page['error']}")
        features = page.get("features", [])
        for feature in features:
            attributes = feature["attributes"]
            text = {key: str(attributes.get(fields.get(key)) or "")
                    for key in ("record_id", "name", "status", "address", "city", "zip", "agent", "type")}
            out.append(Association(
                state=state, source=cfg.get("source", cfg["url"]), source_url=cfg.get("page", cfg["url"]),
                record_id=text["record_id"], name=" ".join(text["name"].split()),
                corp_status=text["status"], address=text["address"], city=text["city"].title(),
                zip=text["zip"][:5], incorporated=epoch_day(attributes.get(fields.get("incorporated"))),
                registered_agent=" ".join(text["agent"].split()), entity_type=text["type"],
                retrieved_at=retrieved_at))
        offset += len(features)
        log.info("%s arcgis: %d rows so far", state, offset)
        if len(features) < ARCGIS_PAGE_SIZE and not page.get("exceededTransferLimit"):
            return out
        time.sleep(0.5)


def fetch_source(session: requests.Session, state: str, src: dict, retrieved_at: str) -> tuple[list[Association], str]:
    """The rows of one configured source and the file they belong to:
    "corp" (state_corps.jsonl) or "registry" (state_registries.jsonl)."""
    kind = src["kind"]
    if kind == "socrata_corp":
        rows = records_socrata.fetch(session, src, state, retrieved_at=retrieved_at)
        if src.get("dedupe"):
            seen: set[str] = set()
            distinct = []
            for row in rows:
                if row.record_id not in seen:
                    seen.add(row.record_id)
                    distinct.append(row)
            rows = distinct
        return rows, "corp"
    if kind == "socrata_registry":
        return records_socrata.fetch(session, src, state, filter_names=src.get("filter_names", False),
                                     retrieved_at=retrieved_at), "registry"
    if kind == "tecuity_corp":
        return records_tecuity.fetch(session, src, state, retrieved_at=retrieved_at), "corp"
    if kind == "arcgis_corp":
        return fetch_arcgis(session, src, state, retrieved_at=retrieved_at), "corp"
    if kind == "csv_corp":
        return fetch_csv(session, src, state, registry=False, retrieved_at=retrieved_at), "corp"
    if kind == "csv_registry":
        return fetch_csv(session, src, state, registry=True, retrieved_at=retrieved_at), "registry"
    raise ValueError(f"unknown source kind {kind!r}")


def coverage_info(src: dict, target: str, count: int) -> tuple[str, dict]:
    """(coverage key, entry) for one fetched source."""
    info = {"source": src.get("source", ""), "records": count, "url": src.get("page", "")}
    if target == "registry":
        return "hoa_registry", info
    info["coverage"] = "partial" if src["kind"] == "tecuity_corp" else src.get("coverage", "all")
    return "corporate", info


def write_corp_coverage(config: dict) -> None:
    """records/corp_coverage.json: per state, whether the corporate roster
    is complete ("all") or a partial sweep. The site build flags a missing
    corporation only where the roster is complete."""
    coverage = {}
    for state, sources in config.items():
        for src in sources or []:
            if src["kind"].endswith("_corp"):
                coverage[state] = "partial" if src["kind"] == "tecuity_corp" else src.get("coverage", "all")
    write_json(CORP_COVERAGE_PATH, coverage)


# ---- the command -----------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", nargs="+", help="limit to these state codes")


def run(args: argparse.Namespace) -> int:
    if not SOURCES_PATH.exists():
        log.error("%s not found — nothing configured yet", SOURCES_PATH)
        return 1
    config = yaml.safe_load(SOURCES_PATH.read_text()) or {}
    session = http.session()
    retrieved_at = now_iso()
    fresh: dict[str, list[Association]] = {"corp": [], "registry": []}
    states_done: dict[str, set[str]] = {"corp": set(), "registry": set()}

    for state, sources in sorted(config.items()):
        if args.state and state not in args.state:
            continue
        for src in sources or []:
            label = f"{state} {src['kind']} ({src.get('source', src.get('dataset', src.get('url', '?')))})"
            started = time.time()
            try:
                rows, target = fetch_source(session, state, src, retrieved_at)
            except Exception as exc:
                log.error("%s FAILED: %s: %s", label, type(exc).__name__, exc)
                continue
            fresh[target].extend(rows)
            states_done[target].add(state)
            update_coverage(state, *coverage_info(src, target, len(rows)))
            log.info("%s -> %d rows in %.0fs", label, len(rows), time.time() - started)

    write_corp_coverage(config)
    for target, path in (("corp", CORPORATIONS_PATH), ("registry", REGISTRIES_PATH)):
        if states_done[target]:
            total = replace_rows(path, (row.as_row() for row in fresh[target]), states_done[target])
            log.info("%s: %d rows total (%d fresh from %s)", path.name, total, len(fresh[target]),
                     ", ".join(sorted(states_done[target])))
    for target, what in (("corp", "association-named corporations"), ("registry", "registry records")):
        for state, count in sorted(Counter(row.state for row in fresh[target]).items()):
            print(f"  {state}: {count:,} {what}")
    return 0
