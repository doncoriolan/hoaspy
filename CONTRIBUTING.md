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
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m unittest discover -s tests -t . -v     # the whole suite, no network
```

The same command runs on every pull request (`.github/workflows/tests.yml`,
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
./venv/bin/python -m hoaspy.collect.registries.get_gov_layers --state AL
```

## Add a collector

Copy the nearest existing module: a trial-court portal is one adapter file in
`hoaspy/collect/courts/court_portals/`; registries are under
`hoaspy/collect/registries/` and recorder indexes under
`hoaspy/collect/liens/`. A collector has:

- **paced requests**, a **checkpoint** so a long run resumes, and an
  **atomic write** of its output file;
- a **module docstring** that states the source, the access path, the output
  file and the caveats;
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

## Open the pull request

`main` is protected: work on a branch and open a pull request. The template
asks for four things — the source, the sourcing rules ticked, the output of
a short live run (`--limit 5` or the collector's equivalent), and the test
you added. A pull request is merged when the suite is green and a maintainer
has read the code and run the collector.

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
