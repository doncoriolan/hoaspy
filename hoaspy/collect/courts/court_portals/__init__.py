"""Trial-court portal adapters for get_state_courts.py.

Each adapter module exposes:

    STATE     two-letter state the portal covers
    KEY       slug used for the output file  courts/trial_<KEY>.jsonl
    INFO      dict for courts/sources.json: name, url, access, coverage, caveat
    NEEDS_COOKIE   True when a human-copied Cookie header is required
    class Client:  __init__(cookie: str | None, pace: float)
                   search(name: str) -> list[dict]   docket records (see below)

Docket record shape (identical to courts/tx_research.jsonl so build_site's
add_courts() folds it in unchanged):

    case_name, court, docket_number, date_filed (YYYY-MM-DD or ""),
    date_terminated, nature_of_suit, cause (status), state, associations
    (list of matched party names), url (public deep link), case_data_id,
    source (== KEY), queries (list of names that found it), retrieved_at

Adapters raise PermissionError when their cookie/session has expired (the
run stops resumably) and RateLimited(retry_after) on a quota hit.
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
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{m.name}")
        if hasattr(mod, "KEY") and hasattr(mod, "Client"):
            out[mod.KEY] = mod
    return out
