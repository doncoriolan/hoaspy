# hoaspy — public-records collectors for US community associations

The data-extraction code behind [HOA Spy](https://hoaspy.com): collectors
that pull homeowner and condominium associations, recorded liens and
foreclosure filings, court cases and consumer complaints out of government
registries, county recorders, court dockets and agency reports — every
record with a link back to the issuing source.

Government and court sources only. Nothing restricted is scraped, no
documents are re-hosted, and collectors never store phone numbers or e-mail
addresses from a filing (association names, officers as filed and management
companies are kept). See [docs/DATA.md](docs/DATA.md) for the sourcing rules
and the record shapes.

## Layout

```
hoaspy/                      the package (run modules from the repo root: python -m hoaspy.<pkg>.<module>)
  collect/registries/        who the associations are — state registries, corporate rosters, local inventories, IRS roster, complaints
  collect/liens/             county lien / foreclosure-filing indexes (Broward, Miami-Dade, NYC ACRIS, Cook County IL) and CA judgment liens
  collect/courts/            CourtListener (federal + appellate), re:SearchTX, trial-court portal adapters, Miami-Dade civil feed
  collect/news/              GDELT news index (retired 2026-09-07, kept for reference)
  lib/                       shared helpers: request pacing
gov_layers/<ST>.yml          county/city HOA inventories (ArcGIS / Socrata / CSV / shapefile layers) per state
state_sources.yml            state corporate registries and HOA registries for get_states
coverage.json                what is collected / available / restricted per state (the master coverage file)
docs/                        per-collector documentation and the generated per-state coverage pages
tests/                       collector parsing tests against captured fixtures (no network)
```

Outputs are written next to the code and are git-ignored: `records/`
(association rosters, complaints), `liens/`, `courts/`, `news/`, plus
per-collector checkpoints under `.cache/` so long runs resume.

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
sudo apt install poppler-utils        # pdftotext, used by get_az_adre
```

## Run

Every collector is a module with `--help`; most are resumable and safe to
re-run. Examples:

```bash
./venv/bin/python -m hoaspy.collect.registries.get_records               # FL DBPR condos + TX TREC certificates
./venv/bin/python -m hoaspy.collect.registries.get_states                # every state in state_sources.yml
./venv/bin/python -m hoaspy.collect.registries.get_gov_layers            # county/city inventories, 25 states
./venv/bin/python -m hoaspy.collect.registries.get_irs_bmf               # IRS exempt-organizations roster, all states
./venv/bin/python -m hoaspy.collect.registries.get_ut_hoa                # Utah HOA Registry
./venv/bin/python -m hoaspy.collect.registries.get_az_adre --limit 20    # Arizona ADRE Public Reports (smoke test)
./venv/bin/python -m hoaspy.collect.registries.get_cook_condos           # Cook County IL condo buildings
./venv/bin/python -m hoaspy.collect.liens.get_liens                      # Broward County official-records index
./venv/bin/python -m hoaspy.collect.liens.get_nyc_liens                  # NYC ACRIS
./venv/bin/python -m hoaspy.collect.liens.get_cook_liens                 # Cook County IL recorder liens (resumable)
./venv/bin/python -m hoaspy.collect.courts.get_courts                    # CourtListener RECAP + state opinions
./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk               # CourtListener bulk files: every caption naming an association (~7.7 GB)
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --list       # trial-court portal adapters
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal va_gdc --limit 5   # one portal, five names
```

No API key or configuration file is needed. A few sources only answer a
real browser or a session you opened yourself:

- **Browser-driven collectors** (`fl_broward`, `va_gdc`, `get_wa_ccfs`) talk
  to a Chromium-based browser over its DevTools port at `127.0.0.1:9100`;
  they were written against BrowserOS, a Chromium fork. Start
  the browser with remote debugging on that port before running them.
- **Cookie-file collectors** (`get_tx_research` — a free re:SearchTX
  account — `md_casesearch`, `get_miamidade_liens`, `get_ca_sos`,
  `get_ca_ucc`) take a cookie or token
  copied from your own browser session, passed on the command line or in a
  git-ignored `*_cookie.txt` / `*_token.txt` file. Each module's docstring
  says what to copy and from where.

**How a given state is fetched** is on its own page: `docs/states/<ST>.md`
lists what is collected for that state, each collector's source and the
exact command to run it, the state's court portals, and what is known to be
blocked or paid. [docs/STATES.md](docs/STATES.md) is the index.

Each collector documents its source, access path, output file and caveats
in its module docstring and in `docs/`:

| Doc | Covers |
| --- | --- |
| [docs/DATA.md](docs/DATA.md) | Entity model, name-matching rules, the flag catalog, file formats |
| [docs/REGISTRIES.md](docs/REGISTRIES.md) | Registries, corporate rosters, local inventories, IRS roster, complaints |
| [docs/LIENS.md](docs/LIENS.md) | County lien indexes and California judgment liens |
| [docs/COURTS.md](docs/COURTS.md) | CourtListener, re:SearchTX, trial-court portal adapters, Miami-Dade civil feed |
| [docs/NEWS.md](docs/NEWS.md) | GDELT news index (retired) |
| [docs/STATES.md](docs/STATES.md) | Per-state coverage and how each state is fetched, one page per state in `docs/states/` (generated from `coverage.json`, `state_sources.yml`, `gov_layers/` and the trial-court adapters) |

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -t . -v
```

Parsing and row-shaping tests against captured fixtures in `tests/fixtures/`
(`tests/test_collectors.py`) and checks on the repository itself
(`tests/test_repo.py`) — no network. The same command runs on every pull
request. The live collectors are exercised by running them.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) is the guide. The most valuable
contribution is a new source: a county recorder index, a
state registry, a trial-court portal that can be searched by association
name. Open an issue with the URL and what it publishes, or send a pull
request with a collector — the trial-court adapters under
`hoaspy/collect/courts/court_portals/` and the `gov_layers/<ST>.yml` layer
configs are the two patterns to copy. Rules for any source:

1. Government or court origin, publicly accessible, no terms that forbid
   automated access.
2. Keep names and roles as filed; never store phone numbers or e-mail
   addresses.
3. Link every record to the issuing source; never re-host documents.
4. Add a fixture and a test in `tests/test_collectors.py` — with
   individuals' names replaced by placeholders (Doe/Roe) — and describe the
   source in the matching page in `docs/`.
5. Never commit collected data or attach it to an issue or a pull request:
   the records name private individuals.

`docs/STATES.md`, `docs/states/` and `coverage.json` are generated and
published from the HOA Spy site build, so edits to them in a pull request
are overwritten at the next publish; say in the pull request what the state
page should list instead.

`main` is protected: changes land through pull requests. Other ways to help
(data access, donations, looking records up by hand) are on
[hoaspy.com/help](https://hoaspy.com/help.html).

## License

MIT — see [LICENSE](LICENSE).
