"""Jittered request pacing with a run-global rate-limit backoff.

Two ideas:

- **Every wait is drawn from a range**, so requests are spread out rather
  than landing on the source at a fixed beat.
- **The backoff level is global, not per-target.** Sources rate-limit by
  client, so a 429 anywhere means the whole run should slow down — and a
  success *decays* the level by one rather than resetting it, so one lucky
  response doesn't undo a whole climb back up.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger("hoa.pacing")

# Seconds. Ranges are [lo, hi], sampled uniformly.
DEFAULT_PACING = {
    "between_requests": [2, 4],     # between paged API calls
    "between_steps": [5, 12],       # after finishing a query or listing
    "backoff_base": 120,            # first 429 wait, doubles each time
    "backoff_max": 1800,            # cap on a single backoff wait
    "max_429_retries": 4,
}


class Pacer:
    def __init__(self, cfg: dict | None = None, fast: bool = False) -> None:
        self.cfg = {**DEFAULT_PACING, **(cfg or {})}
        self.fast = fast
        self.level = 0
        self.total_slept = 0.0
        self.backoffs = 0

    def _sleep(self, seconds: float, why: str) -> None:
        if self.fast:
            return
        self.total_slept += seconds
        log.debug("sleeping %.1fs (%s)", seconds, why)
        time.sleep(seconds)

    def wait(self, kind: str) -> None:
        lo, hi = self.cfg[kind]
        self._sleep(random.uniform(lo, hi), kind)

    def backoff(self, what: str) -> bool:
        """Sleep after a rate-limit response. False when retries are exhausted."""
        if self.level >= self.cfg["max_429_retries"]:
            log.error("still rate limited after %d backoffs (%s) — giving up on it",
                      self.level, what)
            return False
        wait = min(self.cfg["backoff_base"] * (2 ** self.level), self.cfg["backoff_max"])
        self.level += 1
        self.backoffs += 1
        log.warning("rate limited (%s) — backing off %.0fs [level %d]",
                    what, wait, self.level)
        self._sleep(wait + random.uniform(0, 10), "backoff")
        return True

    def ok(self) -> None:
        """A successful request: decay the level rather than resetting it."""
        if self.level:
            self.level -= 1

    def summary(self) -> dict:
        return {
            "total_slept_seconds": round(self.total_slept, 1),
            "backoffs": self.backoffs,
            "final_backoff_level": self.level,
        }
