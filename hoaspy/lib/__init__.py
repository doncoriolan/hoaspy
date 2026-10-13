"""What the collectors share.

    records.py     the record types, one per family of the contract
    contract.py    the record contract and its checks; validate.py is its command
    jsonl.py       reading and writing the output files, atomically
    coverage.py    coverage.json, what was collected per state
    checkpoint.py  resume state for a name-by-name run
    http.py        the user agents and a session
    names.py       the association-name vocabularies
    states.py      the states by postal code
    pacing.py      jittered request pacing with a global backoff
    cli.py         `python -m hoaspy <command>`
"""
