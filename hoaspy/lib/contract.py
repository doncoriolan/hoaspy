"""The record contract — what a row of collector output must carry.

Every collector writes JSON Lines, and each output file belongs to one
*family* of records: `registry`, `corp`, `complaint`, `lien`, `docket`,
`opinion`, `bulk_docket`, `bulk_opinion` or `news`. FAMILIES below says, per
family, which keys a row must have and what each may hold. This module checks
rows against it; `python -m hoaspy.lib.validate` is the command line.

A problem has one of three severities:

- **error** — the row cannot be used: a required key is missing or empty, a
  value has the wrong type, the state code is unknown, a date or a source
  link is malformed.
- **contact** — a phone number or an e-mail address in a field. The sourcing
  rules forbid storing either; `strip_contacts()` removes them, and a
  collector should call it on any free-text field a filer typed into.
- **warning** — the row is usable but worth a look: a date in the future, a
  row that names no association, a number stored as text, a status column
  holding a date (a shifted column), no `retrieved_at`.

    from hoaspy.lib import contract
    problems = contract.check(row, "lien")           # [] when the row conforms
    row, problems = contract.clean(row, "lien")      # contacts stripped; None if still unusable
    report = contract.validate_file(path)            # family read from the file name

Keys the contract does not name are left alone (collectors carry
source-specific extras), except that every value is scanned for contact
details. docs/DATA.md has the table per family.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP".split())

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# (801) 262-3900, 801-256-0465, 801.256.0465 — not a run of digits inside a
# longer number or id (parcel "12-345-678-9012", case "2024-012345-CA-01").
PHONE_RE = re.compile(r"(?<![\w-])(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.]\d{4}(?![\w-])")
URL_RE = re.compile(r"^https?://[^\s/?#]+\.[^\s/?#]+", re.I)
ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The forms a recorder's date arrives in: 2007-11-07, 20071107, 11/7/2007 (a time may follow).
ANY_DAY_RE = re.compile(r"^(?:(\d{4})-\d{2}-\d{2}|(\d{4})\d{4}|\d{1,2}/\d{1,2}/(\d{4}))(?:[ T].*)?$")
LOOKS_LIKE_DAY_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$|^\d{4}-\d{2}-\d{2}$")
FIRST_YEAR = 1900

# Keys that hold an address on the web, where "@" and digit runs mean nothing.
LINK_KEYS = ("url", "source_url", "document_url", "source_page")


def _is_link_key(key: str) -> bool:
    return key in LINK_KEYS or key.endswith("_url")


@dataclass(frozen=True)
class Problem:
    severity: str        # "error" | "contact" | "warning"
    rule: str            # short slug: "missing", "empty", "type", "state", "url", "date", "contact", …
    key: str             # the row key it is about ("" for the row as a whole)
    detail: str = ""

    def __str__(self) -> str:
        where = f" `{self.key}`" if self.key else ""
        return f"{self.severity}: {self.rule}{where}" + (f" — {self.detail}" if self.detail else "")


# ---- field kinds ---------------------------------------------------------
#
# A kind is a function (value, limits) -> None when the value conforms, else a
# list of (severity, rule, detail). `limits` is (the first day that counts as
# the future, the last plausible year). A trailing "?" on a kind's name in
# FAMILIES means the value may be empty.

_LIMITS: dict[date, tuple[str, int]] = {}


def _limits(today: date | None) -> tuple[str, int]:
    today = today or date.today()
    if today not in _LIMITS:
        _LIMITS[today] = ((today + timedelta(days=2)).isoformat(), today.year + 1)
    return _LIMITS[today]


def _wrong(expected: str, v) -> list:
    return [("error", "type", f"expected {expected}, got {type(v).__name__}")]


def _text(v, limits):
    return None if type(v) is str else _wrong("text", v)


def _label(v, limits):
    """A status or type word. A date here means the columns shifted."""
    if type(v) is not str:
        return _wrong("text", v)
    if v[:1].isdigit() and LOOKS_LIKE_DAY_RE.match(v):
        return [("warning", "shifted", f"a date ({v}) where a status belongs")]
    return None


def _state(v, limits):
    if v in STATES:
        return None
    if type(v) is not str:
        return _wrong("a state code", v)
    return [("error", "state", f"{v!r} is not a US state or territory code")]


def _url(v, limits):
    if type(v) is not str:
        return _wrong("a link", v)
    return None if URL_RE.match(v) else [("error", "url", f"{v[:60]!r} is not an http(s) address")]


def _iso(v: str) -> str:
    """A date in any accepted form as YYYY-MM-DD ("" when it is none of them)."""
    head = v.split("T")[0].split(" ")[0]
    if ISO_DAY_RE.match(head):
        return head
    if re.fullmatch(r"\d{8}", head):
        return f"{head[:4]}-{head[4:6]}-{head[6:]}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", head)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def _day(v, limits):
    """YYYY-MM-DD exactly: the site sorts and compares these as text. A day
    past tomorrow is flagged (a source in a later time zone may be a day
    ahead)."""
    if type(v) is not str:
        return _wrong("a date", v)
    if not ISO_DAY_RE.match(v):
        return [("error", "date", f"{v[:30]!r} is not YYYY-MM-DD")]
    return [("warning", "future-date", v)] if v >= limits[0] else None


def _anyday(v, limits):
    """A date as the source prints it: YYYY-MM-DD, YYYYMMDD or M/D/YYYY."""
    if type(v) is not str:
        return _wrong("a date", v)
    m = ANY_DAY_RE.match(v)
    if not m:
        return [("error", "date", f"{v[:30]!r} is not a date")]
    if (m.group(1) or m.group(2) or m.group(3)) < limits[0][:4]:      # an earlier year: not the future
        return None
    return [("warning", "future-date", v)] if _iso(v) >= limits[0] else None


def _year(v, limits):
    if type(v) is not int:
        return _wrong("a year as a number", v)
    return None if FIRST_YEAR <= v <= limits[1] else [("warning", "year-range", str(v))]


def _count(v, limits):
    """A whole number. Digits stored as text are usable but flagged."""
    if type(v) in (int, float):
        return None
    if type(v) is str and v.strip().replace(",", "").isdigit():
        return [("warning", "number-as-text", repr(v))]
    return [("error", "type", f"expected a number, got {v!r:.40}")]


def _ident(v, limits):
    return None if type(v) in (str, int) else _wrong("an id", v)


def _names(v, limits):
    if type(v) is not list or any(type(x) is not str for x in v):
        return [("error", "type", "expected a list of names")]
    return None


def _items(v, limits):
    if type(v) is not list or any(type(x) is not dict for x in v):
        return [("error", "type", "expected a list of objects")]
    return None


KINDS = {"text": _text, "label": _label, "state": _state, "url": _url, "day": _day, "anyday": _anyday,
         "year": _year, "count": _count, "id": _ident, "names": _names, "items": _items}


def _empty(v) -> bool:
    return v is None or v == "" or v == []


@dataclass(frozen=True)
class Family:
    """`required`: the key must be present; a kind ending in "?" may be empty.
    `optional`: checked only when present and not empty. `identity`: the keys
    that make a row unique within its file. `subject`: the key naming the
    association(s) — an empty one is a warning, the row attaches to nothing.
    `empty_state`: "warning" where the build skips a row with no state."""
    required: dict[str, str]
    optional: dict[str, str] = field(default_factory=dict)
    identity: tuple[str, ...] = ()
    subject: str = ""
    empty_state: str = "error"


_REGISTRY_OPTIONAL = {
    "source_url": "url", "document_url": "url", "retrieved_at": "text", "record_id": "id",
    "status": "label", "corp_status": "label", "status_detail": "text", "units": "count",
    "address": "text", "city": "text", "county": "text", "zip": "text", "manager_name": "text",
    "officers": "items",
}
_DOCKET_REQUIRED = {
    "case_name": "text?", "court": "text?", "docket_number": "text", "date_filed": "day?",
    "date_terminated": "day?", "nature_of_suit": "text?", "cause": "text?", "url": "url",
    "state": "state", "associations": "names?", "source": "text", "retrieved_at": "text",
}
_DOCKET_OPTIONAL = {"association_role": "names", "queries": "names", "county": "text", "zip": "text",
                    "level": "text", "disposition": "text"}

FAMILIES: dict[str, Family] = {
    # Who the associations are: state registries, local inventories, the IRS roster.
    "registry": Family(
        required={"name": "text", "state": "state", "source": "text", "source_url": "url"},
        optional=_REGISTRY_OPTIONAL, identity=("source", "record_id", "name")),
    # State corporate registries.
    "corp": Family(
        required={"name": "text", "state": "state", "source": "text", "source_url": "url"},
        optional={**_REGISTRY_OPTIONAL, "incorporated": "anyday", "registered_agent": "text"},
        identity=("source", "record_id", "name")),
    # Consumer-agency complaints.
    "complaint": Family(
        required={"name": "text", "state": "state", "source": "text", "source_url": "url"},
        optional={"year": "year", "date": "day", "category": "text", "status": "label",
                  "retrieved_at": "text", "county": "text", "city": "text", "manager_name": "text"},
        identity=("source", "complaint_number")),
    # County recorder indexes: claims of lien, lis pendens, releases.
    "lien": Family(
        required={"association": "text?", "state": "state", "county": "text?", "doc_id": "id",
                  "doc_type": "text", "year": "year", "recorded_date": "anyday", "source": "text",
                  "source_page": "url", "retrieved_at": "text"},
        optional={"recorded_ymd": "anyday", "respondents": "names", "filers": "names",
                  "doc_type_label": "text", "case_number": "id", "property_address": "text"},
        identity=("source", "doc_id", "association"), subject="association"),
    # Court cases: CourtListener dockets, re:SearchTX, the trial-court adapters.
    "docket": Family(required=_DOCKET_REQUIRED, optional=_DOCKET_OPTIONAL,
                     identity=("source", "court", "docket_number"), subject="associations"),
    "opinion": Family(
        required={"case_name": "text?", "court": "text?", "date_filed": "day?", "url": "url",
                  "state": "state", "associations": "names?", "source": "text", "retrieved_at": "text"},
        optional={"queries": "names"}, identity=("url",), subject="associations",
        empty_state="warning"),
    # CourtListener's bulk files: every court it holds, so a row may have no state.
    "bulk_docket": Family(
        required={**_DOCKET_REQUIRED, "docket_number": "text?", "docket_id": "id", "jurisdiction": "text"},
        optional=_DOCKET_OPTIONAL, identity=("docket_id",), subject="associations",
        empty_state="warning"),
    "bulk_opinion": Family(
        required={"case_name": "text?", "court": "text?", "date_filed": "day?", "url": "url",
                  "state": "state", "associations": "names?", "source": "text", "retrieved_at": "text",
                  "cluster_id": "id", "docket_id": "id", "jurisdiction": "text"},
        optional={"queries": "names", "docket_number": "text", "association_role": "names"},
        identity=("cluster_id",), subject="associations", empty_state="warning"),
    "news": Family(
        required={"title": "text?", "url": "url", "domain": "text", "date": "text", "queries": "items"},
        identity=("url",)),
}

# Which family a file belongs to, by its place under the repository root.
# First match wins; a file that matches nothing is not collector output.
_FILE_RULES = (
    (re.compile(r"(?:^|/)records/(?:llm_audit_\w+|member_links)\.jsonl$"), None),      # derived, not collected
    (re.compile(r"(?:^|/)records/state_corps\.jsonl$"), "corp"),
    (re.compile(r"(?:^|/)records/\w*complaints\w*\.jsonl$"), "complaint"),
    (re.compile(r"(?:^|/)records/\w+\.jsonl$"), "registry"),
    (re.compile(r"(?:^|/)liens/liens\w*\.jsonl$"), "lien"),
    (re.compile(r"(?:^|/)courts/bulk_dockets\.jsonl$"), "bulk_docket"),
    (re.compile(r"(?:^|/)courts/bulk_opinions\.jsonl$"), "bulk_opinion"),
    (re.compile(r"(?:^|/)courts/opinions\.jsonl$"), "opinion"),
    (re.compile(r"(?:^|/)courts/\w+\.jsonl$"), "docket"),
    (re.compile(r"(?:^|/)news/\w+\.jsonl$"), "news"),
)
# Checkpoints and logs a collector leaves beside its output while it runs.
_SIDECAR_RE = re.compile(r"_(?:done|partial|run|names|rejected|remaining|raw)\.jsonl$|(?:^|/)\.")


def family_of(path: str | Path) -> str | None:
    """The family of a collector output file, from its directory and name;
    None for checkpoints and files no collector writes."""
    posix = Path(path).as_posix()
    if _SIDECAR_RE.search(posix):
        return None
    for pattern, family in _FILE_RULES:
        if pattern.search(posix):
            return family
    return None


# ---- contact details -----------------------------------------------------

# Every phone number PHONE_RE accepts ends "-dddd" or ".dddd" after three
# digits. Starting the pattern on the separator lets the regex engine skip
# from one "-" or "." to the next instead of trying every character.
_PHONE_TAIL_RE = re.compile(r"[-.]\d{4}(?![\w-])(?<=\d{3}.{5})")


def may_have_contact(line: str) -> bool:
    """Cheap test on a raw JSON line: False means no value in it can hold an
    e-mail address or a phone number, so the per-field scan can be skipped."""
    return "@" in line or _PHONE_TAIL_RE.search(line) is not None


def strip_contacts(text: str) -> str:
    """`text` without e-mail addresses and phone numbers, whitespace tidied:
    "954-972-3410 7700 NW 5TH COURT" -> "7700 NW 5TH COURT". A value that was
    nothing but a contact detail comes back empty."""
    out = PHONE_RE.sub(" ", EMAIL_RE.sub(" ", text))
    if out == text:
        return text
    return re.sub(r"\s{2,}", " ", out).strip(" ,;:-/")


def _has_contact(text: str) -> bool:
    return EMAIL_RE.search(text) is not None or PHONE_RE.search(text) is not None


def _contact_keys(row: dict) -> list[str]:
    hits = []
    for key, value in row.items():
        if _is_link_key(key):
            continue
        if isinstance(value, str):
            if _has_contact(value):
                hits.append(key)
        elif isinstance(value, (list, dict)) and value:
            if _has_contact(json.dumps(value, ensure_ascii=False)):
                hits.append(key)
    return hits


def _strip_value(value):
    if isinstance(value, str):
        return strip_contacts(value)
    if isinstance(value, list):
        cleaned = [_strip_value(v) for v in value]
        return [v for v in cleaned if not _empty(v)]
    if isinstance(value, dict):
        return {k: (v if _is_link_key(k) else _strip_value(v)) for k, v in value.items()}
    return value


# ---- checking rows -------------------------------------------------------

_COMPILED: dict[str, tuple] = {}
_SEVERITY_ORDER = {"error": 0, "contact": 1, "warning": 2}


def _compiled(family: str) -> tuple:
    """FAMILIES[family] as tuples the per-row loop can walk quickly."""
    if family not in _COMPILED:
        spec = FAMILIES[family]
        required = tuple((key, KINDS[kind.rstrip("?")], kind.endswith("?")) for key, kind in spec.required.items())
        optional = tuple((key, KINDS[kind]) for key, kind in spec.optional.items() if key not in spec.required)
        _COMPILED[family] = (spec, required, optional, "retrieved_at" not in spec.required)
    return _COMPILED[family]


def check(row, family: str, today: date | None = None, contacts: bool = True) -> list[Problem]:
    """Every problem with one row, errors first; [] when it conforms.
    `contacts=False` skips the scan for phone numbers and e-mail addresses
    (when `may_have_contact` already said the line has none)."""
    spec, required, optional, loose_retrieved = _compiled(family)
    limits = _limits(today)
    if type(row) is not dict:
        return [Problem("error", "type", "", "the line is not a JSON object")]
    out: list[Problem] = []

    for key, kind, may_be_empty in required:
        if key not in row:
            out.append(Problem("error", "missing", key))
            continue
        value = row[key]
        if value is None or value == "" or value == []:
            if key == "state" and spec.empty_state == "warning":
                out.append(Problem("warning", "no-state", key, "the row cannot be placed in a state"))
            elif key == spec.subject:
                out.append(Problem("warning", "no-association", key, "the row names no association"))
            elif key == "retrieved_at":
                out.append(Problem("warning", "no-retrieved-at", key))
            elif not may_be_empty:
                out.append(Problem("error", "empty", key))
            continue
        found = kind(value, limits)
        if found:
            out += [Problem(sev, rule, key, detail) for sev, rule, detail in found]
    if loose_retrieved and not row.get("retrieved_at"):
        out.append(Problem("warning", "no-retrieved-at", "retrieved_at"))

    for key, kind in optional:
        value = row.get(key)
        if value is None or value == "" or value == []:
            continue
        found = kind(value, limits)
        if found:
            out += [Problem(sev, rule, key, detail) for sev, rule, detail in found]

    if contacts:
        out += [Problem("contact", "contact", key, "a phone number or an e-mail address")
                for key in _contact_keys(row)]
    if len(out) > 1:
        out.sort(key=lambda p: _SEVERITY_ORDER[p.severity])
    return out


def clean(row, family: str, today: date | None = None, contacts: bool = True) -> tuple[dict | None, list[Problem]]:
    """What a consumer of collector output should do with a row: strip
    contact details from the fields that hold them, then drop the row if it
    is still unusable. Returns (row or None, the problems found). The row is
    a copy only when something was stripped."""
    problems = check(row, family, today, contacts)
    if not problems:
        return row, problems
    hit = [p.key for p in problems if p.severity == "contact"]
    if hit:
        row = dict(row)
        for key in hit:
            row[key] = _strip_value(row[key])
        after = check(row, family, today, contacts=False)
        problems = [p for p in problems if p.severity == "contact"] + after
    if any(p.severity == "error" for p in problems):
        return None, problems
    return row, problems


# ---- checking files ------------------------------------------------------

@dataclass
class Report:
    path: str
    family: str
    rows: int = 0
    bad_rows: int = 0                                # rows with at least one error
    contact_rows: int = 0                            # rows with a contact detail
    counts: Counter = field(default_factory=Counter)  # (severity, rule, key) -> rows
    examples: dict = field(default_factory=dict)      # (severity, rule, key) -> [(line no, detail)]
    duplicates: int = 0
    hosts: Counter = field(default_factory=Counter)   # host of each row's source link

    @property
    def ok(self) -> bool:
        return self.bad_rows == 0 and self.contact_rows == 0

    def total(self, severity: str) -> int:
        return sum(n for (sev, _, _), n in self.counts.items() if sev == severity)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "family": self.family, "rows": self.rows, "ok": self.ok,
            "bad_rows": self.bad_rows, "contact_rows": self.contact_rows, "duplicates": self.duplicates,
            "problems": [{"severity": sev, "rule": rule, "key": key, "rows": n,
                          "examples": [{"line": ln, "detail": d} for ln, d in self.examples.get((sev, rule, key), [])]}
                         for (sev, rule, key), n in sorted(self.counts.items(), key=_problem_order)],
            "hosts": dict(self.hosts.most_common(10)),
        }

    def lines(self) -> list[str]:
        """The report as text: one headline, then one line per kind of problem."""
        verdict = "ok" if self.ok else "FAILED"
        parts = [f"{self.rows:,} rows"]
        if self.bad_rows:
            parts.append(f"{self.bad_rows:,} unusable")
        if self.contact_rows:
            parts.append(f"{self.contact_rows:,} with contact details")
        if self.total("warning"):
            parts.append(f"{self.total('warning'):,} warnings")
        out = [f"{self.path}  [{self.family}]  {verdict}: " + ", ".join(parts)]
        for (sev, rule, key), n in sorted(self.counts.items(), key=_problem_order):
            where = f" `{key}`" if key else ""
            first = self.examples.get((sev, rule, key), [])
            eg = f"  e.g. line {first[0][0]}" + (f": {first[0][1]}" if first[0][1] else "") if first else ""
            out.append(f"  {sev:8s} {rule}{where}: {n:,} rows{eg}")
        if self.duplicates:
            out.append(f"  warning  duplicate: {self.duplicates:,} rows repeat an earlier row's "
                       f"({', '.join(FAMILIES[self.family].identity)})")
        return out


def _problem_order(item):
    (sev, rule, key), _ = item
    return (_SEVERITY_ORDER[sev], rule, key)


def validate_file(path: str | Path, family: str | None = None, today: date | None = None,
                  examples: int = 3) -> Report:
    """Check every row of a JSON Lines file. `family` defaults to the one the
    file's name implies (`family_of`); ValueError when there is none."""
    path = Path(path)
    family = family or family_of(path)
    if family not in FAMILIES:
        raise ValueError(f"{path}: not a known collector output — name its family "
                         f"({', '.join(FAMILIES)})")
    spec = FAMILIES[family]
    report = Report(str(path), family)
    link_key = next((k for k in LINK_KEYS if k in spec.required), "")
    seen: set = set()

    def note(problem: Problem, lineno: int) -> None:
        key = (problem.severity, problem.rule, problem.key)
        report.counts[key] += 1
        found = report.examples.setdefault(key, [])
        if len(found) < examples:
            # never echo the contact detail itself into a report
            found.append((lineno, "" if problem.severity == "contact" else problem.detail))

    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            report.rows += 1
            try:
                row = json.loads(line)
            except ValueError as exc:
                report.bad_rows += 1
                note(Problem("error", "json", "", str(exc)[:80]), lineno)
                continue
            problems = check(row, family, today, contacts=may_have_contact(line))
            for problem in problems:
                note(problem, lineno)
            if any(p.severity == "error" for p in problems):
                report.bad_rows += 1
            if any(p.severity == "contact" for p in problems):
                report.contact_rows += 1
            if isinstance(row, dict):
                if spec.identity:
                    ident = tuple(str(row.get(k)) for k in spec.identity)
                    if ident in seen:
                        report.duplicates += 1
                    seen.add(ident)
                link = row.get(link_key)
                if isinstance(link, str) and link:
                    report.hosts[urlsplit(link).netloc.lower()] += 1
    return report
