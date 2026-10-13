"""How a collector identifies itself to a source, and a session to do it with."""

from __future__ import annotations

import requests

# The project's own identity, for sources that take any client.
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"

# For portals that answer only a browser-shaped client. The Chrome version
# is updated now and then; a site that pairs the version with client-hint
# headers builds those from this string.
BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def session(user_agent: str = USER_AGENT, headers: dict[str, str] | None = None) -> requests.Session:
    """A requests session carrying the user agent and any extra headers."""
    opened = requests.Session()
    opened.headers["User-Agent"] = user_agent
    if headers:
        opened.headers.update(headers)
    return opened
