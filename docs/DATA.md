# Data model & matching rules

How the collectors' outputs become the per-community reports on the site.
Read this before changing `build_site.py`.

## The entity model

The unit of everything is the **community**: one entity per distinct
normalized association name per state (`(state, normalized_name)` key, id =
first 10 hex of sha1). Entities are assembled from sources in this order,
each able to create new entities or attach to existing ones:

1. registrations (`records/associations.jsonl` — FL DBPR + TX TREC;
   `records/state_registries.jsonl` — VA DPOR, HI AOUO, UT HOA Registry,
   WI DFI Homeowners' Association registry (Wis. Stat. 710.085 public
   notices swept by `get_wi_dfi.py`; HOAs only, no condos, filings since 2022),
   MD Montgomery County CCOC, NV Clark County / Henderson / Las Vegas GIS
   HOA layers; `records/irs_exempt_orgs.jsonl` — IRS EO BMF
   tax-exempt associations in every state, folded as registrations but
   *not* counted as a state registry for coverage purposes;
   `records/gov_layers.jsonl` — county/city HOA inventories from
   `get_gov_layers.py`, folded the same way and flagged `local_inventory`)
2. corporations (`.cache/sunbiz/` FL; `records/state_corps.jsonl` others,
   incl. CA — the CA Secretary of State bizfile business search, keyword-
   aggregated by `get_ca_sos.py` because CA has no HOA registry to download,
   and WA — the SoS CCFS advanced search swept through BrowserOS by
   `get_wa_ccfs.py`, list rows only: status, formation date, agent,
   principal-office address; no governors yet)
3. liens (`liens/liens.jsonl` Broward; `liens/liens_miamidade.jsonl`
   Miami-Dade, best-effort per-association; `liens/liens_nyc.jsonl` ACRIS; `liens/liens_cook.jsonl` Cook County IL
   Clerk recordings, a party-name sweep (LXA = a lien against the association, counted under other);
   `liens/liens_ca_ucc.jsonl` CA Secretary of
   State UCC index — judgment liens, statewide, `hoa_role` creditor/debtor,
   doc_type JL counts with claims of lien, JLX = judgment against the HOA)
