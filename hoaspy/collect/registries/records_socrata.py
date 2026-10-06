"""Socrata datasets: the state corporate registries and HOA registries that
are published as one.

The per-state specifics — domain, dataset id, which column holds what —
live in state_sources.yml; this module pages through any such dataset and
shapes its rows. For a corporate registry of millions of rows the
association words go into the SoQL `$where`, so only association-looking
rows leave the server.
"""

from __future__ import annotations

import logging

import requests

from hoaspy.lib.records import Association

log = logging.getLogger(__name__)

PAGE_SIZE = 5000

# Name patterns that make a corporate row worth keeping, applied server-side.
NAME_PATTERNS = [
    "HOMEOWNER", "HOME OWNER", "CONDOMINIUM", "CONDO ", " CONDO",
    "PROPERTY OWNERS", "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION",
    "OWNERS ASSOCIATION", "RESIDENTS ASSOCIATION", "TOWNHOME", "TOWNHOUSE",
]


def column_text(row: dict, spec) -> str:
    """The text of a mapped column: a column name, or a list of columns
    joined with spaces. Socrata's url and location types are dicts; their
    address or url is the text."""
    if not spec:
        return ""
    if isinstance(spec, list):
        return " ".join(str(row.get(column) or "").strip() for column in spec).strip()
    value = row.get(spec)
    if isinstance(value, dict):
        value = value.get("url") or value.get("human_address") or ""
    return str(value or "").strip()


def name_where(name_column: str) -> str:
    """The SoQL filter that keeps association-looking names."""
    return " OR ".join(f"upper({name_column}) like '%{pattern}%'" for pattern in NAME_PATTERNS)


def fetch(session: requests.Session, cfg: dict, state: str, timeout: float = 90.0,
          filter_names: bool = True, retrieved_at: str = "") -> list[Association]:
    """Every row of one configured dataset, shaped. cfg keys: domain,
    dataset, fields {name, record_id, status, address, city, zip, county,
    incorporated, agent, type}, optional `where` (extra SoQL) and
    `app_token`."""
    fields = cfg["fields"]
    url = f"https://{cfg['domain']}/resource/{cfg['dataset']}.json"
    where = []
    if filter_names and fields.get("name"):
        where.append(f"({name_where(fields['name'])})")
    if cfg.get("where"):
        where.append(f"({cfg['where']})")
    headers = {"X-App-Token": cfg["app_token"]} if cfg.get("app_token") else {}

    out: list[Association] = []
    offset = 0
    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        if where:
            params["$where"] = " AND ".join(where)
        response = session.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        for row in rows:
            out.append(Association(
                state=state, source=cfg.get("source", f"socrata:{cfg['domain']}/{cfg['dataset']}"),
                source_url=cfg.get("page", url), record_id=column_text(row, fields.get("record_id")),
                name=" ".join(column_text(row, fields.get("name")).split()),
                corp_status=column_text(row, fields.get("status")),
                address=column_text(row, fields.get("address")),
                city=column_text(row, fields.get("city")).title(),
                county=column_text(row, fields.get("county")).title(),
                zip=column_text(row, fields.get("zip"))[:5],
                incorporated=column_text(row, fields.get("incorporated"))[:10],
                registered_agent=" ".join(column_text(row, fields.get("agent")).split()),
                entity_type=column_text(row, fields.get("type")), retrieved_at=retrieved_at))
        offset += len(rows)
        log.info("%s %s: %d rows so far", state, cfg["dataset"], offset)
        if len(rows) < PAGE_SIZE:
            break
    return out
