# Contributing to hoaspy

hoaspy collects public records about US homeowner and condominium
associations from government registries, county recorders and courts. The
most useful thing you can add is a source it does not reach yet.

There are four ways to help, easiest first. None of them needs an account
with us, an API key or a configuration file.

| You have | Send | Where |
| --- | --- | --- |
| A government page that lists associations, liens or cases | The URL and what it publishes | [New source](../../issues/new?template=new_source.yml) issue |
| A source you tried that is blocked, paid or forbids scripts | What you tried and what happened | [Source blocked or paid](../../issues/new?template=source_blocked.yml) issue |
| A public ArcGIS, Socrata or CSV layer of associations | A few lines of YAML in `gov_layers/<ST>.yml` | Pull request |
| A source that needs its own code | A collector, a fixture and a test | Pull request |

## Sourcing rules

Every source and every pull request is held to these four rules.

1. **Government or court origin, publicly accessible.** No commercial data
   vendors, no leaked files, and no source whose terms forbid automated
   access. No solving image captchas.
2. **Names and roles as filed; no contact details.** Keep association names,
   officers as filed and management companies. Never store phone numbers or
   e-mail addresses from a filing.
3. **Link every record to the issuing source.** Never re-host documents.
4. **No collected data in this repository.** Outputs (`records/`, `liens/`,
   `courts/`, `news/`, `.cache/`) are git-ignored. Do not commit them or
   attach them to an issue or a pull request: lien and court records name
   private individuals, and everything posted here is public for good.

## Set up

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements-dev.txt                 # the collectors' packages plus ruff
./venv/bin/python -m unittest discover -s tests -t . -v        # the whole suite, no network
./venv/bin/ruff check .                                        # the linter
```

The same two commands run on every pull request (`.github/workflows/tests.yml`,
Python 3.11 and 3.14).

## Add a layer (configuration only)

Many counties and cities publish their association inventory as an ArcGIS
layer, a Socrata dataset or a CSV file. Those need no code: add an entry to
`gov_layers/<ST>.yml`, copying one that is already there.

```yaml
- kind: arcgis                       # arcgis | socrata | csv
  source: "Birmingham AL — official neighborhood associations (city GIS)"
  page: "https://…"                  # the page a person would open
  url: "https://…/MapServer/0"       # the layer itself
  fields: {name: NAME, record_id: NUM}
  constants: {city: Birmingham, county: Jefferson}
```

Run the state before you open the pull request, and paste the last lines of
the output into the description (`--help` documents every key an entry may
carry):

```bash
./venv/bin/python -m hoaspy gov-layers --state AL
```

## Add a collector

Copy the nearest existing module: a trial-court portal is one adapter file in
`hoaspy/collect/courts/court_portals/`; registries are under
`hoaspy/collect/registries/` and recorder indexes under
`hoaspy/collect/liens/`. A collector has:

- **a command**: the module defines `add_arguments(parser)` for its options
  and `run(args) -> int` for the work, and is registered by name in
  `COMMANDS` in `hoaspy/lib/cli.py`, which owns the parser, `-v` and
  logging. A collector has no `main()`, parser or logging setup of its own;
- **records**, not dicts: it parses its source into the record types of
  `hoaspy/lib/records.py` (`Association`, `Lien`, `Case`, `Opinion`,
  `Complaint`) and writes `as_row()` of each. A column only this source has
  goes in `extras`, and the module docstring lists it;
- **paced requests**, a **checkpoint** so a long run resumes, and an
  **atomic write** of its output file — `hoaspy/lib/jsonl.py`,
  `hoaspy/lib/checkpoint.py` and `hoaspy/lib/pacing.py` provide them;
- a **module docstring** that states the source, the access path, the output
  file and the caveats;
- output that **conforms to the record contract** (`hoaspy/lib/contract.py`):
  run `./venv/bin/python -m hoaspy validate <your output file>` and fix
  what it reports. Pass free-text fields a filer typed into through
  `contract.strip_contacts()` — it removes phone numbers and e-mail
  addresses;
- a **fixture** under `tests/fixtures/` — a page or response saved from the
  source — and a **test class** in `tests/test_collectors.py` that parses it.
  Tests never touch the network;
- a paragraph in the matching page of `docs/` (`COURTS.md`, `LIENS.md`,
  `REGISTRIES.md`).

**Fixtures are public.** Before you commit one, replace private individuals'
names and street addresses with placeholders (John Doe, Jane Roe, 123 Sample
St). Association and company names stay.

`docs/STATES.md`, `docs/states/` and `coverage.json` are generated and
published from the HOA Spy site build, so edits to them in a pull request are
overwritten at the next publish. Say in the pull request what the state page
should list instead.

## Code conventions

The code is read by people fetching the data themselves, so it is written
to be read. `ruff check .` enforces the mechanical part; the rest:

- **Names say what a thing is.** A module's constants name the source's
  parts, not their type: the site's root is `PORTAL_URL` (a court portal),
  `REGISTRY_URL`, `RECORDER_URL` or `CATALOG_URL`; `SEARCH_URL` and
  `API_URL` are endpoints under it; `SOURCE_PAGE` is the page a reader opens
  (it becomes the row's `source_url` or `source_page`); `SOURCE` is the
  value of the row's `source` column; `*_PATH` is a file, `*_DIR` a
  directory, `*_RE` a compiled pattern, `PAGE_SIZE` and `ROW_CAP` the
  source's limits, `CSV_COLUMNS` the columns of a CSV. Variables are words
  — `response`, `row`, `match`, `session` — not letters; a loop index and
  a comprehension variable over a one-line expression are the exceptions.
- **Modules share, they do not copy.** Paging an ArcGIS layer, opening a
  session, writing a file atomically, recording coverage, resuming a run
  and driving a browser all come from `hoaspy/lib/` and
  `hoaspy/collect/registries/arcgis.py`. A helper that two modules need
  moves there.
- **Docstrings.** The module docstring tells a public reader what the
  source is, how to run the command, how the source is reached (auth,
  caps, quirks), what is written and what is missed — and nothing about a
  particular machine or a private workflow. Every public function says
  what it returns in one sentence; a dense regular expression gets a named
  constant and a comment with an example of what it matches.
- **Functions parse, commands orchestrate.** Parsing and shaping are
  functions that take text or a dict and return a record, so a fixture can
  exercise them; `run()` only reads options, calls them and writes.
- **Behaviour changes are deliberate.** A change to what a collector
  searches or keeps comes with the fixture or test that shows the new
  behaviour; a rename or a tidy-up comes alone.

## Open the pull request

`main` is protected: work on a branch and open a pull request. The template
asks for four things — the source, the sourcing rules ticked, the output of
a short live run (`--limit 5` or the collector's equivalent), and the test
you added. A pull request is merged when the suite and the linter are green
and a maintainer has read the code and run the collector.

Collectors run on the maintainer's machine after they are merged, so write
them to need nothing but the packages in `requirements.txt`. A new dependency
needs a reason in the pull request.

## Data you collected yourself

If you ran a collector under your own access and want the result used by
[hoaspy.com](https://hoaspy.com), do not post the files here — see rule 4.
Say so on [hoaspy.com/help](https://hoaspy.com/help.html) and we will
arrange a private hand-off.

## Reporting a wrong record

Every finding on hoaspy.com links to its source. If a record is attributed
to the wrong association, or a collector misreads a source, open an issue
with the page address and the source link. Leave private individuals' names
out of the issue text.
