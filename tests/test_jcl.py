"""JCL / PROC parsing, dataflow + control-card field lineage, and the artifact manifest."""

import json
from pathlib import Path

from jcl_dependencies.parser import parse_jcl
from jcl_dependencies.views import build_jcl_artifacts, build_jcl_lineage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _job(name: str, resolver=None):
    return parse_jcl((EXAMPLES / name).read_text(), resolver=resolver, source_name=name)


def _art_by_name(job) -> dict:
    return {a["artifact"]: a for a in build_jcl_artifacts(job)["artifacts"]}


# --------------------------------------------------------------------------- #
# parsing: statements, symbolics, DISP, GDG, concatenation
# --------------------------------------------------------------------------- #

def test_symbolic_and_gdg_resolution():
    job = _job("acctunld.jcl")
    assert job.name == "ACCTUNLD"
    assert job.symbols["HLQ"] == "PROD"
    out = next(dd for s in job.steps for dd in s.dds if dd.ddname == "OUTDD")
    seg = out.segments[0]
    assert seg.dsn == "PROD.ACCT.UNLOAD"      # &HLQ..ACCT.UNLOAD resolved
    assert seg.gdg == "+1"                     # (+1) stripped to the base + generation
    assert seg.disp == ["NEW", "CATLG", "DELETE"]


def test_continuation_lines_are_merged():
    # OUTDD spans three physical lines (DSN,/DISP,/SPACE); all operands must be present.
    job = _job("acctunld.jcl")
    out = next(dd for s in job.steps for dd in s.dds if dd.ddname == "OUTDD")
    assert out.segments[0].disp == ["NEW", "CATLG", "DELETE"]


def test_unresolved_symbolic_is_flagged_not_guessed():
    job = parse_jcl("//J JOB\n//S EXEC PGM=P\n//IN DD DSN=&NOPE..DATA,DISP=SHR\n")
    seg = job.steps[0].dds[0].segments[0]
    assert "&NOPE" in seg.dsn                   # left visible, not blanked
    assert any("NOPE" in f for f in job.flags)


def test_concatenated_dd_is_one_dd_many_segments():
    job = parse_jcl(
        "//J JOB\n//S EXEC PGM=P\n"
        "//IN DD DSN=PROD.A,DISP=SHR\n"
        "//   DD DSN=PROD.B,DISP=SHR\n")
    dd = job.steps[0].dds[0]
    assert dd.ddname == "IN"
    assert [s.dsn for s in dd.segments] == ["PROD.A", "PROD.B"]


# --------------------------------------------------------------------------- #
# PROC expansion, overrides, INCLUDE (with a caller-provided resolver)
# --------------------------------------------------------------------------- #

def test_inline_proc_expands_with_override_symbolic_and_override_dd():
    job = _job("dailypost.jcl")
    step = next(s for s in job.steps if s.from_proc == "POSTPRC")
    assert step.pgm == "DAILYPOST"
    assert step.proc_step == "RUNPOST"
    tranin = next(dd for dd in step.dds if dd.ddname == "TRANIN")
    assert tranin.segments[0].dsn == "PROD.FIN.TRANS"     # &ENV -> PROD (EXEC override)
    audit = next(dd for dd in step.dds if dd.ddname == "AUDIT")
    assert audit.override is True                         # //RUNPOST.AUDIT DD ... applied
    assert audit.segments[0].dsn == "PROD.FIN.AUDIT"


def test_cataloged_proc_resolved_via_provided_function():
    lib = {"MYPROC": "//MYPROC PROC\n//RUN EXEC PGM=EDIT\n"
                     "//IN DD DSN=PROD.IN,DISP=SHR\n//   PEND\n"}
    job = parse_jcl("//J JOB\n//S1 EXEC MYPROC\n", resolver=lambda n: lib.get(n.upper()))
    step = next(s for s in job.steps if s.from_proc == "MYPROC")
    assert step.pgm == "EDIT"
    assert step.proc_resolved is True
    assert not job.flags


def test_unresolved_proc_is_flagged_not_invented():
    job = parse_jcl("//J JOB\n//S1 EXEC NOSUCH\n")     # no resolver
    step = job.steps[0]
    assert step.proc == "NOSUCH" and step.proc_resolved is False
    assert any("NOSUCH" in f for f in job.flags)


