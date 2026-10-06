"""County recorder indexes and the California judgment-lien index -> liens/.

Every collector here writes `Lien` records (hoaspy/lib/records.py) in one
vocabulary of document codes, so the site build ingests Broward,
Miami-Dade, NYC, Cook County and California alike:

    LIE   claim of lien          LP    lis pendens          NCL  notice of contest of lien
    LIEX  amended claim of lien  CLP   lis pendens cancelled
    JL    judgment lien the association holds (California)
    JLX / LXA   a lien recorded against the association itself
"""

from hoaspy import ROOT

LIENS_DIR = ROOT / "liens"
