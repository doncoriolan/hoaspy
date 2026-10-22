"""`python -m hoaspy <command>`: the one entry point to every collector.

    python -m hoaspy                         # the commands, one line each
    python -m hoaspy cook-condos --help      # one command's options
    python -m hoaspy cook-condos --limit 500

Each command is a module with two functions, and nothing else about
running it: `add_arguments(parser)` declares its options on the parser it
is given, and `run(args)` does the work and returns the exit status. This
module owns the rest — the parser, `-v/--verbose`, logging — so no
collector carries its own copy. The module is imported only when its
command runs, so listing the commands costs nothing.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    module: str
    help: str


COMMANDS: dict[str, Command] = {
    # registries: who the associations are -> records/
    "records": Command("hoaspy.collect.registries.get_records",
                       "FL DBPR condominiums and TX TREC certificates -> records/associations.jsonl"),
    "states": Command("hoaspy.collect.registries.get_states",
                      "every state registry and corporate roster in state_sources.yml"),
    "gov-layers": Command("hoaspy.collect.registries.get_gov_layers",
                          "county and city HOA inventories in gov_layers/<ST>.yml"),
    "irs-bmf": Command("hoaspy.collect.registries.get_irs_bmf",
                       "IRS exempt-organization roster, every state"),
    "ut-hoa": Command("hoaspy.collect.registries.get_ut_hoa", "Utah HOA Registry (resumable)"),
    "wi-dfi": Command("hoaspy.collect.registries.get_wi_dfi", "Wisconsin DFI HOA registry (resumable)"),
    "nv-hoa": Command("hoaspy.collect.registries.get_nv_hoa",
                      "Nevada: Clark County, Henderson and Las Vegas GIS layers"),
    "hi-registry": Command("hoaspy.collect.registries.get_hi_registry", "Hawaii AOUO contact list (PDF)"),
    "az-adre": Command("hoaspy.collect.registries.get_az_adre",
                       "Arizona subdivision Public Reports (resumable; needs pdftotext)"),
    "ca-sos": Command("hoaspy.collect.registries.get_ca_sos",
                      "California Secretary of State business search (your browser's cookie and token)"),
    "wa-ccfs": Command("hoaspy.collect.registries.get_wa_ccfs",
                       "Washington CCFS corporations (browser-driven, resumable)"),
    "cook-condos": Command("hoaspy.collect.registries.get_cook_condos", "Cook County IL condominium buildings"),
    "sc-complaints": Command("hoaspy.collect.registries.get_sc_complaints", "South Carolina HOA complaint reports"),
    # liens: county recorder indexes -> liens/
    "liens": Command("hoaspy.collect.liens.get_liens", "Broward County FL official-records index (SFTP)"),
    "miamidade-liens": Command("hoaspy.collect.liens.get_miamidade_liens",
                               "Miami-Dade County FL official records, by association name (cookie)"),
    "daily-liens": Command("hoaspy.collect.liens.get_daily_liens",
                           "yesterday's Broward and Miami-Dade recordings, a file per day (alerts)"),
    "nyc-liens": Command("hoaspy.collect.liens.get_nyc_liens", "NYC ACRIS condo-board liens"),
    "cook-liens": Command("hoaspy.collect.liens.get_cook_liens", "Cook County IL recorder liens (resumable)"),
    "ca-ucc": Command("hoaspy.collect.liens.get_ca_ucc", "California judgment liens from the SOS UCC search"),
    # courts -> courts/
    "courts": Command("hoaspy.collect.courts.get_courts", "CourtListener search: RECAP dockets and state opinions"),
    "courts-bulk": Command("hoaspy.collect.courts.get_courts_bulk",
                           "CourtListener bulk files: every caption naming an association"),
    "state-courts": Command("hoaspy.collect.courts.get_state_courts",
                            "trial-court portal adapters (--list shows them)"),
    "tx-research": Command("hoaspy.collect.courts.get_tx_research", "re:SearchTX, Texas trial courts (your login)"),
    "miamidade-civil": Command("hoaspy.collect.courts.records_miamidade_civil",
                               "Miami-Dade civil court feed staged under .cache/"),
    # news -> news/
    "news": Command("hoaspy.collect.news.get_news", "GDELT news index (retired)"),
    # checks
    "validate": Command("hoaspy.lib.validate", "check collector output against the record contract"),
}

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = ["usage: python -m hoaspy <command> [options]", "",
             "commands (each takes --help):"]
    lines += [f"  {name:<{width}}  {command.help}" for name, command in COMMANDS.items()]
    return "\n".join(lines)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
    logging.getLogger("paramiko").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(usage())
        return 0
    name, options = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"unknown command {name!r}\n\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[name].module)
    parser = argparse.ArgumentParser(prog=f"python -m hoaspy {name}", description=module.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    module.add_arguments(parser)
    args = parser.parse_args(options)
    configure_logging(args.verbose)
    return module.run(args)