def test_include_member_resolved_and_its_dd_attaches_to_the_open_step():
    lib = {"FINSTD": "//STDLIB DD DSN=PROD.FIN.STDCTL,DISP=SHR"}
    job = _job("dailypost.jcl", resolver=lambda n: lib.get(n.upper()))
    binds = build_jcl_lineage(job)["ddBindings"]
    assert any(b["ddname"] == "STDLIB" and b["dataset"] == "PROD.FIN.STDCTL"
               for b in binds)
    assert not job.flags


# --------------------------------------------------------------------------- #
# lineage: dataflow across steps + control-card field lineage
# --------------------------------------------------------------------------- #

def test_dataflow_edge_between_producer_and_consumer_step():
    lin = build_jcl_lineage(_job("acctunld.jcl"))
    edges = {(e["from"], e["to"], e["dataset"]) for e in lin["dataflow"]}
    assert ("STEP01", "STEP02", "PROD.ACCT.UNLOAD") in edges
    # the shared dataset is marked intermediate (produced then consumed within the job)
    inter = next(d for d in lin["datasets"] if d["dsn"] == "PROD.ACCT.UNLOAD")
    assert inter["intermediate"] is True


def test_sort_control_card_gives_byte_field_lineage():
    lin = build_jcl_lineage(_job("acctunld.jcl"))
    fl = next(r for r in lin["fieldLineage"] if r["utility"] == "SORT/DFSORT")
    assert fl["input"] == "PROD.ACCT.UNLOAD" and fl["output"] == "PROD.ACCT.SORTED"
    assert fl["filter"]["kind"] == "INCLUDE"
    # BUILD=(1,5,6,20,28,8) -> three fields; the third copies input 28-35 to output 26-33
    f3 = fl["fields"][2]
    assert f3["from"] == "input" and f3["inBytes"] == "28-35" and f3["outBytes"] == "26-33"


def test_idcams_repro_is_a_copy_edge():
    lin = build_jcl_lineage(_job("copyrepr.jcl"))
    fl = next(r for r in lin["fieldLineage"] if r["utility"] == "IDCAMS")
    assert fl["operations"][0]["op"] == "REPRO"


def test_dd_bindings_resolve_a_cobol_programs_ddname_to_a_dataset():
    """The whole point: STEP01 runs SQLUNLD, whose OUT-FILE is ASSIGNed to ddname OUTDD.
    The COBOL side could only say 'OUTDD, DSN in the JCL'; this supplies the DSN."""
    lin = build_jcl_lineage(_job("acctunld.jcl"))
    b = next(x for x in lin["ddBindings"]
             if x["program"] == "SQLUNLD" and x["ddname"] == "OUTDD")
    assert b["dataset"] == "PROD.ACCT.UNLOAD" and b["io"] == "output"


# --------------------------------------------------------------------------- #
# step conditions: IF/THEN/ELSE and COND=
# --------------------------------------------------------------------------- #

def _steps_by_name(lin):
    return {s["step"]: s for s in lin["steps"]}


def test_if_then_else_gives_each_branch_its_polarity():
    lin = build_jcl_lineage(_job("condflow.jcl"))
    steps = _steps_by_name(lin)
    ok = steps["LOADOK"]["conditions"]["if"]
    fb = steps["FALLBACK"]["conditions"]["if"]
    assert ok == [{"test": "(EXTRACT.RC = 0)", "negated": False}]
    assert fb == [{"test": "(EXTRACT.RC = 0)", "negated": True}]
    # ENDIF closes the scope: CLEANUP carries no IF condition
    assert "if" not in (steps["CLEANUP"].get("conditions") or {})


def test_nested_ifs_conjoin():
    job = parse_jcl(
        "//J JOB\n"
        "// IF (RC = 0) THEN\n"
        "// IF (STEP1.RC = 0) THEN\n"
        "//S2 EXEC PGM=P\n"
        "// ENDIF\n"
        "// ENDIF\n")
    assert [c["expr"] for c in job.steps[0].conditions] == ["(RC = 0)", "(STEP1.RC = 0)"]


