"""Court records naming associations -> courts/: CourtListener (federal
dockets, appellate opinions, the bulk files), re:SearchTX, the trial-court
portal adapters under court_portals/, and the Miami-Dade civil feed. Every
collector writes `Case` records (hoaspy/lib/records.py); the two
CourtListener collectors also write `Opinion` records."""

from hoaspy import ROOT

COURTS_DIR = ROOT / "courts"
