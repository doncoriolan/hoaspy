"""coverage.json: what has been collected for each state, and from where.

A collector records what it wrote (`update_coverage`) under
`states.<ST>.collected.<key>`; the per-state documentation pages are
generated from the file. Keys in use: `hoa_registry`, `corporate`,
`complaints`, `irs_exempt`, `local_inventory`, `condo_buildings`,
`judgment_liens`, `courts_federal`, `courts_appellate`,
`courts_bulk_dockets`, `courts_bulk_opinions`; county lien indexes sit
under `states.<ST>.counties.<County>.liens`.
"""

from __future__ import annotations

from pathlib import Path

from hoaspy import ROOT
from hoaspy.lib.jsonl import now_iso, read_json, write_json

COVERAGE_PATH = ROOT / "coverage.json"


def load(path: Path = COVERAGE_PATH) -> dict:
    """The coverage file, or an empty one when it does not exist yet."""
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("states", {})
    return data


def save(data: dict, path: Path = COVERAGE_PATH) -> None:
    """Write the coverage file back, stamping `updated_at`."""
    data["updated_at"] = now_iso()
    write_json(path, data)


def state_entry(data: dict, state: str, name: str | None = None) -> dict:
    """The entry for `state`, created when missing. A state that was only
    pending or being researched becomes `collected`."""
    entry = data["states"].setdefault(state, {"name": name or state, "collected": {}})
    entry.setdefault("collected", {})
    if entry.get("status") in (None, "pending", "researching"):
        entry["status"] = "collected"
    return entry


def update_coverage(state: str, key: str, info: dict, path: Path = COVERAGE_PATH) -> None:
    """Record what one collector wrote for one state: `info` replaces
    `states.<state>.collected.<key>`, nothing else is touched."""
    data = load(path)
    state_entry(data, state)["collected"][key] = info
    save(data, path)