def test_cond_back_to_front_sense_is_spelt_out():
    """COND=(4,LT) BYPASSES the step when 4 < a preceding RC - the classic misreading.
    The parsed structure states both directions so a reader cannot take it forwards."""
    lin = build_jcl_lineage(_job("condflow.jcl"))
    cond = _steps_by_name(lin)["REPORT"]["conditions"]["cond"]
    assert cond["sense"] == "bypass-when-true"
    assert cond["tests"] == [{"code": 4, "op": "LT"}]
    assert "unless" in cond["runsWhen"]


def test_cond_even_and_step_scoped_tests():
    lin = build_jcl_lineage(_job("condflow.jcl"))
    assert _steps_by_name(lin)["CLEANUP"]["conditions"]["cond"]["even"] is True
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=A\n"
                    "//S2 EXEC PGM=B,COND=((4,LT),(8,EQ,S1),ONLY)\n")
    cond = job.steps[1].cond_parsed
    assert cond["only"] is True
    assert {"code": 8, "op": "EQ", "step": "S1"} in cond["tests"]


def test_dataflow_edges_carry_the_consumer_condition():
    lin = build_jcl_lineage(_job("condflow.jcl"))
    edge = next(e for e in lin["dataflow"] if e["to"] == "FALLBACK")
    assert edge["conditions"]["consumer"]["if"][0]["negated"] is True


def test_unbalanced_else_and_endif_are_flagged():
    job = parse_jcl("//J JOB\n// ELSE\n//S EXEC PGM=P\n// ENDIF\n")
    assert any("ELSE without" in f for f in job.flags)
    assert any("ENDIF without" in f for f in job.flags)
    job2 = parse_jcl("//J JOB\n// IF (RC = 0) THEN\n//S EXEC PGM=P\n")
    assert any("IF without ENDIF" in f for f in job2.flags)


# --------------------------------------------------------------------------- #
# artifacts manifest (same shape as the COBOL one)
# --------------------------------------------------------------------------- #

def test_artifacts_list_datasets_programs_and_dependency_tags():
    art = _art_by_name(_job("acctunld.jcl"))
    ds = art["PROD.ACCT.UNLOAD"]
    assert ds["kind"] == "dataset" and ds["dependency"] == "runtime"
    assert ds["io"] == "read-write"            # written by STEP01, read by STEP02
    assert ds["identity"] == "global" and ds["resolvedBy"] is None   # DSN is the identity
    assert ds["gdg"] is True
    assert art["SQLUNLD"]["kind"] == "program"
    assert art["SORT"]["kind"] == "program"


def test_proc_and_include_are_compile_time_artifacts():
    art = _art_by_name(_job("dailypost.jcl"))
    assert art["POSTPRC"]["kind"] == "proc"
    assert art["POSTPRC"]["dependency"] == "compile-time"
    assert art["FINSTD"]["kind"] == "include-member"
    assert art["FINSTD"]["dependency"] == "compile-time"


def test_temp_dataset_is_job_scoped_not_global():
    job = parse_jcl(
        "//J JOB\n//S1 EXEC PGM=A\n//OUT DD DSN=&&WORK,DISP=(NEW,PASS)\n"
        "//S2 EXEC PGM=B\n//IN DD DSN=&&WORK,DISP=(OLD,DELETE)\n")
    art = {a["artifact"]: a for a in build_jcl_artifacts(job)["artifacts"]}
    work = art["&&WORK"]
    assert work["identity"] == "job-scoped" and work["temporary"] is True


def test_sysout_and_dummy_are_excluded_with_reason():
    job = parse_jcl(
        "//J JOB\n//S EXEC PGM=P\n//RPT DD SYSOUT=*\n//SCR DD DUMMY\n")
    art = build_jcl_artifacts(job)
    ex = {e["name"]: e for e in art["excluded"]}
    assert ex["RPT"]["kind"] == "spool"
    assert ex["SCR"]["kind"] == "dummy"


# --------------------------------------------------------------------------- #
# the join: bind_cobol_artifacts resolves a COBOL manifest's ddnames via JCL
# --------------------------------------------------------------------------- #

def _sqlunld_manifest():
    """The COBOL artifact manifest bind_cobol_artifacts binds against, as a COMMITTED
    FIXTURE rather than one built live.

    This package must not depend on the COBOL one - they are peers - so the manifest
    arrives as data, which is what it actually is: a schema contract between two
    distributions that release separately. The COBOL side regenerates this file and
    fails if it has drifted (see its test_manifest_contract.py), so the two learn about
    a schema change from a red test rather than from a quietly unbound manifest.
    """
    return json.loads((FIXTURES / "sqlunld.artifacts.json").read_text(encoding="utf-8"))


