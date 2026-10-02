"""Generic Socrata-dataset collector for state business registries and HOA
registries.

Many states publish their corporate registry (or an HOA-specific registry) as
a Socrata dataset. The per-state specifics — domain, dataset id, and which
column holds what — live in `state_sources.yml`; this module just pages
through any such dataset and normalises rows.

Server-side name filtering: for multi-million-row corporate registries we
push a SoQL `$where upper(<name>) like '%HOMEOWNER%' OR ...` so only
association-looking rows ever leave the server.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("records.socrata")

PAGE_SIZE = 5000

# Name patterns that make a corporate row worth keeping. Kept SQL-side.
NAME_PATTERNS = [
    "HOMEOWNER", "HOME OWNER", "CONDOMINIUM", "CONDO ", " CONDO",
    "PROPERTY OWNERS", "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION",
    "OWNERS ASSOCIATION", "RESIDENTS ASSOCIATION", "TOWNHOME", "TOWNHOUSE",
]


def _get(row: dict, spec) -> str:
    """Column spec: a column name, or a list of columns joined with spaces."""
    if not spec:
        return ""
    if isinstance(spec, list):
        return " ".join(str(row.get(c) or "").strip() for c in spec).strip()
    v = row.get(spec)
    if isinstance(v, dict):  # socrata url/location types
        v = v.get("url") or v.get("human_address") or ""
    return str(v or "").strip()


def name_where(name_field: str) -> str:
    clauses = [f"upper({name_field}) like '%{p}%'" for p in NAME_PATTERNS]
    return " OR ".join(clauses)


def fetch(session: requests.Session, cfg: dict, state: str,
          timeout: float = 90.0, filter_names: bool = True) -> list[dict]:
    """Page through one configured Socrata dataset and normalise rows.

    cfg keys: domain, dataset, fields {name, record_id, status, address, city,
    zip, county, incorporated, agent, type}, optional `where` (extra SoQL),
    optional `app_token`.
    """
    fields = cfg["fields"]
    url = f"https://{cfg['domain']}/resource/{cfg['dataset']}.json"
    where_parts = []
    if filter_names and fields.get("name"):
        where_parts.append(f"({name_where(fields['name'])})")
    if cfg.get("where"):
        where_parts.append(f"({cfg['where']})")

    headers = {}
    if cfg.get("app_token"):
        headers["X-App-Token"] = cfg["app_token"]

    out: list[dict] = []
    offset = 0
    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        if where_parts:
            params["$where"] = " AND ".join(where_parts)
        resp = session.get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            out.append({
                "state": state,
                "source": cfg.get("source", f"socrata:{cfg['domain']}/{cfg['dataset']}"),
                "source_url": cfg.get("page", url),
                "record_id": _get(row, fields.get("record_id")),
                "name": " ".join(_get(row, fields.get("name")).split()),
                "corp_status": _get(row, fields.get("status")),
                "address": _get(row, fields.get("address")),
                "city": _get(row, fields.get("city")).title(),
                "county": _get(row, fields.get("county")).title(),
                "zip": _get(row, fields.get("zip"))[:5],
                "incorporated": _get(row, fields.get("incorporated"))[:10],
                "registered_agent": " ".join(_get(row, fields.get("agent")).split()),
                "entity_type": _get(row, fields.get("type")),
            })
        offset += len(rows)
        log.info("%s %s: %d rows so far", state, cfg["dataset"], offset)
        if len(rows) < PAGE_SIZE:
            break
    return out
