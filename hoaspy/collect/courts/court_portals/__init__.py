"""Trial-court portal adapters, one module per portal, driven by the
state-courts command (hoaspy/collect/courts/get_state_courts.py).

Each adapter module exposes:

    STATE          the two-letter state the portal covers
    KEY            the slug of its output file, courts/trial_<KEY>.jsonl
    INFO           what courts/sources.json records: name, url, access, coverage, caveat
    NEEDS_COOKIE   True when a Cookie header copied from a browser is required
    COUNTIES       (optional) the upper-cased counties a county portal covers
    SWEEPS         (optional) statewide prefixes queried after the names
    class Client:  __init__(cookie: str | None, pace: float)
                   search(name: str) -> list[dict]   Case rows (hoaspy/lib/records.py)

`_common.record()` builds a Case from a portal's raw text; an adapter adds
`association_role` and whatever else the portal says (county, level, a
judgment). A Client raises PermissionError when its cookie or session has
expired (the run stops resumably) and RateLimited(retry_after) on a quota.
"""

from __future__ import annotations

import importlib
import pkgutil


class RateLimited(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"portal rate limit; retry after {retry_after}s")


def registry() -> dict[str, object]:
    """{KEY: module} for every adapter module in this package."""
    out = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(module, "KEY") and hasattr(module, "Client"):
            out[module.KEY] = module
    return out