def test_binding_closes_the_ddname_to_dsn_chain():
    """The join both sides were built for: SQLUNLD's OUT-FILE row said 'ddname OUTDD, DSN
    in the JCL'; ACCTUNLD's STEP01 says OUTDD -> PROD.ACCT.UNLOAD. Bound, the row carries
    the dataset and the ACTUAL DD statement that resolved it."""
    from jcl_dependencies.views import bind_cobol_artifacts
    out = bind_cobol_artifacts(_sqlunld_manifest(), [_job("acctunld.jcl")])
    row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
    assert row["dataset"] == "PROD.ACCT.UNLOAD"
    assert row["resolvedBy"] == "JCL DD statement: ACCTUNLD.STEP01"
    assert "needs" not in row                       # the identity chain is closed
    assert row["boundBy"][0]["generation"] == "+1"
    assert out["jclBinding"]["boundFiles"] == 1


def test_binding_does_not_mutate_the_input_manifest():
    from jcl_dependencies.views import bind_cobol_artifacts
    manifest = _sqlunld_manifest()
    bind_cobol_artifacts(manifest, [_job("acctunld.jcl")])
    row = next(a for a in manifest["artifacts"] if a.get("ddname") == "OUTDD")
    assert "dataset" not in row and "needs" in row


def test_binding_ignores_steps_running_a_different_program():
    from jcl_dependencies.views import bind_cobol_artifacts
    other = parse_jcl("//J JOB\n//S1 EXEC PGM=OTHERPGM\n"
                      "//OUTDD DD DSN=PROD.WRONG.FILE,DISP=(NEW,CATLG)\n",
                      source_name="other.jcl")
    out = bind_cobol_artifacts(_sqlunld_manifest(), [other])
    row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
    assert "dataset" not in row                     # same ddname, wrong program: no join
    assert "needs" in row                           # still honestly unresolved


def test_conflicting_bindings_list_candidates_and_flag_never_collapse():
    """The same program bound to different datasets in different jobs is a FACT (it runs
    against different data), not an error - and picking one silently would be a lie."""
    from jcl_dependencies.views import bind_cobol_artifacts
    job_b = parse_jcl("//OTHERJOB JOB\n//S1 EXEC PGM=SQLUNLD\n"
                      "//OUTDD DD DSN=TEST.ACCT.UNLOAD,DISP=(NEW,CATLG)\n",
                      source_name="other.jcl")
    out = bind_cobol_artifacts(_sqlunld_manifest(), [_job("acctunld.jcl"), job_b])
    row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
    assert "dataset" not in row
    assert row["datasetCandidates"] == ["PROD.ACCT.UNLOAD", "TEST.ACCT.UNLOAD"]
    assert len(row["boundBy"]) == 2
    assert any("2 different datasets" in f for f in out["flags"])


def test_binding_carries_the_steps_run_conditions():
    """A binding made by a conditional step only holds when the step runs - the condition
    from the IF travels with the boundBy entry."""
    from jcl_dependencies.views import bind_cobol_artifacts
    job = parse_jcl("//J JOB\n// IF (PREP.RC = 0) THEN\n//S1 EXEC PGM=SQLUNLD\n"
                    "//OUTDD DD DSN=PROD.ACCT.UNLOAD,DISP=(NEW,CATLG)\n// ENDIF\n",
                    source_name="cond.jcl")
    out = bind_cobol_artifacts(_sqlunld_manifest(), [job])
    row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
    assert row["boundBy"][0]["conditions"]["if"] == [
        {"test": "(PREP.RC = 0)", "negated": False}]


# --------------------------------------------------------------------------- #
# CLI integration: auto-detection and companion output
# --------------------------------------------------------------------------- #


def test_bare_proc_member_is_analysed_with_its_defaults():
    """A .prc that only DEFINES a PROC (never EXECs it) is analysed directly, expanded with
    its own default symbolics, so the member is not empty."""
    job = _job("edvalid.prc")
    assert job.is_proc is True
    step = job.steps[0]
    assert step.from_proc == "EDVALID" and step.pgm == "EDCHECK"
    cardin = next(dd for dd in step.dds if dd.ddname == "CARDIN")
    assert cardin.segments[0].dsn == "TEST.EDIT.CARDS"    # &ENV -> TEST (the default)


