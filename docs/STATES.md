# State-by-state data coverage

_Generated from `coverage.json` (updated 2026-10-02) by `hoaspy.build.make_states_md` — edit the JSON, not this file. One page per state lives in [`states/`](states/)._

## Coverage at a glance

| Tier | States | What that means |
|---|---|---|
| **County lien index + state data** | FL, IL, NY | recorded liens/foreclosure filings per community, plus registry or corporate status |
| **State-level data** | AK, AL, AR, AZ, CA, CO, CT, DC, DE, GA, HI, IA, ID, IN, KS, KY, LA, MA, MD, MI, MN, MO, MS, MT, NC, ND, NE, NM, NV, OH, OK, OR, PA, SC, TN, TX, UT, VA, VT, WA, WI, WV, WY | HOA registry, corporate registry with status, complaint data, or county/city HOA inventories |
| **IRS tax-exempt roster only** | ME, NH, NJ, RI, SD | associations holding a 501(c) ruling in the IRS EO Business Master File (most HOAs file Form 1120-H and are absent) — no state registry or corporate bulk file |
| **Courts only** |  | federal dockets + state appellate opinions (collected for every state) |

Every state has nationwide court coverage; the tiers describe what exists **beyond** that. Reports on the site state their own coverage limits per community.

## Every jurisdiction

| State | Tier | Collected beyond courts | Lien counties | Trial-court collectors | Config |
| --- | --- | --- | --- | --- | --- |
| [Alaska (AK)](states/AK.md) | State-level data | corporate registry, IRS tax-exempt associations |  | ak_courtview | state_sources.yml |
| [Alabama (AL)](states/AL.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/AL.yml |
| [Arkansas (AR)](states/AR.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/AR.yml |
| [Arizona (AZ)](states/AZ.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  |  |  |
| [California (CA)](states/CA.md) | State-level data | corporate registry, county/city HOA inventories, IRS tax-exempt associations, statewide judgment liens |  |  | gov_layers/CA.yml |
| [Colorado (CO)](states/CO.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Connecticut (CT)](states/CT.md) | State-level data | corporate registry, IRS tax-exempt associations |  | ct_civil | state_sources.yml |
| [District of Columbia (DC)](states/DC.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Delaware (DE)](states/DE.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/DE.yml |
| [Florida (FL)](states/FL.md) | County lien index + state data | HOA/condo registry, corporate registry, IRS tax-exempt associations | Broward, Miami-Dade | fl_broward, fl_hillsborough, fl_miamidade |  |
| [Georgia (GA)](states/GA.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/GA.yml |
| [Hawaii (HI)](states/HI.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  |  |  |
| [Iowa (IA)](states/IA.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Idaho (ID)](states/ID.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Illinois (IL)](states/IL.md) | County lien index + state data | county/city HOA inventories, county condo-building inventory, IRS tax-exempt associations | Cook |  | gov_layers/IL.yml |
| [Indiana (IN)](states/IN.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/IN.yml |
| [Kansas (KS)](states/KS.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/KS.yml |
| [Kentucky (KY)](states/KY.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/KY.yml |
| [Louisiana (LA)](states/LA.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/LA.yml |
| [Massachusetts (MA)](states/MA.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/MA.yml |
| [Maryland (MD)](states/MD.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  | md_casesearch | state_sources.yml |
| [Maine (ME)](states/ME.md) | IRS tax-exempt roster only | IRS tax-exempt associations |  |  |  |
| [Michigan (MI)](states/MI.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/MI.yml |
| [Minnesota (MN)](states/MN.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/MN.yml |
| [Missouri (MO)](states/MO.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/MO.yml |
| [Mississippi (MS)](states/MS.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Montana (MT)](states/MT.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/MT.yml |
| [North Carolina (NC)](states/NC.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/NC.yml |
| [North Dakota (ND)](states/ND.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Nebraska (NE)](states/NE.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/NE.yml |
| [New Hampshire (NH)](states/NH.md) | IRS tax-exempt roster only | IRS tax-exempt associations |  |  |  |
| [New Jersey (NJ)](states/NJ.md) | IRS tax-exempt roster only | IRS tax-exempt associations |  |  |  |
| [New Mexico (NM)](states/NM.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/NM.yml |
| [Nevada (NV)](states/NV.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  |  |  |
| [New York (NY)](states/NY.md) | County lien index + state data | corporate registry, IRS tax-exempt associations | New York City (5 boroughs) |  | state_sources.yml |
| [Ohio (OH)](states/OH.md) | State-level data | corporate registry, IRS tax-exempt associations |  | oh_supreme | state_sources.yml |
| [Oklahoma (OK)](states/OK.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/OK.yml |
| [Oregon (OR)](states/OR.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  | state_sources.yml |
| [Pennsylvania (PA)](states/PA.md) | State-level data | corporate registry, IRS tax-exempt associations |  | pa_ujs | state_sources.yml |
| [Rhode Island (RI)](states/RI.md) | IRS tax-exempt roster only | IRS tax-exempt associations |  |  |  |
| [South Carolina (SC)](states/SC.md) | State-level data | consumer complaints, IRS tax-exempt associations |  |  |  |
| [South Dakota (SD)](states/SD.md) | IRS tax-exempt roster only | IRS tax-exempt associations |  |  | gov_layers/SD.yml |
| [Tennessee (TN)](states/TN.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/TN.yml |
| [Texas (TX)](states/TX.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  | tx_research |  |
| [Utah (UT)](states/UT.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  |  |  |
| [Virginia (VA)](states/VA.md) | State-level data | HOA/condo registry, IRS tax-exempt associations |  | va_gdc | state_sources.yml |
| [Vermont (VT)](states/VT.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/VT.yml |
| [Washington (WA)](states/WA.md) | State-level data | corporate registry, IRS tax-exempt associations |  |  |  |
| [Wisconsin (WI)](states/WI.md) | State-level data | HOA/condo registry, county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/WI.yml |
| [West Virginia (WV)](states/WV.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/WV.yml |
| [Wyoming (WY)](states/WY.md) | State-level data | county/city HOA inventories, IRS tax-exempt associations |  |  | gov_layers/WY.yml |

Each state page lists what was collected (with the collector that produced it), the trial-court situation, where members can look things up, known gaps, the source configuration and the exact commands to collect and rebuild that state.