4. consumer complaints (`records/sc_complaints.jsonl`)
5. court dockets + opinions (`courts/dockets.jsonl`, `courts/opinions.jsonl`
   CourtListener; `courts/bulk_dockets.jsonl`, `courts/bulk_opinions.jsonl`
   CourtListener's bulk files, collected by `get_courts_bulk.py` and folded
   in once per case by `add_bulk_courts` — federal dockets, and state
   appellate cases as their opinion or, with none, as a `level: appellate`
   docket; `courts/tx_research.jsonl` re:SearchTX TX trial courts,
   best-effort per-association — same docket record shape; every
   `courts/trial_*.jsonl` written by `get_state_courts.py` — one file per
   state/county portal adapter in `hoaspy/collect/courts/court_portals/`, same shape plus an
   `association_role` list — folded in the same way; `courts/trial_fl_miamidade.jsonl`
   is written by `records_miamidade_civil.py` from the Miami-Dade Clerk's
   Civil FTP feed instead of a portal search: same shape plus `county`,
   `zip` (the sued property's ZIP, never its street address or owner) and a
   per-case public Online Case Search link — `add_courts` carries `zip` and
   `association_role` onto the case and uses `county` as the match hint)
6. news (`news/articles.jsonl`, when present)

## The record contract

Every collector writes JSON Lines, and each output file belongs to one
*family* of records. `hoaspy/lib/contract.py` says which keys a row of each
family must carry and what each may hold. `python -m hoaspy.lib.validate`
checks files against it, and the site build reads every file through it, so
a collector that starts writing something else is caught before its rows are
used.

| Family | Files | Keys every row carries | A row is unique by |
| --- | --- | --- | --- |
| `registry` | every `records/*.jsonl` not listed below | `name`, `state`, `source`, `source_url` | `source`, `record_id`, `name` |
| `corp` | `records/state_corps.jsonl` | `name`, `state`, `source`, `source_url` | `source`, `record_id`, `name` |
| `complaint` | `records/*complaints*.jsonl` | `name`, `state`, `source`, `source_url` | `source`, `complaint_number` |
| `lien` | `liens/liens*.jsonl` | `association`, `state`, `county`, `doc_id`, `doc_type`, `year`, `recorded_date`, `source`, `source_page`, `retrieved_at` | `source`, `doc_id`, `association` |
| `docket` | `courts/dockets.jsonl`, `courts/tx_research.jsonl`, `courts/trial_*.jsonl` | `case_name`, `court`, `docket_number`, `date_filed`, `date_terminated`, `nature_of_suit`, `cause`, `url`, `state`, `associations`, `source`, `retrieved_at` | `source`, `court`, `docket_number` |
| `opinion` | `courts/opinions.jsonl` | `case_name`, `court`, `date_filed`, `url`, `state`, `associations`, `source`, `retrieved_at` | `url` |
| `bulk_docket` | `courts/bulk_dockets.jsonl` | the `docket` keys, plus `docket_id` and `jurisdiction` | `docket_id` |
| `bulk_opinion` | `courts/bulk_opinions.jsonl` | the `opinion` keys, plus `cluster_id`, `docket_id` and `jurisdiction` | `cluster_id` |
| `news` | `news/*.jsonl` | `title`, `url`, `domain`, `date`, `queries` | `url` |

Checkpoints and logs beside an output file (`.trial_<KEY>_partial.jsonl`,
`*_done.jsonl`, `*_raw.jsonl`, …) belong to no family and are not checked.
Keys the contract does not name are left alone — collectors carry
source-specific extras — except that every value is scanned for contact
details.

**Value shapes.** `state` is a two-letter US state or territory code.
Court dates (`date_filed`, `date_terminated`, a complaint's `date`) are
`YYYY-MM-DD` or empty, because the site sorts and compares them as text. A
recorder's date (`recorded_date`, `recorded_ymd`, `incorporated`) may be
`YYYY-MM-DD`, `YYYYMMDD` or `M/D/YYYY`, as the source prints it. `year` is a
number. A source link (`url`, `source_url`, `source_page`) is an `http(s)`
address. `associations`, `respondents` and `filers` are lists of names.
Some keys must be present but may be empty: a lien's `association` and
`county`, a docket's `case_name`, `court`, the two dates, `nature_of_suit`
and `cause`.

**Three severities.**

| Severity | What it means | Examples |
| --- | --- | --- |
| `error` | the row cannot be used | a required key missing or empty; `year` kept as text; `state: "Florida"`; `date_filed: "03/14/2024"`; a source link that is not an address; a line that is not JSON |
| `contact` | a phone number or an e-mail address in a field — collectors never store either | a managing entity's phone number typed into its address line; an e-mail address where the association's name goes |
| `warning` | usable, but worth a look | a date after tomorrow; a `year` before 1900; a row that names no association, or (court files) no state; digits stored as text; a date where a status belongs (a shifted column); no `retrieved_at`; a repeated row |

`contract.strip_contacts(text)` removes phone numbers and e-mail addresses
and keeps the rest (`"954-555-0134 7700 NW 5TH COURT"` →
`"7700 NW 5TH COURT"`); a collector calls it on any free-text field a filer
typed into. Links are never scanned, and neither are digit runs that belong
to a longer id (a parcel number, a case number).

**The command.**

```bash
./venv/bin/python -m hoaspy.lib.validate                          # every output file under records/ liens/ courts/ news/
./venv/bin/python -m hoaspy.lib.validate liens/liens_cook.jsonl   # some files
./venv/bin/python -m hoaspy.lib.validate --family lien bundle.jsonl   # a file whose name does not say what it is
./venv/bin/python -m hoaspy.lib.validate --json report.json       # the same report as JSON ("-" for stdout)
```

It prints, per file, the row count and one line per kind of problem with the
first line number it occurs on, and exits 1 when any file has an `error` or a
`contact` (`--strict`: a warning too), 2 when a file's family cannot be told.
A contact detail is reported by line and key only — the report never repeats
the phone number or the address.

**In the site build.** `Builder.rows(path, family)` is how `build_site.py`
reads every collector file. It strips a contact detail from the field that
holds it, so none reaches the site whatever a collector let through, and
drops a row that is still unusable — an e-mail address where the name goes
leaves no name. The tallies are logged and written to `meta.json` as
`stats.contract` (`rows`, `dropped`, `stripped`, and per affected file the
count of each problem). A few unusable rows are a source's own mess; when
one file has at least 20 of them and they are more than 1% of its rows
(`MIN_UNUSABLE_ROWS`, `MAX_UNUSABLE_SHARE`), that is a collector bug and the
build stops before it writes anything.

## Where a community is (`hoaspy/build/places.py`)

A community's `city` / `zip` / `address` come from the first record that
carries them, but only when that record places it in its own state:

- `Builder.take_location` skips a record whose ZIP is in another state
  (`zip_state`, a USPS three-digit prefix table) — a registered agent's,
  manager's or developer's mailing address — and drops a "city" that is a
  street line or PO box (`is_address_line`; the NC OneMap mailing-city column
  does this for ~2k parcels). A Fort Lauderdale condo used to read
  "Boston, FL" from its manager's Sunbiz address.
- `Builder.validate_locations` runs last: a location still in another state
  is cleared, a street-line city is cleared, and every remaining city is
  checked against the Census 2023 gazetteer (places + county subdivisions,
  cached under `.cache/geocode/`; `norm_place` expands Ft./N./St. and strips
  "city/town/CDP/township"). A city the gazetteer knows only in *other*
  states is cleared when no in-state ZIP vouches for it; USPS-only names
  (Flushing, Coconut Grove, Hallandale) stay and count as *unverified*. The
  counts land in `meta.json` `stats.locations` and the build log.
- The learned ZIP → county map is keyed by state for the same reason.

## Name normalization

`normalize()`: uppercase, strip punctuation, expand ASSN/ASSOC→ASSOCIATION,
CONDO(S)→CONDOMINIUM, HOMEOWNER→HOMEOWNERS; drop trailing filing noise
(INC, "ET AL", OBO, clerk truncations); drop leading THE.

`core_of()`: the distinctive part — drops generic tokens (ASSOCIATION,
HOMEOWNERS, CONDOMINIUM, PHASE, numbers, roman numerals…). "SUNRISE LAKES
CONDOMINIUM PHASE 4" → core "SUNRISE LAKES".

## Excluded entities (financial institutions)

National banks are chartered as "… National Association" (the N.A. in
"U.S. Bank, N.A."), so their mortgage-foreclosure filings in county indexes
match the association-name filter. `is_financial()` in build_site.py drops a
name at entity creation when it has a **bank word-form** (incl. clerk
misspellings: BANKN, SBANK, BANKNATIONAL, NATIONALK, NAIONTAL…) in
**financial context** (NATIONAL/NATL/NAT, TRUST, FEDERAL, MORTGAGE, series/
certificate language, TR/SAV/CUST suffixes), or a known **brand** (JPMorgan,
HSBC, Citibank, Wachovia…). A **community token** (HOMEOWNERS, CONDOMINIUM,
OWNERS ASSOCIATION, ESTATES, VILLAS…) always overrides the exclusion — so
"Bankston Meadows Homeowners Association", "Marlbank Cove", "The Bank
Building" condo, and multi-party court captions naming a bank *and* a real
HOA all stay in. ~418 records excluded as of 2026-08. Non-community industry
orgs (insurers, realtor boards, utility co-ops) are additionally excluded
from SEO profile pages by `NON_COMMUNITY_RE` in build_seo.py.

## Court parties that never become communities

Trial-court portals match parties on leading words, so a query for
"GOLDEN LAKES, A CONDO" also returned *Golden Lakes Medical Center Inc*,
and *Third Avenue Chiropractic Ctr* came back for a Third Avenue condo.
`Builder.party_ok` (using `party_is_association` / `looks_like_association`
from the collectors' `court_portals/_common.py`) decides whether a party
that matches no existing community may become one: yes if it carries an
association marker (association, assn, HOA/POA/COA, condominium, homeowners,
property owners, townhomes, cooperative, board of managers) and is not a
business (`has_business_form`: an owner form LLC/LP, Ltd/PLLC without the
full word Association, or a business activity — finance, mortgage, bank,
insurance/underwriters, realty, management, services, associates, leasing,
apartments — *after* the last association word, so "Homeowners Finance Co."
and "Community Association Underwriters" are out while "Left Bank
Condominium Association" and "Property Owners Association, Ltd." are in), or
if it is the queried association's core name plus entity/association
suffixes. Everything else is dropped and counted
(`stats.court_parties_dropped`); a party that *does* match an existing
community still attaches. The same test keeps corporate-registry rows with a
business form (developer LLCs, "Homeowners Finance Co.") from becoming
communities, and runs inside the Broward and Hillsborough adapters at
collection time.

## Cross-source matching (deliberately conservative)

The failure mode that matters is attaching one association's liens to
another's report, so matching is:

1. **exact** normalized-name match within the state, else
2. **unique core** match — only when exactly one entity and (for corps)
   exactly one corporation share that core, else
3. **zip disambiguation** — several corps share a core (phase communities):
   attach only if exactly one shares the entity's zip, else
4. **leave unlinked**. Ambiguity never guesses.

County hints constrain lien matching (a Broward lien only core-matches
entities in Broward or with no county).

## Flags ("things to watch out for") and score

| Flag | Trigger | Severity |
| --- | --- | --- |
| Registration delinquent | DBPR `status_detail == Delinquent` | serious |
| Corporation not in good standing | attached corp status matches dissolved/forfeited/delinquent/… | serious |
| No active corporation found | **only** in active-only registries (FL Sunbiz), only when the entity's own name is corporate-style, only with recent activity | serious |
| Claims of lien filed | any liens; scaled by last-5-years count | info→serious |
| Foreclosure filings (lis pendens) | LP/foreclosure docs | warning→serious |
| Liens contested by owners | notices of contest | info/warning |
| High lien rate for size | recent liens ≥ 8% of unit count | serious |
| Federal housing/civil-rights, debt-collection suits | nature-of-suit | serious |
| Insurance case | nature-of-suit | info (usually the association suing its insurer) |
| State appellate case | opinion in caption, or a state appeals-court docket naming the association (`level: appellate`, CourtListener bulk files) | warning |
| Taken to the state supreme court | docket with `level: supreme` (Supreme Court of Ohio); the detail carries the appeal type and the outcome | warning |
| Consumer complaints (SC) | SCDCA complaint rows | info/warning |
| Timeshare/resort context | name pattern + liens | info (contextualizes inflated counts) |
| No adverse records | nothing above fired | good — with an explicit absence-of-coverage caveat |

Score is a ranking heuristic only (browse order), not shown as a number of
record: recent liens + 4×recent foreclosures + case/complaint weights.

Per-county **percentile** (`liens.percentile`) compares recent-5y lien counts
among that county's filers only — Broward and Manhattan are different worlds.

## Coverage honesty rules

- Corp-registry coverage per state is `active_only` / `all` / `partial`
  (`records/corp_coverage.json`). Absence-of-corporation is only ever
  evidence under `active_only`.
- `meta.coverage[state].irs_exempt` marks states where the IRS roster is
  the only association list: the footer says so, and STATES.md puts them
  in their own "IRS tax-exempt roster only" tier rather than "state-level
  data" — most HOAs file Form 1120-H and never appear in the BMF.
- Every report footer states what was NOT checked for that state
  (from `meta.coverage`), with retrieval dates.
- The index/search layer (`site/data/index-XX.txt`) is pipe-delimited:
  `id|name|city|county|type|score|badges` where badges are
  `D` delinquent, `B` corp not in good standing, `C` no-corp,
  `L{n}` liens, `P{n}` foreclosures, `F{n}` cases, `X{n}` complaints,
  `N{n}` news, `OK` clean.
- Full reports live in `site/data/detail/<id[:2]>.json` shards.

## Master tracking

`coverage.json` — per state: `collected` (with counts), `available`
(exists, untapped), `unavailable` (paid/restricted + why), `counties`,
`notes`. `hoaspy.build.make_states_md` renders it (plus `state_sources.yml`,
`gov_layers/`, the trial-court adapters and `site/portals.json`) to `STATES.md`
and one page per state under `states/`; `--check` fails the suite when stale.