# --------------------------------------------------------------------------- #
# instream DLM and PROC-step DD overrides (review finding J9)
# --------------------------------------------------------------------------- #

def test_custom_dlm_delimiter_ends_the_instream_data():
    # DLM='$$' is quoted syntax; the delimiter is $$. Keeping the quotes meant the parser
    # never found the end and the instream ran to EOF, swallowing every later step.
    job = parse_jcl(
        "//J        JOB (A),'T'\n"
        "//STEP01   EXEC PGM=READER\n"
        "//SYSIN    DD DATA,DLM='$$'\n"
        "FIRST DATA LINE\n"
        "//LOOKS LIKE JCL BUT IS DATA\n"
        "$$\n"
        "//STEP02   EXEC PGM=WRITER\n"
        "//OUT      DD DSN=PROD.OUT,DISP=(NEW,CATLG)\n"
    )
    assert [s.name for s in job.steps] == ["STEP01", "STEP02"], "instream swallowed a step"
    sysin = next(dd for s in job.steps for dd in s.dds if dd.ddname == "SYSIN")
    assert sysin.instream_lines == ["FIRST DATA LINE", "//LOOKS LIKE JCL BUT IS DATA"]


_TWICE = {"MYPROC": "//PS   EXEC PGM=UTIL\n//OUT  DD DSN=&&TEMP,DISP=(NEW,PASS)\n"}


def test_proc_dd_override_binds_to_its_own_invocation():
    # the same PROC run twice; each //PS.OUT override applies to the EXEC it follows,
    # not to the first invocation for both (which clobbered one and skipped the other).
    job = parse_jcl(
        "//J     JOB (A),'T'\n"
        "//RUN1  EXEC MYPROC\n"
        "//PS.OUT DD DSN=PROD.FIRST.OUT\n"
        "//RUN2  EXEC MYPROC\n"
        "//PS.OUT DD DSN=PROD.SECOND.OUT\n",
        resolver=_TWICE.get)
    outs = {s.name: next(dd.segments[0].dsn for dd in s.dds if dd.ddname == "OUT")
            for s in job.steps}
    assert outs == {"RUN1.PS": "PROD.FIRST.OUT", "RUN2.PS": "PROD.SECOND.OUT"}


def test_proc_dd_override_merges_and_keeps_the_disp_direction():
    from jcl_dependencies.parser import _dd_direction
    # the override names only DSN, so the PROC DD's DISP (and its output direction) must
    # survive - replacing the whole DD nulled it and the dataflow edge disappeared.
    job = parse_jcl(
        "//J    JOB (A),'T'\n"
        "//RUN  EXEC MYPROC\n"
        "//PS.OUT DD DSN=PROD.REAL.OUT\n",
        resolver=_TWICE.get)
    out = next(dd for s in job.steps for dd in s.dds if dd.ddname == "OUT")
    assert out.segments[0].dsn == "PROD.REAL.OUT"          # override's DSN wins
    assert out.segments[0].disp == ["NEW", "PASS"]         # PROC DD's DISP kept
    assert _dd_direction(out.segments[0]) == "output"      # ...so direction survives


# --------------------------------------------------------------------------- #
# the record-and-replay contract
#
# prefetch_jcl closes over a job's PROCs / INCLUDEs / control cards by REPLAYING the
# parse under a recording resolver until it stops asking for anything new. That works
# only while _Parser._resolve is the SOLE route by which the parser reaches an external
# member. Anything that memoizes resolution inside the parser, short-circuits when the
# resolver returns None, or adds a second resolution path silently SHORTENS the closure -
# no error, just a job that reads as though it had fewer steps than it runs. These tests
# pin the sequence and the round count so such a change is visible rather than merely
# quieter.
# --------------------------------------------------------------------------- #

_NESTED = {
    # job -> PROC -> INCLUDE -> a step whose control card names a DSN(MEMBER)
    "OUTERPRC": ("//OUTERPRC PROC\n"
                 "//         INCLUDE MEMBER=INNERINC\n"
                 "//PS1 EXEC PGM=IEFBR14\n"),
    "INNERINC": ("//PS2 EXEC PGM=SORT\n"
                 "//SYSIN DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n"),
    "SORTCRD": "  SORT FIELDS=(1,8,CH,A)\n",
}

