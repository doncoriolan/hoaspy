"""Business-search JSON API used by several states' Secretaries of State
(Idaho SOSBiz, North Dakota FirstStop — same vendor platform).

The public search endpoint returns JSON with no auth, but caps every query at
500 rows and has no pagination, so coverage comes from a union of overlapping
keyword queries. That makes this a PARTIAL extract: fine for attaching a
corporation (and its standing) to a known association, never evidence that an
association has no corporation.
"""

from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger("records.tecuity")

# Broad association keywords, then common name-word slicers to get under the
# per-query cap in the "HOMEOWNERS" family, which is the biggest.
KEYWORDS = [
    "HOMEOWNERS ASSOCIATION", "CONDOMINIUM", "OWNERS ASSOCIATION",
    "PROPERTY OWNERS", "COMMUNITY ASSOCIATION", "TOWNHOME", "TOWNHOUSE",
]
SLICERS = [
    "RANCH HOMEOWNERS", "ESTATES HOMEOWNERS", "PARK HOMEOWNERS",
    "MEADOW HOMEOWNERS", "CREEK HOMEOWNERS", "RIDGE HOMEOWNERS",
    "VILLAGE HOMEOWNERS", "LAKE HOMEOWNERS", "HILLS HOMEOWNERS",
    "VALLEY HOMEOWNERS", "VIEW HOMEOWNERS", "HEIGHTS HOMEOWNERS",
    "SPRINGS HOMEOWNERS", "POINTE HOMEOWNERS", "GLEN HOMEOWNERS",
    "WOOD HOMEOWNERS", "TRAIL HOMEOWNERS", "SUB HOMEOWNERS",
]

_ID_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def fetch(session: requests.Session, cfg: dict, state: str) -> list[dict]:
    url = cfg["url"]
    seen: dict[str, dict] = {}
    capped = 0
    for kw in KEYWORDS + SLICERS:
        body = {"SEARCH_VALUE": kw, "STARTS_WITH": False, "ACTIVE_ONLY": False}
        try:
            resp = session.post(url, json=body, timeout=60)
            resp.raise_for_status()
            rows = resp.json().get("rows") or {}
        except Exception as exc:
            log.warning("%s %r failed: %s", state, kw, exc)
            continue
        if len(rows) >= 500:
            capped += 1
        for rid, r in rows.items():
            if rid in seen:
                continue
            title = r.get("TITLE") or [""]
            name = _ID_SUFFIX.sub("", title[0]).strip()
            seen[rid] = {
                "state": state,
                "source": cfg.get("source", url),
                "source_url": cfg.get("page", url),
                "record_id": r.get("RECORD_NUM", rid),
                "name": name,
                "corp_status": r.get("STATUS", ""),
                "address": "", "city": "", "county": "", "zip": "",
                "incorporated": _iso(r.get("FILING_DATE", "")),
                "registered_agent": (r.get("AGENT") or "").strip(),
                "entity_type": title[1] if len(title) > 1 else "",
            }
        log.info("%s %-28r -> %d rows (%d distinct so far)",
                 state, kw, len(rows), len(seen))
        time.sleep(cfg.get("delay", 2.0))
    if capped:
        log.warning("%s: %d queries hit the 500-row cap — extract is partial", state, capped)
    return list(seen.values())


def _iso(mdY: str) -> str:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", mdY or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""
