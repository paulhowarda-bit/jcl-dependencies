"""Stage 1 for JCL: the closure over PROCs, INCLUDEs and control cards.

Moved here from the origin repository when this one became the single source. The tests
that matter are the ones that fail *silently* without prefetch - a JCL step that simply
does not appear - because silence is the whole problem: nothing raises, and the output
looks like a finished answer about a simpler job than the one that actually runs.
"""

from jcl_dependencies.parser import parse_jcl
from jcl_dependencies.prefetch import prefetch_jcl
from jcl_dependencies.views import build_jcl_artifacts

from fakes.estate import PAYPROC, SORTCRD, fetch_artifact  # noqa: F401

# A job whose only step EXECs a cataloged PROC. Everything real is inside the PROC.
JOB = (
    "//PAYJOB   JOB (ACCT),'PAYROLL'\n"
    "//STEP1    EXEC PAYPROC\n"
)


def _row(result, member):
    return next(r for r in result.rows if r["member"] == member)


def test_a_jcl_step_inside_a_cataloged_proc_appears_only_after_prefetch():
    """Every real step of PAYJOB lives in PAYPROC; parsed without it the job has no
    programs and no datasets at all."""
    blind = build_jcl_artifacts(parse_jcl(JOB, resolver=None))
    assert not [r for r in blind["artifacts"] if r["kind"] == "program"]

    pre = prefetch_jcl(JOB, fetch_artifact)
    job = parse_jcl(JOB, resolver=pre.resolver())
    seeing = build_jcl_artifacts(job)
    assert "DCIOC104" in {r["artifact"] for r in seeing["artifacts"]
                          if r["kind"] == "program"}
    assert "PROD.PAY.MASTER" in {r["artifact"] for r in seeing["artifacts"]
                                 if r["kind"] == "dataset"}


def test_the_jcl_closure_reaches_a_control_card_named_inside_a_proc():
    """SORTCRD is named by a DD inside PAYPROC - so it cannot be discovered until
    PAYPROC has been retrieved. A single-pass scan of the JCL file finds neither."""
    log = []

    def logging_fetch(name, type=None, copy=None):     # noqa: A002
        log.append(str(name))
        return fetch_artifact(name, type=type, copy=copy)

    pre = prefetch_jcl(JOB, logging_fetch)
    # In that order, necessarily - and by MEMBER name: the engine resolves the
    # DSN(MEMBER) form to the retrievable member before asking the service.
    assert log == ["PAYPROC", "SORTCRD"]
    row = _row(pre, "SORTCRD")
    assert row["status"] == "fetched"
    assert row["dataset"] == "PARM.LIB(SORTCRD)"       # requested as the member within


def test_a_failed_request_is_an_error_row_never_an_absence():
    """The one invariant of the estate contract: raising means THE REQUEST FAILED
    (fixable); returning found:False means the estate was asked and had nothing. A
    client that blurs them makes a whole estate read as empty."""
    job = ("//J JOB (A),'T'\n"
           "//S1 EXEC BOOM\n")
    pre = prefetch_jcl(job, fetch_artifact)
    row = _row(pre, "BOOM")
    assert row["status"] == "error"
    assert "share unreachable" in row["error"]
    assert "NOT evidence the member is absent" in row["reason"]