_JOB = "//J JOB (A),'T'\n//RUN EXEC OUTERPRC\n"


def test_every_external_member_is_asked_for_through_the_resolver():
    """One parse must funnel PROC, nested INCLUDE and control-card DSN through the one
    resolver call - that is what makes replaying the parse ask the right questions."""
    asked = []

    def recording(name):
        asked.append(name)
        return _NESTED.get(str(name).upper().strip("'\""))

    parse_jcl(_JOB, resolver=recording)
    # Sequence, not set: the order is the nesting order, and a parser that resolved the
    # INCLUDE before expanding the PROC that contains it would be a different traversal.
    assert asked[0].upper() == "OUTERPRC"
    assert "INNERINC" in [a.upper() for a in asked]
    assert any("SORTCRD" in a.upper() for a in asked)


def test_the_closure_converges_and_costs_one_round_per_level_of_nesting():
    """Three levels of nesting resolve in three retrieval rounds, then the parse stops
    asking. A change to the replay loop shows up here as a different round count."""
    from cobol_xstate_core.prefetch import PrefetchResult
    from jcl_dependencies.prefetch import prefetch_jcl

    rounds = []

    def fetcher(name, type=None, copy=None):        # noqa: A002 - the wire keyword
        key = str(name).upper().strip("'\"")
        rounds.append(key)
        if "(" in key:                              # PARM.LIB(SORTCRD) -> the member
            key = key.split("(", 1)[1].rstrip(")")
        return _NESTED.get(key) or {"found": False}

    result = prefetch_jcl(_JOB, fetcher, source_name="job.jcl")
    got = [r["member"] for r in result.rows if r["status"] == "fetched"]
    assert got == ["OUTERPRC", "INNERINC", "SORTCRD"]   # one per level, in nesting order
    # ...and it converged rather than hitting the bound, so no <closure> row was filed.
    assert not [r for r in result.rows if r["member"] == "<closure>"]


def test_a_closure_deeper_than_the_bound_says_so_instead_of_looking_complete():
    """Silently stopping would look exactly like a job that had no more members."""
    from jcl_dependencies.prefetch import prefetch_jcl

    # Each PROC EXECs the next, forever: the closure can never converge.
    def fetcher(name, type=None, copy=None):        # noqa: A002 - the wire keyword
        key = str(name).upper().strip("'\"")
        return f"//{key[:8]} PROC\n//S EXEC {key}X\n"

    result = prefetch_jcl(_JOB, fetcher, source_name="job.jcl", max_rounds=3)
    closure = [r for r in result.rows if r["member"] == "<closure>"]
    assert len(closure) == 1
    assert closure[0]["status"] == "skipped"
    assert "3 resolution rounds" in closure[0]["reason"]


# --------------------------------------------------------------------------- #
# instream DATA is data, not control cards (audit finding #10)
# --------------------------------------------------------------------------- #

def test_instream_data_on_a_non_card_dd_is_never_classified_as_control():
    """Content-sniffing ran over the instream lines of EVERY DD, so transaction data on
    `//INDATA DD *` containing action words (`DELETE ACCT001 FROM MASTER`) was
    classified as an IDCAMS control card - and the lineage view then published a
    phantom destructive operation. Only the utilities' own card DDs carry syntax."""
    job = parse_jcl(
        "//J JOB (A),'T'\n"
        "//S1 EXEC PGM=MYPROG\n"
        "//INDATA DD *\n"
        "DELETE ACCT001 FROM MASTER PLEASE\n"
        "REPRO INFILE(A) OUTFILE(B)\n"
        "/*\n"
        "//S2 EXEC PGM=IDCAMS\n"
        "//SYSIN DD *\n"
        " DELETE PROD.OLD.FILE\n"
        "/*\n")
    indata = next(dd for s in job.steps for dd in s.dds if dd.ddname == "INDATA")
    assert indata.control is None
    # ...while a REAL control card on SYSIN still classifies.
    sysin = next(dd for s in job.steps for dd in s.dds if dd.ddname == "SYSIN")
    assert sysin.control["utility"] == "IDCAMS"
    assert sysin.control["operations"] == [{"op": "DELETE", "target": "PROD.OLD.FILE"}]
