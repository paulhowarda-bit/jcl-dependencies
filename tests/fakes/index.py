"""A fixed estate INDEX for the reverse direction, reachable as a --dependents-resolver.

``fakes.estate`` answers "what is this member?"; this answers the other direction, "what
depends on this name?". The CLI's door takes MODULE:FUNC, so exercising it end to end
needs a resolver that can be IMPORTED by name rather than passed in as a callable - which
is the whole point of the flag, and the reason a gathered bundle has to record what it
answered.

Deliberately narrow: it covers one dataset ``acctunld.jcl`` writes and nothing else. A
name it does not cover returns None - NOT ANSWERED - which is what keeps the three answers
distinguishable in a test.
"""

#: Keyed (name, kind) exactly as the lookup asks.
INDEX = {
    ("PROD.ACCT.UNLOAD", "dataset"): [
        {"name": "ACCTLOAD", "kind": "JOB", "via": "DD DSN=... DISP=SHR",
         "match_strength": "qualified", "detail": "step LOAD reads it as INDD"},
    ],
}


def dependents(name, kind=None):
    """What the index says depends on ``name``, or None for a name it does not cover."""
    return INDEX.get((name, kind))
