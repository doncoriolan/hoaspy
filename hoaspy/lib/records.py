"""The record types collectors write: one class per family of the record
contract (hoaspy/lib/contract.py).

A collector parses its source into instances of these classes and writes
`as_row()` of each as one JSON line. The class is the one place a family's
columns are named and typed; the contract says which of them a row must
carry and checks the files that were written. A column only one source has
(a parcel number, an EIN, a judgment amount) goes in `extras`, a dict
written next to the shared columns; the collector's docstring lists what it
puts there.

    Association(name="Palm Villas Condominium Association", state="FL",
                source="dbpr-condo-extract", source_url=SOURCE_PAGE,
                record_id="PR1234", units=120).as_row()

Every field is keyword-only, so a construction reads as a labelled record,
and every shared column is written even when it is empty, so the rows of
one family carry the same keys whichever collector wrote them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any


@dataclass(kw_only=True)
class Officer:
    """A person named on a filing, as filed: a name and a title ("President")."""

    name: str
    title: str = ""


@dataclass(kw_only=True)
class Record:
    """What the families share: the `extras` bag and `as_row()`."""

    extras: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """The record as the dict one JSON line holds: the shared columns in
        declaration order, then the extras. An extra may not repeat a
        shared column — that is a collector bug, not a data case."""
        row = {f.name: _plain(getattr(self, f.name)) for f in fields(self) if f.name != "extras"}
        repeated = row.keys() & self.extras.keys()
        if repeated:
            raise ValueError(f"extras repeat shared columns: {sorted(repeated)}")
        row.update(self.extras)
        return row


def _plain(value: Any) -> Any:
    """Nested records as dicts, lists element by element, anything else as is."""
    if isinstance(value, Record):
        return value.as_row()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@dataclass(kw_only=True)
class Association(Record):
    """One association as a registry, a corporate roster, a local inventory
    or the IRS roster lists it (contract families `registry` and `corp`)."""

    name: str
    state: str
    source: str                 # the collector or dataset the row came from
    source_url: str             # the page a reader opens to see the record
    record_id: str = ""         # the source's own identifier for the record
    dba: str = ""
    status: str = ""            # registration or filing status, as the source words it
    status_detail: str = ""
    recorded_date: str = ""     # the date the source attaches to the record: registered, issued, ruled
    address: str = ""
    city: str = ""
    county: str = ""
    zip: str = ""
    units: int | None = None
    manager_name: str = ""
    officers: list[Officer] = field(default_factory=list)
    registration_type: str = ""
    # Corporate-registry columns; empty on rows from an HOA registry.
    corp_status: str = ""
    incorporated: str = ""
    registered_agent: str = ""
    entity_type: str = ""
    document_url: str = ""      # the filed document itself, when the source links one
    retrieved_at: str = ""


@dataclass(kw_only=True)
class Complaint(Record):
    """One consumer-agency complaint about an association (family `complaint`)."""

    name: str
    state: str
    source: str
    source_url: str
    complaint_number: str = ""
    date: str = ""
    year: int | None = None
    city: str = ""
    county: str = ""
    manager_name: str = ""
    category: str = ""
    status: str = ""
    retrieved_at: str = ""


@dataclass(kw_only=True)
class Lien(Record):
    """One recorded document of the lien family (family `lien`): a claim of
    lien, a lis pendens, a release, a judgment lien. `recorded_date` is the
    day as the source prints it; `recorded_ymd` the same day in a form that
    sorts as text."""

    doc_id: str
    doc_type: str               # the shared code: LIE, LP, NCL, JL, LXA …
    doc_type_label: str         # claim_of_lien, lis_pendens …
    recorded_date: str
    recorded_ymd: str = ""
    year: int
    state: str
    county: str
    association: str = ""       # the association party; "" when no party reads as one
    filers: list[str] = field(default_factory=list)
    respondents: list[str] = field(default_factory=list)
    n_parties: int = 0
    amount: str | int | float | None = ""
    case_number: str = ""
    parcel_id: str = ""
    legal_description: str = ""
    property_address: str = ""
    source: str
    source_page: str            # the index a reader searches the document in
    retrieved_at: str = ""


@dataclass(kw_only=True)
class Case(Record):
    """One court case naming an association (family `docket`): a trial-court
    portal hit, a re:SearchTX hit, a CourtListener docket."""

    case_name: str
    court: str
    docket_number: str
    date_filed: str = ""        # YYYY-MM-DD, or "" when the source does not say
    date_terminated: str = ""
    nature_of_suit: str = ""
    cause: str = ""             # the case's status or cause of action, as the portal words it
    state: str
    associations: list[str]
    association_role: list[str] = field(default_factory=list)   # plaintiff / defendant / … as the caption has it
    url: str                    # a public link to the case, or the search page that reaches it
    case_data_id: str = ""      # the portal's own case key, when it has one beyond the docket number
    county: str = ""
    level: str = ""             # "" for a trial court; "supreme" or "appellate" above it
    source: str
    queries: list[str] = field(default_factory=list)   # the names or phrases that found the case
    retrieved_at: str = ""


@dataclass(kw_only=True)
class Opinion(Record):
    """One appellate opinion with an association in its caption (family `opinion`)."""

    case_name: str
    court: str
    state: str
    date_filed: str = ""
    associations: list[str]
    association_role: list[str] = field(default_factory=list)
    url: str
    source: str
    queries: list[str] = field(default_factory=list)
    retrieved_at: str = ""


@dataclass(kw_only=True)
class Article(Record):
    """One news article (family `news`) and the queries that surfaced it."""

    url: str
    title: str = ""
    domain: str = ""
    date: str = ""
    queries: list[dict[str, str]] = field(default_factory=list)
    retrieved_at: str = ""
