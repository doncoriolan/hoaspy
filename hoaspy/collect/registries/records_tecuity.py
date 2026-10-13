"""The business-search JSON API several Secretaries of State run on the
Tecuity platform (Idaho SOSBiz, North Dakota FirstStop).

The endpoint needs no login but caps every query at 500 rows and offers no
paging, so the roster is the union of overlapping keyword queries. That
makes it a PARTIAL extract: good for attaching a corporation (and its
standing) to a known association, never evidence that an association has
no corporation.
"""

from __future__ import annotations

import logging
import re
import time

import requests

from hoaspy.lib.records import Association

log = logging.getLogger(__name__)

# Broad association keywords, then slicers that get the biggest family,
# "HOMEOWNERS", under the per-query cap.
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
ROW_CAP = 500

ID_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")        # "NAME (12345)": the id the title carries


def iso_date(mdy: str) -> str:
    """'03/21/2025' -> '2025-03-21'; '' when the text is not a date."""
    match = re.match(r"(\d{2})/(\d{2})/(\d{4})", mdy or "")
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else ""


def fetch(session: requests.Session, cfg: dict, state: str, retrieved_at: str = "") -> list[Association]:
    """Every distinct entity the keyword queries return, shaped."""
    url = cfg["url"]
    seen: dict[str, Association] = {}
    capped = 0
    for keyword in KEYWORDS + SLICERS:
        body = {"SEARCH_VALUE": keyword, "STARTS_WITH": False, "ACTIVE_ONLY": False}
        try:
            response = session.post(url, json=body, timeout=60)
            response.raise_for_status()
            rows = response.json().get("rows") or {}
        except Exception as exc:
            log.warning("%s %r failed: %s", state, keyword, exc)
            continue
        if len(rows) >= ROW_CAP:
            capped += 1
        for row_id, row in rows.items():
            if row_id in seen:
                continue
            title = row.get("TITLE") or [""]
            seen[row_id] = Association(
                state=state, source=cfg.get("source", url), source_url=cfg.get("page", url),
                record_id=row.get("RECORD_NUM", row_id), name=ID_SUFFIX_RE.sub("", title[0]).strip(),
                corp_status=row.get("STATUS", ""), incorporated=iso_date(row.get("FILING_DATE", "")),
                registered_agent=(row.get("AGENT") or "").strip(),
                entity_type=title[1] if len(title) > 1 else "", retrieved_at=retrieved_at)
        log.info("%s %-28r -> %d rows (%d distinct so far)", state, keyword, len(rows), len(seen))
        time.sleep(cfg.get("delay", 2.0))
    if capped:
        log.warning("%s: %d queries hit the %d-row cap — the extract is partial", state, capped, ROW_CAP)
    return list(seen.values())
