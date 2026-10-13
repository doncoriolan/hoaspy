"""The fifty states and the District of Columbia, by postal code."""

from __future__ import annotations

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}

STATE_CODES = frozenset(STATE_NAMES)

# Longest names first, so "West Virginia" is found before "Virginia" and
# "Arkansas" before the "Kansas" inside it.
_BY_LENGTH = sorted(STATE_NAMES.items(), key=lambda item: -len(item[1]))


def state_in(text: str) -> str:
    """The code of the first state whose name appears in `text` as whole
    words ("District Court, E.D. Michigan" -> "MI"); "" when none does."""
    import re

    for code, name in _BY_LENGTH:
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return code
    return ""
