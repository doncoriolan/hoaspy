"""Who the associations are: state HOA and condominium registries,
corporate rosters, local inventories, the IRS roster, complaints -> records/.

Several collectors write into the same two merged files, each replacing
only its own state's rows:

    REGISTRIES_PATH    records/state_registries.jsonl   HOA / condominium registries
    CORPORATIONS_PATH  records/state_corps.jsonl        corporate rosters, association-named rows
"""

from hoaspy import ROOT

RECORDS_DIR = ROOT / "records"
REGISTRIES_PATH = RECORDS_DIR / "state_registries.jsonl"
CORPORATIONS_PATH = RECORDS_DIR / "state_corps.jsonl"
