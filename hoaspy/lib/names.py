"""Which names read as a community association.

Three vocabularies, because three kinds of text are read and each was
tuned on its own data:

- `REGISTRY_NAME_RE` — an entity name in a corporate roster, a local
  inventory or the IRS file. Decides whether a row is kept at all, so it
  wants the words a community puts in its registered name.
- `LIEN_PARTY_RE` — a party name in a county recorder's index. Clerks
  abbreviate ("ASSN INC") and write "VILLAS OF …", so those forms count.
- `CORPORATION_NAME_RE` — a corporation name in a Secretary of State
  search (Florida's Sunbiz extract, California's bizfile), where plurals
  and possessives vary ("HOMEOWNERS'", "TOWNHOMES").

The trial-court adapters keep their own, wider vocabulary in
hoaspy/collect/courts/court_portals/_common.py, where a party must also
be told apart from a business wearing the same words.
"""

from __future__ import annotations

import re

REGISTRY_NAME_RE = re.compile(
    r"(HOMEOWNER|HOME OWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNERS|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|OWNERS ASSOCIATION|"
    r"RESIDENTS ASSOCIATION|TOWNHOME|TOWNHOUSE|\bHOA\b)", re.I)

LIEN_PARTY_RE = re.compile(
    r"(HOMEOWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNER|MASTER ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|\bHOA\b|TOWNHOUSE|TOWNHOME|VILLAS? OF|"
    r"ASSOCIATION,? INC|ASSN,? INC|\bASSN\b)", re.I)

CORPORATION_NAME_RE = re.compile(
    r"\b(HOMEOWNERS?|HOME OWNERS?|CONDOMINIUM|CONDO\b|PROPERTY OWNERS?|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|RESIDENTS ASSOCIATION|"
    r"TOWNHOMES?\b|TOWNHOUSES?\b|VILLAS\b|HOA\b)", re.I)
