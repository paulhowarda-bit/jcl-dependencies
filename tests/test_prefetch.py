"""Stage 1 for JCL: the closure over PROCs, INCLUDEs and control cards.

Moved here from the origin repository when this one became the single source. The tests
that matter are the ones that fail *silently* without prefetch - a JCL step that simply
does not appear - because silence is the whole problem: nothing raises, and the output
looks like a finished answer about a simpler job than the one that actually runs.
"""

from jcl_dependencies.parser import parse_jcl
from jcl_dependencies.prefetch import prefetch_jcl
from jcl_dependencies.views import build_jcl_artifacts, build_jcl_lineage

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


# --------------------------------------------------------------------------- #
# batch-26 ledger item 62: a member is asked for as what the parser knows it is
# --------------------------------------------------------------------------- #

# One member name, two members: a job and a cataloged PROC that share it. A real estate
# is full of these, and a service asked by the name alone can only pick one of them.
SHARED_JOB = "//SHARED   JOB (A),'THE JOB'\n//JS1      EXEC PGM=JOBPGM\n"
SHARED_PROC = ("//SHARED   PROC\n"
               "//PS1      EXEC PGM=PROCPGM\n"
               "//OUT      DD DSN=PROD.SHARED.OUT,DISP=SHR\n")


def _typed_estate(asked):
    """Answers the JOB to an untyped ask, and the PROC only to `type="proc"`."""
    def fetch(name, type=None, copy=None):             # noqa: A002 - the wire keyword
        asked.append((str(name), type))
        text = SHARED_PROC if type == "proc" else SHARED_JOB
        return {"artifact_name": str(name), "found": True, "text": text,
                "detected_type": type or "jcl", "source_location": f"PROD.LIB({name})"}
    return fetch


def test_a_proc_is_asked_for_as_a_proc_not_as_whatever_shares_its_name():
    """Asked by the name alone, the estate answers with the job - and the job's steps
    then stand in for the PROC's, silently, because something did come back."""
    asked = []
    src = "//J        JOB (A),'T'\n//S1       EXEC SHARED\n"
    pre = prefetch_jcl(src, _typed_estate(asked))
    job = parse_jcl(src, resolver=pre.resolver())
    assert asked == [("SHARED", "proc")]
    assert [s["program"] for s in build_jcl_lineage(job)["steps"]] == ["PROCPGM"]
    assert not [f for f in job.flags if "resolver returned nothing" in f], job.flags


def test_an_include_member_and_a_control_card_are_asked_for_as_cntl():
    asked = []

    def logging_fetch(name, type=None, copy=None):     # noqa: A002
        asked.append((str(name), type))
        return fetch_artifact(name, type=type, copy=copy)

    src = ("//J        JOB (A),'T'\n"
           "//S1       EXEC PGM=SORT\n"
           "//SYSIN    DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n"
           "//         INCLUDE MEMBER=FINSTD\n")
    prefetch_jcl(src, logging_fetch)
    assert asked == [("FINSTD", "cntl"), ("SORTCRD", "cntl")]
