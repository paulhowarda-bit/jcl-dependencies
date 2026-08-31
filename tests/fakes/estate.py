"""A deterministic stand-in for the estate's artifact service (mf-fetch).

The real default client is ``mf_fetch:fetch_artifact`` - an external
library that talks to a mainframe share. Nothing here can reach it, so without a
stand-in the retrieval reports are untestable and the byte-stability ratchet could not
cover them.

Answers from a fixed table, so a run is reproducible on any machine with no network.
The members cover what the JCL closure actually exercises: a cataloged PROC (whose
steps exist in no other file), a control-card member requested as ``DSN(MEMBER)``, an
INCLUDE member used by the shipped examples, a name the estate does not have, and a
name whose REQUEST FAILS - which is not the same fact as absence and must never be
reported as one.
"""

from __future__ import annotations

PAYPROC = (
    "//PAYPROC  PROC\n"
    "//PS1      EXEC PGM=DCIOC104\n"
    "//SYSIN    DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n"
    "//OUT      DD DSN=PROD.PAY.MASTER,DISP=SHR\n"
    "//         PEND\n"
)
SORTCRD = "  SORT FIELDS=(1,8,CH,A)\n"
# The INCLUDE member dailypost.jcl asks for: a step and a DD that exist in no other file.
FINSTD = (
    "//FINSTEP  EXEC PGM=FINPOST\n"
    "//FINDD    DD DSN=PROD.FIN.DAILY,DISP=SHR\n"
)

TABLE = {
    "PAYPROC": PAYPROC,
    "SORTCRD": SORTCRD,
    "FINSTD": FINSTD,
}


class EstateRequestFailed(RuntimeError):
    """The request itself failed - credentials, connectivity, a service fault."""


def fetch_artifact(name, type=None, copy=None):        # noqa: A002 - the wire keyword
    """The mf-fetch calling convention: ``f(name, type=..., copy=...)``."""
    key = str(name).strip().strip("'\"").upper()
    if "(" in key:                                     # PARM.LIB(SORTCRD) -> the member
        key = key.split("(", 1)[1].rstrip(")")
    if key == "BOOM":
        raise EstateRequestFailed("estate share unreachable (simulated)")
    text = TABLE.get(key)
    if text is None:
        return {"artifact_name": key, "found": False}
    return {"artifact_name": key, "found": True, "text": text,
            "detected_type": "proc" if key.endswith("PROC") else "control-card",
            "source_location": f"PROD.PROCLIB({key})"}
