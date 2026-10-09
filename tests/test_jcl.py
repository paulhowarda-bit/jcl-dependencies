"""JCL / PROC parsing, dataflow + control-card field lineage, and the artifact manifest."""

import json
from pathlib import Path

from jcl_dependencies.parser import parse_jcl, _dd_direction
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


# --------------------------------------------------------------------------- #
# the operand field ends at the first unquoted blank: the card identification field
# (columns 73-80) and inline comments are never operands
# --------------------------------------------------------------------------- #

def _card(stmt: str, ident: str) -> str:
    """A real 80-column card image: the statement, blanks to column 72, then the
    free-form identification field in columns 73-80."""
    card = stmt.ljust(71) + " " + ident
    assert len(card) == 80
    return card


def test_an_identification_field_is_not_part_of_the_program_name():
    job = parse_jcl("//J JOB\n" + _card("//S1 EXEC PGM=WRKUTIL", "00012260") + "\n")
    assert job.steps[0].pgm == "WRKUTIL"


def test_a_statement_without_an_identification_field_is_unchanged():
    """Regression guard: the normal path must be untouched. The same card punched short
    and punched to 80 columns has to name the same program - it did not before."""
    short = parse_jcl("//J JOB\n//S1 EXEC PGM=WRKUTIL\n")
    padded = parse_jcl("//J JOB\n" + _card("//S1 EXEC PGM=WRKUTIL", "00012260") + "\n")
    assert short.steps[0].pgm == padded.steps[0].pgm == "WRKUTIL"


def test_an_identification_field_is_not_part_of_a_dataset_name():
    job = parse_jcl("//J JOB\n//S EXEC PGM=P\n"
                    + _card("//D1 DD DSN=A.B.C", "00001000") + "\n")
    assert job.steps[0].dds[0].segments[0].dsn == "A.B.C"


def test_an_identification_field_does_not_swallow_the_last_keyword():
    """DSN= is only corrupted when it is last on the card; whichever keyword IS last takes
    the absorption. Here that is DISP, and a corrupted DISP loses the direction."""
    job = parse_jcl("//J JOB\n//S EXEC PGM=P\n"
                    + _card("//D1 DD DSN=A.B.C,DISP=SHR", "00001000") + "\n")
    seg = job.steps[0].dds[0].segments[0]
    assert seg.dsn == "A.B.C" and seg.disp == ["SHR"]


def test_a_continuation_is_followed_when_the_card_carries_an_identification_field():
    """The one that fails loudest: the continuation test used to see the identification
    field rather than the operand field, so the comma was invisible and every operand on
    the following cards was lost."""
    job = parse_jcl(
        "//J JOB\n//S EXEC PGM=P\n"
        + _card("//D1 DD DSN=A.B.C,", "00002200") + "\n"
        "//             DISP=(NEW,CATLG,DELETE),\n"
        "//             UNIT=SYSDA\n")
    seg = job.steps[0].dds[0].segments[0]
    assert seg.disp == ["NEW", "CATLG", "DELETE"]
    assert _dd_direction(seg) == "output"


def test_an_inline_comment_does_not_become_an_operand():
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=IEFBR14  RUN THE NULL PROGRAM\n")
    assert job.steps[0].pgm == "IEFBR14"


def test_a_quoted_operand_containing_a_blank_survives():
    """The case a naive split() breaks, and the reason the scan tracks quotes."""
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=X,PARM='A B'\n")
    assert job.steps[0].parm == "'A B'"


def test_a_quoted_literal_split_across_cards_survives():
    """The quote state carries along the continuation chain. A card that resumes a literal
    is operand text throughout, blanks included; scanning it as if it began outside a
    quote would cut 'ALPHA,BETA GAMMA' down to 'ALPHA,BETA."""
    job = parse_jcl("//J JOB\n"
                    "//S1 EXEC PGM=Y,PARM='ALPHA,\n"
                    "//             BETA GAMMA'\n")
    assert job.steps[0].parm == "'ALPHA,BETA GAMMA'"


def test_proc_overrides_on_a_continuation_card_reach_the_expansion():
    """Both cards' overrides must arrive, not just the first card's."""
    job = parse_jcl(
        "//J JOB\n"
        "//P PROC DSN=DEFAULT.DATA,JOBNAME=NONE\n"
        "//RUN EXEC PGM=PAYCALC\n"
        "//IN DD DSN=&DSN,DISP=SHR\n"
        "//    PEND\n"
        + _card("//S1 EXEC P,DSN=X.Y,", "00003000") + "\n"
        "//             JOBNAME=J\n")
    seg = next(dd for s in job.steps for dd in s.dds if dd.ddname == "IN").segments[0]
    assert seg.dsn == "X.Y"                     # the first card's override
    assert job.steps[0].from_proc == "P"


def test_an_if_expression_keeps_its_blanks_and_still_sheds_its_identification_field():
    """IF is the one statement the first-blank rule does not fit: its operand is a
    relational expression that legitimately contains blanks, so it ends at THEN instead.
    Stopping at the first blank would reduce `(PREP.RC = 0)` to `(PREP.RC`."""
    plain = parse_jcl("//J JOB\n// IF (PREP.RC = 0) THEN\n"
                      "//S1 EXEC PGM=P\n// ENDIF\n")
    assert plain.steps[0].conditions == [{"expr": "(PREP.RC = 0)", "negated": False}]
    # ...and ending at THEN sheds the identification field for free, which the trailing
    # `THEN\s*$` strip could not do while the sequence number sat behind it.
    padded = parse_jcl("//J JOB\n" + _card("// IF (PREP.RC = 0) THEN", "00001000") + "\n"
                       "//S1 EXEC PGM=P\n// ENDIF\n")
    assert padded.steps[0].conditions == plain.steps[0].conditions


def test_a_file_of_short_lines_is_unaffected():
    """No truncation happens where there is nothing to truncate."""
    src = ("//J JOB\n//S EXEC PGM=P\n"
           "//IN DD DSN=PROD.A,DISP=SHR\n"
           "//   DD DSN=PROD.B,DISP=SHR\n")
    assert max(len(ln) for ln in src.splitlines()) < 72
    job = parse_jcl(src)
    dd = job.steps[0].dds[0]
    assert [s.dsn for s in dd.segments] == ["PROD.A", "PROD.B"]
    assert all(s.disp == ["SHR"] for s in dd.segments)


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


def test_two_members_of_one_card_library_are_two_artifacts():
    """A control-card row was keyed on the DSN alone, so two steps reading two different
    MEMBERS of one library collapsed into a single row: the second member vanished from
    the manifest entirely, and the first was reported as read by a step that never read
    it (stage 2 then never fetched the second either). For a control card the MEMBER is
    the artifact - the library is only where it lives."""
    job = parse_jcl(
        "//J JOB\n"
        "//STEP01 EXEC PGM=EZTPA00\n"
        "//SYSIN DD DSN=PROD.EZTSRC(MEMBERA),DISP=SHR\n"
        "//STEP02 EXEC PGM=EZTPA00\n"
        "//SYSIN DD DSN=PROD.EZTSRC(MEMBERB),DISP=SHR\n")
    cards = {a["artifact"]: a for a in build_jcl_artifacts(job)["artifacts"]
             if a["kind"] == "control-card"}
    assert sorted(cards) == ["PROD.EZTSRC(MEMBERA)", "PROD.EZTSRC(MEMBERB)"]
    # ...and each row names ONLY the step that actually read it.
    assert [t["step"] for t in cards["PROD.EZTSRC(MEMBERA)"]["touchedBy"]] == ["STEP01"]
    assert [t["step"] for t in cards["PROD.EZTSRC(MEMBERB)"]["touchedBy"]] == ["STEP02"]


def test_one_card_member_read_by_two_steps_stays_one_artifact():
    """The converse of that collapse: keying on (DSN, MEMBER) must not over-split. One
    member read by two steps is ONE artifact that both steps touch."""
    job = parse_jcl(
        "//J JOB\n"
        "//STEP01 EXEC PGM=SORT\n"
        "//SYSIN DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n"
        "//STEP02 EXEC PGM=SORT\n"
        "//SYSIN DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n")
    cards = [a for a in build_jcl_artifacts(job)["artifacts"]
             if a["kind"] == "control-card"]
    assert len(cards) == 1
    assert [t["step"] for t in cards[0]["touchedBy"]] == ["STEP01", "STEP02"]


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
    assert any("not a job" in f for f in job.flags)       # ... and says those DSNs are defaults


# --------------------------------------------------------------------------- #
# a bare PROC member says so: the defaults-only flag and the isProc field
# --------------------------------------------------------------------------- #

def test_bare_proc_member_flags_that_it_is_a_proc_not_a_job():
    """The standalone expansion uses the PROC's own defaults - no invoking job's SET, no EXEC
    overrides - so every DSN it produces is a default a real invocation may replace. Say so:
    an empty job name is an inference from an absence, not an assertion."""
    job = parse_jcl("//PAYPRC PROC ENV=TEST\n"
                    "//RUN EXEC PGM=PAYCALC\n"
                    "//IN  DD DSN=&ENV..PAY.IN,DISP=SHR\n"
                    "//    PEND\n")
    assert job.is_proc is True and job.name == ""
    notice = next(f for f in job.flags if "not a job" in f)
    assert "PAYPRC" in notice                             # the notice names the PROC expanded


def test_both_views_carry_isproc_for_a_bare_proc_member():
    """An empty `job` cannot carry this: an INCLUDE member and a JCL fragment have one too."""
    job = _job("edvalid.prc")
    assert build_jcl_lineage(job)["isProc"] is True
    assert build_jcl_artifacts(job)["isProc"] is True


def test_a_job_that_invokes_a_proc_is_not_flagged_as_a_proc():
    """The defaults-only notice belongs to the standalone member expansion only. A real job
    EXECs the PROC with its own overrides, so neither the flag nor isProc may fire there."""
    lib = {"MYPROC": "//MYPROC PROC ENV=TEST\n//RUN EXEC PGM=EDIT\n"
                     "//IN DD DSN=&ENV..IN,DISP=SHR\n//   PEND\n"}
    job = parse_jcl("//J JOB\n//S1 EXEC MYPROC,ENV=PROD\n",
                    resolver=lambda n: lib.get(n.upper()))
    assert job.is_proc is False
    assert not any("not a job" in f for f in job.flags)   # the PROC path must not inherit it
    assert build_jcl_artifacts(job)["isProc"] is False


def test_an_include_style_fragment_is_not_a_proc():
    """A fragment with DD statements and no PROC card has an empty job name just like a bare
    PROC member does - isProc is what tells the two apart."""
    job = parse_jcl("//STDLIB DD DSN=PROD.FIN.STDCTL,DISP=SHR\n"
                    "//SYSOUT DD SYSOUT=*\n")
    assert job.name == "" and job.is_proc is False
    assert build_jcl_lineage(job)["isProc"] is False


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
    from mainframe_artifacts.prefetch import PrefetchResult
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


# --------------------------------------------------------------------------- #
# Column 72: the continuation signal a trailing-comma test cannot see.
# Upstream ledger batch 10, item 32. No example in the corpus carries a non-blank
# column 72 outside a //* comment, so the byte ratchet can see neither the defect nor
# the fix - these tests are the only coverage. They live here rather than in examples/
# deliberately: a new example would add four golden lines and destroy the
# byte-neutrality claim that is this change's acceptance signal.
# --------------------------------------------------------------------------- #

def _cont_card(stmt: str, indicator: str = "X", ident: str = "") -> str:
    """An 80-column card continued the other way: coded through column 71, with a
    non-blank continuation character in column 72."""
    card = stmt.ljust(71) + indicator + ident.ljust(8)
    assert len(card) == 80 and card[71] == indicator
    return card


def test_an_open_literal_at_column_71_does_not_absorb_the_identification_field():
    """The defect. With no blank outside quotes to stop at, the first-blank rule cannot
    remove columns 73-80, so the scan ran to the end of the physical line and the
    continuation test saw the sequence number instead of the comma."""
    job = parse_jcl("//J JOB\n"
                    + _card("//S1 EXEC PGM=Y,PARM='ALPHA,", "00230000") + "\n"
                    "//             BETA GAMMA'\n")
    assert job.steps[0].parm == "'ALPHA,BETA GAMMA'"


def test_the_absorbed_card_does_not_become_a_phantom_statement():
    """The second consequence: the tail of the literal was matched as a statement in its
    own right, under whatever its first token happened to be."""
    job = parse_jcl("//J JOB\n"
                    + _card("//S1 EXEC PGM=IKJEFT01,PARM='ALPHA,", "00230000") + "\n"
                    + _card("//             BETA GAMMA'", "00240000") + "\n"
                    + _card("//IN      DD DSN=PROD.INPUT.FILE,DISP=SHR", "00250000") + "\n")
    assert [s.name for s in job.steps] == ["S1"]
    assert job.steps[0].pgm == "IKJEFT01"
    assert job.steps[0].dds[0].ddname == "IN"
    assert job.steps[0].dds[0].segments[0].dsn == "PROD.INPUT.FILE"


def test_column_72_continues_a_literal_with_no_trailing_comma_anywhere():
    """JCL splits a quoted value by coding through 71 and putting any character in 72.
    There is no comma for a trailing-comma test to find."""
    job = parse_jcl("//J JOB\n"
                    + _cont_card("//S1 EXEC PGM=Y,PARM='ALPHA") + "\n"
                    "//             BETA'\n")
    assert job.steps[0].parm == "'ALPHABETA'"


def test_a_blank_inside_a_continued_literal_survives():
    """`NON STD` puts a blank INSIDE the literal, so it exercises the quote threading as
    well as the column rule."""
    job = parse_jcl("//J JOB\n"
                    + _cont_card("//S1 EXEC PGM=Y,PARM='VENDOR,NON STD") + "\n"
                    "//             ,120'\n")
    assert job.steps[0].parm == "'VENDOR,NON STD,120'"


def test_a_non_blank_column_72_with_balanced_quotes_does_not_continue():
    """Both signals are required. A misaligned identification field must not swallow the
    statement after it - that is a worse failure than the one being fixed."""
    job = parse_jcl("//J JOB\n"
                    + _cont_card("//S1 EXEC PGM=Y,PARM='ALPHA'") + "\n"
                    "//IN      DD DSN=PROD.INPUT.FILE,DISP=SHR\n")
    assert job.steps[0].parm == "'ALPHA'"
    assert job.steps[0].dds[0].ddname == "IN"
    assert job.steps[0].dds[0].segments[0].dsn == "PROD.INPUT.FILE"


def test_an_apostrophe_in_a_comment_is_not_an_open_literal():
    """A quote-parity count over columns 1-71 reports nearly twice as many cards on a
    real corpus as it should; the whole difference is comment apostrophes. The scan ends at
    the first unquoted blank, so the apostrophe is never reached."""
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=P\n"
                    "//EXCPRPT DD SYSOUT=*  DON'T WRITE TO CLASS 7\n")
    assert job.steps[0].dds[0].segments[0].sysout == "*"


def test_the_instream_data_path_gets_the_same_rule():
    """The continuation loop is shared with `Parser._logical_with_data`. Fixing only
    `_gather` would leave a job carrying instream data parsing differently from one
    that does not."""
    job = parse_jcl("//J JOB\n"
                    + _card("//S1 EXEC PGM=Y,PARM='ALPHA,", "00230000") + "\n"
                    "//             BETA GAMMA'\n"
                    "//SYSIN DD *\n"
                    "  SOME CONTROL CARD\n"
                    "/*\n")
    assert job.steps[0].parm == "'ALPHA,BETA GAMMA'"
    assert job.steps[0].dds[0].ddname == "SYSIN"


def test_a_continuation_that_never_arrives_is_flagged_not_swallowed():
    """A promised continuation that runs off the end of the deck. The statement is
    incomplete and its remaining operands are simply not in the model - silence there
    reads afterwards as a job that did not name them."""
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=P\n//D1 DD DSN=A.B.C,\n")
    assert any("continues past the last card" in f for f in job.flags), job.flags


def test_a_promised_continuation_followed_by_a_non_continuation_is_flagged():
    """The other silent exit: the next card is a statement in its own right, so the
    operands the first card promised are lost."""
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=P\n"
                    "//D1 DD DSN=A.B.C,\n"
                    "//D2 DD DSN=D.E.F,DISP=SHR\n")
    assert any("is not one" in f for f in job.flags), job.flags


def test_the_manifest_conforms_to_the_written_core():
    """mainframe-artifacts now writes the manifest's shared row vocabulary down (upstream
    ledger batch 10, item 31), so this package can check itself against the contract
    rather than against the COBOL package's prose."""
    from mainframe_artifacts.manifest import validate_manifest

    for path in sorted(EXAMPLES.glob("*")):
        job = parse_jcl(path.read_text(), source_name=path.name)
        assert validate_manifest(build_jcl_artifacts(job)) == [], path.name


# --------------------------------------------------------------------------- #
# a PROC step's own COND and the one its invocation applied, kept apart
# --------------------------------------------------------------------------- #

_POSTPROC = {"POSTPROC": "//POSTPROC PROC\n//RUN EXEC PGM=EDIT,COND=(8,LT)\n"
                         "//POST EXEC PGM=POSTX\n"}


def _proc_steps(card: str, lib=None):
    lib = _POSTPROC if lib is None else lib
    job = parse_jcl("//J JOB\n" + card, resolver=lambda n: lib.get(n.upper()))
    return job, {s["step"]: s.get("conditions") or {} for s in build_jcl_lineage(job)["steps"]}


def _raw(conditions: dict, key: str):
    return (conditions.get(key) or {}).get("raw")


def test_a_proc_step_with_its_own_cond_keeps_it_beside_the_invocations():
    _, steps = _proc_steps("//S1 EXEC POSTPROC,COND=(4,LT)\n")
    assert _raw(steps["S1.RUN"], "cond") == "(8,LT)"
    assert _raw(steps["S1.RUN"], "invokedCond") == "(4,LT)"


def test_a_proc_step_with_no_cond_publishes_only_the_invocations():
    """Absent rather than null, so "not coded in the PROC" stays distinguishable."""
    _, steps = _proc_steps("//S1 EXEC POSTPROC,COND=(4,LT)\n")
    assert "cond" not in steps["S1.POST"]
    assert _raw(steps["S1.POST"], "invokedCond") == "(4,LT)"


def test_an_invocation_with_no_cond_leaves_only_the_procs_own():
    _, steps = _proc_steps("//S1 EXEC POSTPROC\n")
    assert _raw(steps["S1.RUN"], "cond") == "(8,LT)"
    assert "invokedCond" not in steps["S1.RUN"]
    assert steps["S1.POST"] == {}


def test_the_invoked_cond_is_parsed_like_any_cond():
    job, _ = _proc_steps("//S1 EXEC POSTPROC,COND=((4,LT),EVEN)\n")
    post = next(s for s in job.steps if s.name == "S1.POST")
    assert post.cond is None
    assert post.invoked_cond_parsed["tests"] == [{"code": 4, "op": "LT"}]
    assert post.invoked_cond_parsed["even"] is True


def test_cond_procstep_applies_to_the_named_step_alone():
    """Read as a symbolic override, `COND.POST=` used to vanish and POST looked
    unconditional."""
    job, steps = _proc_steps("//S1 EXEC POSTPROC,COND.POST=(0,NE)\n")
    assert _raw(steps["S1.POST"], "invokedCond") == "(0,NE)"
    assert "invokedCond" not in steps["S1.RUN"]
    assert job.flags == []


def test_a_step_that_only_its_invocation_conditions_still_counts_as_conditional():
    lib = {"COPYPROC": "//COPYPROC PROC\n//COPY EXEC PGM=IEBGENER\n"
                       "//SYSUT2 DD DSN=PROD.COPY.OUT,DISP=(NEW,CATLG,DELETE)\n"}
    job = parse_jcl("//J JOB\n//S1 EXEC COPYPROC,COND=(4,LT)\n",
                    resolver=lambda n: lib.get(n.upper()))
    art = _art_by_name(job)["PROD.COPY.OUT"]
    assert all(t.get("conditional") for t in art["touchedBy"])


def test_cond_procstep_naming_no_step_of_the_proc_is_flagged_not_dropped():
    job, steps = _proc_steps("//S1 EXEC POSTPROC,COND.NOPE=(0,NE)\n")
    assert all("invokedCond" not in c for c in steps.values())
    assert any("COND.NOPE= names no step of PROC POSTPROC" in f for f in job.flags)


def test_both_forms_coded_together_are_applied_per_step_and_flagged():
    job, steps = _proc_steps("//S1 EXEC POSTPROC,COND=(4,LT),COND.RUN=(2,LT)\n")
    assert _raw(steps["S1.RUN"], "invokedCond") == "(2,LT)"
    assert _raw(steps["S1.POST"], "invokedCond") == "(4,LT)"
    assert any("both COND= and COND.procstep= are coded" in f for f in job.flags)


def test_a_nested_proc_step_takes_the_cond_aimed_at_the_step_that_called_its_proc():
    lib = dict(_POSTPROC, OUTER="//OUTER PROC\n//S1 EXEC POSTPROC\n//S2 EXEC PGM=TAIL\n")
    _, steps = _proc_steps("//J1 EXEC OUTER,COND.S1=(12,LT)\n", lib)
    assert _raw(steps["J1.S1.RUN"], "invokedCond") == "(12,LT)"
    assert _raw(steps["J1.S1.POST"], "invokedCond") == "(12,LT)"
    assert "invokedCond" not in steps["J1.S2"]


def test_the_outer_invocation_overrides_an_inner_one():
    lib = dict(_POSTPROC, OUTER="//OUTER PROC\n//S1 EXEC POSTPROC,COND=(4,LT)\n")
    _, steps = _proc_steps("//J1 EXEC OUTER,COND=(9,LT)\n", lib)
    assert _raw(steps["J1.S1.POST"], "invokedCond") == "(9,LT)"
    _, steps = _proc_steps("//J1 EXEC OUTER\n", lib)
    assert _raw(steps["J1.S1.POST"], "invokedCond") == "(4,LT)"


def test_the_example_keeps_both_conditions_apart():
    steps = _steps_by_name(build_jcl_lineage(_job("proccond.jcl")))
    assert _raw(steps["NIGHTLY.POSTRUN"]["conditions"], "cond") == "(8,LT)"
    assert _raw(steps["NIGHTLY.POSTRUN"]["conditions"], "invokedCond") == "(4,LT)"
    assert _raw(steps["NIGHTLY.AUDIT"]["conditions"], "invokedCond") == "(4,LT)"
    assert "invokedCond" not in steps["WEEKLY.POSTRUN"]["conditions"]
    assert _raw(steps["WEEKLY.AUDIT"]["conditions"], "invokedCond") == "(0,NE)"


# --------------------------------------------------------------------------- #
# a symbol's value coded in apostrophes: they delimit the value, they are not in it
# --------------------------------------------------------------------------- #

def _bindings(text: str, resolver=None):
    job = parse_jcl(text, resolver=resolver)
    return job, {(b["step"], b["ddname"]): b for b in build_jcl_lineage(job)["ddBindings"]}


_QPROC = ("//P PROC HLQ='PROD'\n"
          "//S EXEC PGM=X\n"
          "//DD1 DD DSN=&HLQ..A.B,DISP=SHR\n")


def test_a_quoted_proc_default_is_substituted_without_its_apostrophes():
    """`'PROD'.A.B` is a name no catalog holds."""
    _, b = _bindings("//J JOB\n" + _QPROC + "// PEND\n//R EXEC P\n")
    assert b[("R.S", "DD1")]["dataset"] == "PROD.A.B"


def test_a_cataloged_procs_quoted_default_is_unquoted_too():
    _, b = _bindings("//J JOB\n//R EXEC P\n", resolver={"P": _QPROC}.get)
    assert b[("R.S", "DD1")]["dataset"] == "PROD.A.B"


def test_a_bare_proc_members_quoted_default_is_unquoted_too():
    _, b = _bindings(_QPROC)
    assert b[("P.S", "DD1")]["dataset"] == "PROD.A.B"


def test_a_quoted_exec_override_beats_the_proc_default_and_is_unquoted():
    _, b = _bindings("//J JOB\n" + _QPROC + "// PEND\n//R EXEC P,HLQ='TEST'\n")
    assert b[("R.S", "DD1")]["dataset"] == "TEST.A.B"


def test_a_quoted_generation_is_still_split_from_the_dataset():
    """The apostrophes hid `(+1)` from the pattern that splits it off, so the generation
    stayed in the name."""
    _, b = _bindings("//J JOB\n// SET GEN='+1'\n//S EXEC PGM=X\n"
                     "//DD1 DD DSN=PROD.A.GDG(&GEN),DISP=SHR\n")
    assert b[("S", "DD1")]["dataset"] == "PROD.A.GDG"
    assert b[("S", "DD1")]["generation"] == "+1"


def test_a_quoted_member_is_still_split_from_the_dataset():
    _, b = _bindings("//J JOB\n// SET MEM='CARD1'\n//S EXEC PGM=X\n"
                     "//DD1 DD DSN=PROD.CNTL(&MEM),DISP=SHR\n")
    assert b[("S", "DD1")]["dataset"] == "PROD.CNTL"
    assert b[("S", "DD1")]["member"] == "CARD1"


def test_a_quoted_set_in_an_include_member_is_unquoted_too():
    _, b = _bindings("//J JOB\n// INCLUDE MEMBER=SETS\n//S EXEC PGM=X\n"
                     "//DD1 DD DSN=&HLQ..A.B,DISP=SHR\n",
                     resolver={"SETS": "// SET HLQ='PROD'\n"}.get)
    assert b[("S", "DD1")]["dataset"] == "PROD.A.B"


def test_a_quoted_temporary_name_is_still_a_temporary_dataset():
    job, b = _bindings("//J JOB\n// SET WORK='&&SCRATCH'\n//S EXEC PGM=X\n"
                       "//DD1 DD DSN=&WORK,DISP=(NEW,PASS)\n")
    assert b[("S", "DD1")]["dataset"] == "&&SCRATCH"
    assert _art_by_name(job)["&&SCRATCH"]["temporary"] is True


def test_a_quoted_parm_still_reaches_the_step_as_coded():
    """The negative: PARM is read from the same operand map and is not a symbol."""
    job = parse_jcl("//J JOB\n" + _QPROC + "// PEND\n//S1 EXEC PGM=X,PARM='A,B'\n")
    assert job.steps[0].parm == "'A,B'"


def test_symbol_value_strips_one_enclosing_pair_and_collapses_a_doubled_apostrophe():
    from jcl_dependencies.parser import _symbol_value
    assert _symbol_value("'PROD'") == "PROD"
    assert _symbol_value("'O''NEIL'") == "O'NEIL"
    assert _symbol_value("''") == ""                 # a nullified symbol
    assert _symbol_value("PROD") == "PROD"           # unquoted: untouched
    assert _symbol_value("(A,'B')") == "(A,'B')"     # not ENCLOSED in apostrophes


# --------------------------------------------------------------------------- #
# a backward reference (DSN=*.stepname.ddname) is a pointer, not a dataset name
# --------------------------------------------------------------------------- #

def _edges(job) -> set:
    return {(e["from"], e["to"], e["dataset"]) for e in build_jcl_lineage(job)["dataflow"]}


def test_a_referback_binds_the_dataset_the_earlier_dd_names():
    """Published as coded, the two steps looked like they touched two datasets and the
    edge between them was never drawn."""
    job, b = _bindings("//J JOB\n"
                       "//STEP010 EXEC PGM=SORT\n"
                       "//SORTOUT DD DSN=A.B,DISP=(NEW,PASS)\n"
                       "//STEP015 EXEC PGM=X\n"
                       "//IN DD DSN=*.STEP010.SORTOUT,DISP=(OLD,DELETE)\n")
    assert b[("STEP015", "IN")]["dataset"] == "A.B"
    assert ("STEP010", "STEP015", "A.B") in _edges(job)
    assert not any(d.startswith("*.") for d in _art_by_name(job))
    assert job.flags == []


def test_a_one_part_referback_names_a_dd_of_its_own_step():
    _, b = _bindings("//J JOB\n//S EXEC PGM=X\n"
                     "//SYSUT1 DD DSN=A.B,DISP=SHR\n"
                     "//SYSUT2 DD DSN=*.SYSUT1,DISP=SHR\n")
    assert b[("S", "SYSUT2")]["dataset"] == "A.B"


_RPROC = ("//P PROC\n"
          "//STEP1 EXEC PGM=X\n"
          "//OUT DD DSN=&HLQ..WORK,DISP=(NEW,PASS)\n"
          "//STEP2 EXEC PGM=Y\n"
          "//IN DD DSN=*.STEP1.OUT,DISP=(OLD,PASS)\n")


def test_a_three_part_referback_reaches_into_an_execed_procs_step():
    job, b = _bindings("//J JOB\n//RUN EXEC P,HLQ=PROD\n"
                       "//S2 EXEC PGM=Z\n"
                       "//IN DD DSN=*.RUN.STEP1.OUT,DISP=(OLD,DELETE)\n",
                       resolver={"P": _RPROC}.get)
    assert b[("S2", "IN")]["dataset"] == "PROD.WORK"
    assert ("RUN.STEP1", "S2", "PROD.WORK") in _edges(job)


def test_a_proc_body_refers_back_to_its_own_step_by_the_procs_step_name():
    """Inside a PROC a sibling step is named as the PROC names it, while the expanded
    step is `invocation.procstep` - matched against the expanded name alone, every
    referback written in a PROC body stayed a pointer."""
    job, b = _bindings("//J JOB\n//RUN EXEC P,HLQ=PROD\n", resolver={"P": _RPROC}.get)
    assert b[("RUN.STEP2", "IN")]["dataset"] == "PROD.WORK"
    assert ("RUN.STEP1", "RUN.STEP2", "PROD.WORK") in _edges(job)
    assert job.flags == []


def test_a_bare_proc_members_own_referback_is_resolved():
    _, b = _bindings(_RPROC.replace("&HLQ.", "PROD"))
    assert b[("P.STEP2", "IN")]["dataset"] == "PROD.WORK"


def test_a_proc_run_twice_resolves_each_referback_within_its_own_invocation():
    _, b = _bindings("//J JOB\n//RUN1 EXEC P,HLQ=PROD\n//RUN2 EXEC P,HLQ=TEST\n",
                     resolver={"P": _RPROC}.get)
    assert b[("RUN1.STEP2", "IN")]["dataset"] == "PROD.WORK"
    assert b[("RUN2.STEP2", "IN")]["dataset"] == "TEST.WORK"


def test_a_referback_on_a_proc_dd_override_is_read_in_the_jobs_scope():
    _, b = _bindings("//J JOB\n//S0 EXEC PGM=W\n"
                     "//OUT DD DSN=JOB.LEVEL,DISP=(NEW,PASS)\n"
                     "//RUN EXEC P,HLQ=PROD\n"
                     "//STEP2.IN DD DSN=*.S0.OUT\n",
                     resolver={"P": _RPROC}.get)
    assert b[("RUN.STEP2", "IN")]["dataset"] == "JOB.LEVEL"


def test_a_referback_carries_the_generation_and_the_member():
    _, b = _bindings("//J JOB\n//S1 EXEC PGM=X\n"
                     "//OUT DD DSN=PROD.A.GDG(+1),DISP=(NEW,CATLG)\n"
                     "//LIB DD DSN=PROD.CNTL(CARD1),DISP=SHR\n"
                     "//S2 EXEC PGM=Y\n"
                     "//IN DD DSN=*.S1.OUT,DISP=SHR\n"
                     "//CARDS DD DSN=*.S1.LIB,DISP=SHR\n")
    assert (b[("S2", "IN")]["dataset"], b[("S2", "IN")]["generation"]) == ("PROD.A.GDG", "+1")
    assert (b[("S2", "CARDS")]["dataset"], b[("S2", "CARDS")]["member"]) == ("PROD.CNTL",
                                                                              "CARD1")


def test_a_referback_to_a_referback_resolves_through_it():
    _, b = _bindings("//J JOB\n//S1 EXEC PGM=X\n//OUT DD DSN=A.B,DISP=(NEW,PASS)\n"
                     "//S2 EXEC PGM=Y\n//MID DD DSN=*.S1.OUT,DISP=(OLD,PASS)\n"
                     "//S3 EXEC PGM=Z\n//IN DD DSN=*.S2.MID,DISP=(OLD,DELETE)\n")
    assert b[("S3", "IN")]["dataset"] == "A.B"


def test_a_control_card_dataset_named_by_referback_is_still_read():
    lib = {"PARM.LIB(SORTCRD)": "  SORT FIELDS=(1,8,CH,A)\n"}
    job = parse_jcl("//J JOB\n//S1 EXEC PGM=IEFBR14\n"
                    "//CARDS DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n"
                    "//S2 EXEC PGM=SORT\n"
                    "//SYSIN DD DSN=*.S1.CARDS,DISP=SHR\n", resolver=lib.get)
    sysin = next(dd for s in job.steps for dd in s.dds if dd.ddname == "SYSIN")
    assert sysin.control and sysin.control["sortFields"] == "1,8,CH,A"


def test_a_referback_to_an_unknown_step_stays_as_written_and_is_flagged():
    job, b = _bindings("//J JOB\n//S EXEC PGM=X\n//IN DD DSN=*.NOSTEP.X,DISP=SHR\n")
    assert b[("S", "IN")]["dataset"] == "*.NOSTEP.X"
    assert any("*.NOSTEP.X" in f for f in job.flags)


def test_a_referback_to_a_later_dd_is_not_resolved():
    """It is a BACKWARD reference: the DD it names has to come first."""
    job, b = _bindings("//J JOB\n//S1 EXEC PGM=X\n//IN DD DSN=*.S2.OUT,DISP=SHR\n"
                       "//S2 EXEC PGM=Y\n//OUT DD DSN=A.B,DISP=(NEW,PASS)\n")
    assert b[("S1", "IN")]["dataset"] == "*.S2.OUT"
    assert any("*.S2.OUT" in f for f in job.flags)


def test_a_referback_to_a_dd_with_no_dataset_is_flagged_not_guessed():
    job, b = _bindings("//J JOB\n//S1 EXEC PGM=X\n//RPT DD SYSOUT=*\n"
                       "//S2 EXEC PGM=Y\n//IN DD DSN=*.S1.RPT,DISP=SHR\n")
    assert b[("S2", "IN")]["dataset"] == "*.S1.RPT"
    assert any("*.S1.RPT" in f for f in job.flags)


def test_the_example_resolves_its_quoted_symbols_and_its_referbacks():
    job = _job("refback.jcl")
    b = {(x["step"], x["ddname"]): x for x in build_jcl_lineage(job)["ddBindings"]}
    assert b[("NIGHTLY.SORT", "SORTIN")]["dataset"] == "PROD.SALES.DAILY"
    assert b[("NIGHTLY.REPORT", "INFILE")]["dataset"] == "PROD.SALES.SORTED"
    assert b[("ARCHIVE", "SYSUT1")]["dataset"] == "PROD.SALES.SORTED"
    assert (b[("ARCHIVE", "SYSUT2")]["dataset"],
            b[("ARCHIVE", "SYSUT2")]["generation"]) == ("PROD.SALES.ARCHIVE", "+1")
    assert {("NIGHTLY.SORT", "NIGHTLY.REPORT", "PROD.SALES.SORTED"),
            ("NIGHTLY.SORT", "ARCHIVE", "PROD.SALES.SORTED")} <= _edges(job)
    assert job.flags == []


# --------------------------------------------------------------------------- #
# concatenation: every dataset of a concatenated DD is published
# --------------------------------------------------------------------------- #

_CONCAT = ("//CONCAT JOB (ACCT),CLASS=A\n"
           "//STEP010 EXEC PGM=SORT\n"
           "//SORTIN  DD DSN=A.B.IN1,DISP=SHR\n"
           "//        DD DSN=A.B.IN2,DISP=SHR\n"
           "//        DD DSN=A.B.IN3,DISP=SHR\n"
           "//SORTOUT DD DSN=A.B.OUT,DISP=(NEW,CATLG)\n")

_CONCAT_PROC = ("//SORTPRC PROC\n"
                "//PS1     EXEC PGM=SORT\n"
                "//SORTIN  DD DSN=P.IN1,DISP=SHR\n"
                "//        DD DSN=P.IN2,DISP=SHR\n"
                "//        DD DSN=P.IN3,DISP=SHR\n"
                "//SORTOUT DD DSN=P.OUT,DISP=(NEW,CATLG)\n"
                "//        PEND\n")


def _rows(text: str, resolver=None):
    """(job, lineage, [(step, ddname, concatIndex, dataset, io)] in published order)."""
    job = parse_jcl(text, resolver=resolver)
    lin = build_jcl_lineage(job)
    return job, lin, [(b["step"], b["ddname"], b.get("concatIndex"), b["dataset"], b["io"])
                      for b in lin["ddBindings"]]


def test_every_dataset_of_a_concatenated_dd_is_bound_in_order():
    """Only the first was published: the step read as though it read one file, and the
    other datasets were in no view at all."""
    job, lin, rows = _rows(_CONCAT)
    assert rows == [("STEP010", "SORTIN", 1, "A.B.IN1", "input"),
                    ("STEP010", "SORTIN", 2, "A.B.IN2", "input"),
                    ("STEP010", "SORTIN", 3, "A.B.IN3", "input"),
                    ("STEP010", "SORTOUT", None, "A.B.OUT", "output")]
    assert [d["dsn"] for d in lin["datasets"]] == ["A.B.IN1", "A.B.IN2", "A.B.IN3",
                                                   "A.B.OUT"]
    third = next(d for d in lin["datasets"] if d["dsn"] == "A.B.IN3")
    assert third["consumedBy"] == [{"step": "STEP010", "ddname": "SORTIN",
                                    "concatIndex": 3, "disp": "SHR"}]
    assert [(d["ddname"], d["concatIndex"], d["dataset"])
            for d in lin["steps"][0]["inputs"]] == [("SORTIN", 1, "A.B.IN1"),
                                                    ("SORTIN", 2, "A.B.IN2"),
                                                    ("SORTIN", 3, "A.B.IN3")]
    arts = _art_by_name(job)
    assert [arts[n]["io"] for n in ("A.B.IN1", "A.B.IN2", "A.B.IN3")] == ["read"] * 3
    assert arts["A.B.IN2"]["touchedBy"] == [{"step": "STEP010", "ddname": "SORTIN",
                                             "concatIndex": 2, "disp": "SHR"}]
    assert job.flags == []


def test_a_dd_of_one_statement_carries_no_concat_index():
    """The position is said only where there is one to say, so a job with no
    concatenation publishes what it always did."""
    job, lin, _ = _rows(_CONCAT)
    assert "concatIndex" not in next(b for b in lin["ddBindings"]
                                     if b["ddname"] == "SORTOUT")
    assert "concatIndex" not in lin["steps"][0]["outputs"][0]
    out = next(d for d in lin["datasets"] if d["dsn"] == "A.B.OUT")
    assert out["producedBy"] == [{"step": "STEP010", "ddname": "SORTOUT", "disp": "NEW"}]
    assert _art_by_name(job)["A.B.OUT"]["touchedBy"] == [
        {"step": "STEP010", "ddname": "SORTOUT", "disp": "NEW"}]


def test_a_concatenation_inside_a_proc_is_published_the_same():
    want = [("STEP1.PS1", "SORTIN", 1, "P.IN1", "input"),
            ("STEP1.PS1", "SORTIN", 2, "P.IN2", "input"),
            ("STEP1.PS1", "SORTIN", 3, "P.IN3", "input"),
            ("STEP1.PS1", "SORTOUT", None, "P.OUT", "output")]
    assert _rows("//J JOB\n" + _CONCAT_PROC + "//STEP1 EXEC SORTPRC\n")[2] == want
    cataloged = {"SORTPRC": _CONCAT_PROC}
    assert _rows("//J JOB\n//STEP1 EXEC SORTPRC\n",
                 resolver=lambda n: cataloged.get(n.upper()))[2] == want


def test_a_named_dd_after_a_concatenation_starts_a_new_ddname():
    _, _, rows = _rows("//J JOB\n//S EXEC PGM=P\n"
                       "//IN  DD DSN=A.IN1,DISP=SHR\n"
                       "//    DD DSN=A.IN2,DISP=SHR\n"
                       "//IN2 DD DSN=A.IN3,DISP=SHR\n")
    assert rows == [("S", "IN", 1, "A.IN1", "input"), ("S", "IN", 2, "A.IN2", "input"),
                    ("S", "IN2", None, "A.IN3", "input")]


def test_a_comment_between_concatenated_statements_does_not_end_it():
    _, _, rows = _rows("//J JOB\n//S EXEC PGM=P\n"
                       "//IN  DD DSN=A.IN1,DISP=SHR\n"
                       "//* the second day\n"
                       "//    DD DSN=A.IN2,DISP=SHR\n")
    assert rows == [("S", "IN", 1, "A.IN1", "input"), ("S", "IN", 2, "A.IN2", "input")]


def test_a_comma_continued_dd_is_one_statement_not_a_concatenation():
    _, _, rows = _rows("//J JOB\n//S EXEC PGM=P\n"
                       "//OUT DD DSN=A.OUT,\n"
                       "//       DISP=(NEW,CATLG),\n"
                       "//       UNIT=SYSDA\n")
    assert rows == [("S", "OUT", None, "A.OUT", "output")]


def test_a_proc_override_replaces_the_first_dataset_and_keeps_the_rest():
    """One overriding DD statement overrides the first DD of the PROC's concatenation.
    It used to replace the whole DD, which dropped every dataset after the first."""
    _, _, rows = _rows("//J JOB\n" + _CONCAT_PROC + "//STEP1 EXEC SORTPRC\n"
                       "//PS1.SORTIN DD DSN=O.IN1\n")
    assert rows[:3] == [("STEP1.PS1", "SORTIN", 1, "O.IN1", "input"),
                        ("STEP1.PS1", "SORTIN", 2, "P.IN2", "input"),
                        ("STEP1.PS1", "SORTIN", 3, "P.IN3", "input")]


def test_unnamed_dds_after_an_override_address_the_proc_concatenation_in_order():
    """A DD with nothing on it leaves that position as the PROC coded it; the next one
    overrides the second. They used to be concatenated to the last DD of the step -
    here SORTOUT, a different ddname altogether."""
    _, _, rows = _rows("//J JOB\n" + _CONCAT_PROC + "//STEP1 EXEC SORTPRC\n"
                       "//PS1.SORTIN DD\n"
                       "//           DD DSN=O.IN2\n")
    assert rows == [("STEP1.PS1", "SORTIN", 1, "P.IN1", "input"),
                    ("STEP1.PS1", "SORTIN", 2, "O.IN2", "input"),
                    ("STEP1.PS1", "SORTIN", 3, "P.IN3", "input"),
                    ("STEP1.PS1", "SORTOUT", None, "P.OUT", "output")]


def test_unnamed_dds_past_the_end_of_the_proc_concatenation_add_to_it():
    _, _, rows = _rows("//J JOB\n" + _CONCAT_PROC + "//STEP1 EXEC SORTPRC\n"
                       "//PS1.SORTIN DD\n//  DD\n//  DD\n"
                       "//           DD DSN=O.IN4,DISP=SHR\n")
    assert [r[2:4] for r in rows if r[1] == "SORTIN"] == [
        (1, "P.IN1"), (2, "P.IN2"), (3, "P.IN3"), (4, "O.IN4")]


def test_an_override_naming_no_proc_step_takes_its_continuation_with_it():
    """The override is flagged as not applied. What is concatenated to it is not applied
    either - not handed to whichever DD happened to come last."""
    job, _, rows = _rows("//J JOB\n" + _CONCAT_PROC + "//STEP1 EXEC SORTPRC\n"
                         "//NOSTEP.SORTIN DD DSN=O.IN1,DISP=SHR\n"
                         "//              DD DSN=O.IN2,DISP=SHR\n")
    assert [r[3] for r in rows] == ["P.IN1", "P.IN2", "P.IN3", "P.OUT"]
    assert any("NOSTEP.SORTIN" in f for f in job.flags)


def test_instream_data_after_an_override_belongs_to_the_overridden_dd():
    proc = ("//TWO PROC\n//PS1 EXEC PGM=SORT\n//SYSIN DD DUMMY\n"
            "//PS2 EXEC PGM=IEFBR14\n//LAST DD DSN=P.LAST,DISP=SHR\n// PEND\n")
    job = parse_jcl("//J JOB\n" + proc + "//STEP1 EXEC TWO\n"
                    "//PS1.SYSIN DD *\n  SORT FIELDS=COPY\n/*\n")
    by = {(s.name, d.ddname): d for s in job.steps for d in s.dds}
    assert by[("STEP1.PS1", "SYSIN")].instream_lines == ["  SORT FIELDS=COPY"]
    assert by[("STEP1.PS2", "LAST")].instream_lines == []


_TWO_STEP_PROC = ("//TWO PROC\n"
                  "//PS1 EXEC PGM=A\n"
                  "//X   DD DSN=P.X,DISP=SHR\n"
                  "//PS2 EXEC PGM=B\n"
                  "//Y   DD DSN=P.Y,DISP=SHR\n"
                  "//    PEND\n")


def _dds_by_step(job):
    return [(s.name, [d.ddname for d in s.dds]) for s in job.steps]


def test_a_dd_naming_no_proc_step_is_added_to_the_first_step_of_the_proc():
    """z/OS MVS JCL Reference, 'Location in the JCL': a modifying statement that names no
    step applies to the FIRST step of the procedure while no earlier one has named a
    step. It was added to the last step."""
    job = parse_jcl("//J JOB\n" + _TWO_STEP_PROC + "//STEP1 EXEC TWO\n"
                    "//ADDED DD DSN=J.ADDED,DISP=SHR\n")
    assert _dds_by_step(job) == [("STEP1.PS1", ["X", "ADDED"]), ("STEP1.PS2", ["Y"])]


def test_a_dd_naming_no_proc_step_overrides_the_first_steps_dd_of_that_name():
    """Where the first step has a DD of that name it is an override like any other: it
    merges (the PROC's DISP survives, so the direction does), it replaces the first
    dataset only, and the unnamed DD after it addresses the second. It was a second,
    unmerged DD of that name on the last step."""
    proc = ("//TWO PROC\n//PS1 EXEC PGM=A\n//IN DD DSN=P.IN1,DISP=SHR\n"
            "//   DD DSN=P.IN2,DISP=SHR\n//   DD DSN=P.IN3,DISP=SHR\n"
            "//PS2 EXEC PGM=B\n//Y DD DSN=P.Y,DISP=SHR\n// PEND\n")
    job, _, rows = _rows("//J JOB\n" + proc + "//STEP1 EXEC TWO\n"
                         "//IN DD DSN=J.IN1\n"
                         "//   DD DSN=J.IN2\n")
    assert rows == [("STEP1.PS1", "IN", 1, "J.IN1", "input"),
                    ("STEP1.PS1", "IN", 2, "J.IN2", "input"),
                    ("STEP1.PS1", "IN", 3, "P.IN3", "input"),
                    ("STEP1.PS2", "Y", None, "P.Y", "input")]
    assert job.steps[0].dds[0].override is True


def test_a_dd_naming_no_proc_step_follows_the_step_the_previous_one_named():
    """The same topic: it applies to the step named in the previous overriding or added
    statement - forwards and back - and to the first step only until one is named."""
    job = parse_jcl("//J JOB\n" + _TWO_STEP_PROC + "//STEP1 EXEC TWO\n"
                    "//FIRST DD DSN=J.FIRST,DISP=SHR\n"
                    "//PS2.Y DD DSN=J.Y\n"
                    "//NEW   DD DSN=J.NEW,DISP=SHR\n"
                    "//PS1.X DD DSN=J.X\n"
                    "//BACK  DD DSN=J.BACK,DISP=SHR\n")
    assert _dds_by_step(job) == [("STEP1.PS1", ["X", "FIRST", "BACK"]),
                                 ("STEP1.PS2", ["Y", "NEW"])]


def test_the_step_a_dd_names_does_not_outlive_its_invocation():
    job = parse_jcl("//J JOB\n" + _TWO_STEP_PROC + "//STEP1 EXEC TWO\n"
                    "//PS2.Y DD DSN=J.Y\n"
                    "//STEP2 EXEC TWO\n"
                    "//ADDED DD DSN=J.ADDED,DISP=SHR\n")
    assert _dds_by_step(job) == [("STEP1.PS1", ["X"]), ("STEP1.PS2", ["Y"]),
                                 ("STEP2.PS1", ["X", "ADDED"]), ("STEP2.PS2", ["Y"])]


def test_instream_data_after_a_dd_naming_no_proc_step_overrides_the_first_steps_dd():
    """``//SYSIN DD *`` after the EXEC of a PROC whose first step has a SYSIN: the cards
    are that step's SYSIN, not a second SYSIN on the last step."""
    proc = ("//TWO PROC\n//PS1 EXEC PGM=SORT\n//SYSIN DD DUMMY\n"
            "//PS2 EXEC PGM=IEFBR14\n//LAST DD DSN=P.LAST,DISP=SHR\n// PEND\n")
    job = parse_jcl("//J JOB\n" + proc + "//STEP1 EXEC TWO\n"
                    "//SYSIN DD *\n  SORT FIELDS=COPY\n/*\n")
    assert _dds_by_step(job) == [("STEP1.PS1", ["SYSIN"]), ("STEP1.PS2", ["LAST"])]
    assert job.steps[0].dds[0].instream_lines == ["  SORT FIELDS=COPY"]


def test_a_concatenated_dd_added_to_a_proc_binds_as_the_ibm_example_says():
    """z/OS MVS JCL Reference, 'References to concatenated data sets', its PROC example:
    DD1 resolves to MYDSN1 and MYDSN4 is concatenated to MYDSN3; DDA resolves to MINE1 and
    MINE4 is concatenated to MINE3. With INPUT sent to the last step, ``//S2.INPUT``
    overrode it there: MYDSN1 and MYDSN4 were in no view, and DD1 was flagged as reading
    something not in the JCL."""
    job, _, rows = _rows(
        "//J JOB\n"
        "//TPROC  PROC\n"
        "//S1     EXEC PGM=IEFBR14\n"
        "//DD1    DD DDNAME=INPUT\n"
        "//DD2    DD DSN=MYDSN2,DISP=SHR\n"
        "//DD3    DD DSN=MYDSN3,DISP=SHR\n"
        "//S2     EXEC PGM=IEFBR14\n"
        "//DDA    DD DDNAME=INPUT\n"
        "//DDB    DD DSN=MINE2,DISP=SHR\n"
        "//DDC    DD DSN=MINE3,DISP=SHR\n"
        "//       PEND\n"
        "//STEP1  EXEC TPROC\n"
        "//INPUT  DD DSN=MYDSN1,DISP=SHR\n"
        "//       DD DSN=MYDSN4,DISP=SHR\n"
        "//S2.INPUT DD DSN=MINE1,DISP=SHR\n"
        "//       DD DSN=MINE4,DISP=SHR\n")
    assert rows == [("STEP1.S1", "DD1", None, "MYDSN1", "input"),
                    ("STEP1.S1", "DD2", None, "MYDSN2", "input"),
                    ("STEP1.S1", "DD3", 1, "MYDSN3", "input"),
                    ("STEP1.S1", "DD3", 2, "MYDSN4", "input"),
                    ("STEP1.S2", "DDA", None, "MINE1", "input"),
                    ("STEP1.S2", "DDB", None, "MINE2", "input"),
                    ("STEP1.S2", "DDC", 1, "MINE3", "input"),
                    ("STEP1.S2", "DDC", 2, "MINE4", "input")]
    assert not any("no later DD statement" in f for f in job.flags)


def test_a_dd_naming_no_proc_step_inside_an_include_goes_to_the_first_step_too():
    """An INCLUDE member's statements are read where the INCLUDE stands - whether the
    EXEC of the PROC is in the job or in the member itself."""
    dd = "//EXTRA DD DSN=I.EXTRA,DISP=SHR\n"
    members = {"DDONLY": dd, "CALLS": "//STEP1 EXEC TWO\n" + dd}
    for body in ("//STEP1 EXEC TWO\n// INCLUDE MEMBER=DDONLY\n",
                 "// INCLUDE MEMBER=CALLS\n"):
        job = parse_jcl("//J JOB\n" + _TWO_STEP_PROC + body, resolver=members.get)
        assert _dds_by_step(job) == [("STEP1.PS1", ["X", "EXTRA"]), ("STEP1.PS2", ["Y"])]


def test_a_repeated_ddname_in_a_step_that_runs_a_program_is_not_an_override():
    """Only a DD coded after the EXEC of a PROC modifies anything. In a PGM= step - and
    under a PROC that did not resolve, whose DDs are not known - it stays as coded."""
    for exec_ in ("PGM=A", "NOPROC"):
        job = parse_jcl(f"//J JOB\n//S EXEC {exec_}\n"
                        "//X DD DSN=A.X1,DISP=SHR\n//X DD DSN=A.X2,DISP=OLD\n")
        assert [(d.ddname, d.override, [g.dsn for g in d.segments])
                for d in job.steps[0].dds] == [("X", False, ["A.X1"]),
                                               ("X", False, ["A.X2"])]


def test_a_ddname_reference_takes_the_definition_a_later_dd_supplies():
    """``DDNAME=CARDS`` postpones SYSUT1 to the DD named CARDS; CARDS is not a ddname the
    step allocates."""
    job, _, rows = _rows("//J JOB\n//S EXEC PGM=IEBGENER\n"
                         "//SYSUT1 DD DDNAME=CARDS\n"
                         "//SYSUT2 DD DSN=A.OUT,DISP=(NEW,CATLG)\n"
                         "//CARDS  DD DSN=A.IN,DISP=SHR\n")
    assert rows == [("S", "SYSUT1", None, "A.IN", "input"),
                    ("S", "SYSUT2", None, "A.OUT", "output")]
    assert job.flags == []


def test_a_ddname_reference_to_a_concatenation_right_after_it_takes_all_of_it():
    job, _, rows = _rows("//J JOB\n//S EXEC PGM=IEBGENER\n"
                         "//SYSUT2 DD SYSOUT=*\n"
                         "//SYSUT1 DD DDNAME=INPUT\n"
                         "//INPUT  DD DSN=A.IN1,DISP=SHR\n"
                         "//       DD DSN=A.IN2,DISP=SHR\n")
    assert rows == [("S", "SYSUT1", 1, "A.IN1", "input"),
                    ("S", "SYSUT1", 2, "A.IN2", "input")]
    assert job.flags == []


def test_a_ddname_reference_to_a_concatenation_binds_as_the_system_does():
    """With DD statements between the reference and the concatenation, the reference
    takes the FIRST dataset and the rest are concatenated to the last DD statement
    before the concatenation - not to the referencing DD. Surprising, and so flagged."""
    job, _, rows = _rows("//J JOB\n//S EXEC PGM=IEBGENER\n"
                         "//SYSUT1 DD DDNAME=INPUT\n"
                         "//OTHER  DD DSN=A.OTHER,DISP=SHR\n"
                         "//INPUT  DD DSN=A.IN1,DISP=SHR\n"
                         "//       DD DSN=A.IN2,DISP=SHR\n")
    assert rows == [("S", "SYSUT1", None, "A.IN1", "input"),
                    ("S", "OTHER", 1, "A.OTHER", "input"),
                    ("S", "OTHER", 2, "A.IN2", "input")]
    assert any("DDNAME=INPUT" in f and "DD OTHER" in f for f in job.flags)


def test_a_ddname_reference_no_later_dd_answers_is_flagged():
    job, _, rows = _rows("//J JOB\n//S EXEC PGM=IEBGENER\n"
                         "//INPUT  DD DSN=A.EARLIER,DISP=SHR\n"
                         "//SYSUT1 DD DDNAME=INPUT\n")
    assert rows == [("S", "INPUT", None, "A.EARLIER", "input")]   # earlier: not it
    assert any("SYSUT1" in f and "DDNAME=INPUT" in f for f in job.flags)


def test_a_referback_to_a_concatenation_is_its_first_dataset_only():
    _, _, rows = _rows("//J JOB\n//S1 EXEC PGM=P\n"
                       "//IN DD DSN=A.IN1,DISP=SHR\n"
                       "//   DD DSN=A.IN2,DISP=SHR\n"
                       "//S2 EXEC PGM=Q\n"
                       "//X  DD DSN=*.S1.IN,DISP=SHR\n")
    assert rows[2] == ("S2", "X", None, "A.IN1", "input")


def test_disp_on_a_later_statement_does_not_publish_a_write():
    """A concatenation is read through. OLD or MOD on position 2 or later says how that
    dataset is allocated - and it used to make the FIRST dataset read as written too."""
    job, lin, rows = _rows("//J JOB\n//S EXEC PGM=P\n"
                           "//IN DD DSN=A.IN1,DISP=SHR\n"
                           "//   DD DSN=A.IN2,DISP=OLD\n"
                           "//   DD DSN=A.IN3,DISP=(MOD,KEEP)\n")
    assert [r[4] for r in rows] == ["input", "input", "input"]
    assert all(d["producedBy"] == [] for d in lin["datasets"])
    arts = _art_by_name(job)
    assert [arts[n]["io"] for n in ("A.IN1", "A.IN2", "A.IN3")] == ["read"] * 3
    assert not any("directionAmbiguous" in a for a in arts.values())


def test_statements_naming_no_dataset_have_no_row_but_keep_their_position():
    """``DD *`` and DUMMY bind no dataset, concatenated or not, so they have no binding
    row; the datasets around them keep the position they really hold, and the step's
    inputs list all five statements by kind. A temporary dataset is a dataset."""
    job, lin, rows = _rows("//J JOB\n//S EXEC PGM=P\n"
                           "//IN DD DSN=A.IN1,DISP=SHR\n"
                           "//   DD *\nDATA\n/*\n"
                           "//   DD DUMMY\n"
                           "//   DD DSN=&&TEMP,DISP=(OLD,DELETE)\n"
                           "//   DD DSN=A.IN5,DISP=SHR\n")
    assert [r[2:4] for r in rows] == [(1, "A.IN1"), (4, "&&TEMP"), (5, "A.IN5")]
    assert [(d["concatIndex"], d["kind"]) for d in lin["steps"][0]["inputs"]] == [
        (1, "dataset"), (2, "instream"), (3, "dummy"), (4, "dataset"), (5, "dataset")]
    assert next(d for d in lin["datasets"] if d["dsn"] == "&&TEMP")["temporary"] is True
    assert any("DUMMY at position 3 of a concatenation of 5" in f for f in job.flags)


def test_a_concatenated_card_dd_is_read_as_one_stream():
    """Only the first member's cards were read, so a RUN PROGRAM in a later one - and the
    program it names - was not in the model."""
    lib = {"PARM.LIB(DSNCMD)": " DSN SYSTEM(DB2P)",
           "PARM.LIB(RUNPAY)": " RUN PROGRAM(PAYCALC) PLAN(PAYPLAN)\n END"}
    job = parse_jcl("//J JOB\n//S EXEC PGM=IKJEFT01\n"
                    "//SYSTSIN DD DSN=PARM.LIB(DSNCMD),DISP=SHR\n"
                    "//        DD DSN=PARM.LIB(RUNPAY),DISP=SHR\n",
                    resolver=lib.get)
    arts = _art_by_name(job)
    assert arts["PAYCALC"]["runVia"] == "TSO/DSN"
    assert arts["PARM.LIB(DSNCMD)"]["touchedBy"] == [
        {"step": "S", "ddname": "SYSTSIN", "concatIndex": 1}]
    assert arts["PARM.LIB(RUNPAY)"]["touchedBy"] == [
        {"step": "S", "ddname": "SYSTSIN", "concatIndex": 2}]
    assert job.flags == []


def test_a_concatenated_sort_input_names_every_dataset():
    job = parse_jcl(_CONCAT + "//SYSIN DD *\n  SORT FIELDS=(1,10,CH,A)\n/*\n")
    fl, = build_jcl_lineage(job)["fieldLineage"]
    assert fl["input"] == "A.B.IN1"
    assert fl["inputs"] == ["A.B.IN1", "A.B.IN2", "A.B.IN3"]
    assert fl["output"] == "A.B.OUT"


def test_binding_a_concatenated_ddname_lists_its_datasets_in_order():
    """One step binding a ddname to several datasets is a concatenation - one file read
    from each in turn - not the program running against different data."""
    from jcl_dependencies.views import bind_cobol_artifacts
    job = parse_jcl("//CATJOB JOB\n//S1 EXEC PGM=SQLUNLD\n"
                    "//OUTDD DD DSN=PROD.B,DISP=SHR\n"
                    "//      DD DSN=PROD.A,DISP=SHR\n", source_name="cat.jcl")
    out = bind_cobol_artifacts(_sqlunld_manifest(), [job])
    row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
    assert row["datasets"] == ["PROD.B", "PROD.A"]            # read order, not sorted
    assert "dataset" not in row and "datasetCandidates" not in row
    assert [e["concatIndex"] for e in row["boundBy"]] == [1, 2]
    assert row["resolvedBy"] == "JCL DD statement: CATJOB.S1"
    assert "needs" not in row
    assert not any("different datasets" in f for f in out["flags"])


def test_a_concatenation_in_one_job_and_a_single_dataset_in_another_are_candidates():
    from jcl_dependencies.views import bind_cobol_artifacts
    cat = parse_jcl("//CATJOB JOB\n//S1 EXEC PGM=SQLUNLD\n"
                    "//OUTDD DD DSN=PROD.B,DISP=SHR\n"
                    "//      DD DSN=PROD.A,DISP=SHR\n", source_name="cat.jcl")
    one = parse_jcl("//ONEJOB JOB\n//S1 EXEC PGM=SQLUNLD\n"
                    "//OUTDD DD DSN=PROD.B,DISP=SHR\n", source_name="one.jcl")
    out = bind_cobol_artifacts(_sqlunld_manifest(), [cat, one])
    row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
    assert row["datasetCandidates"] == ["PROD.A", "PROD.B"]
    assert "dataset" not in row and "datasets" not in row
    assert any("different datasets" in f for f in out["flags"])


def test_the_example_publishes_every_dataset_each_concatenated_dd_reads():
    job, lin, rows = _rows((EXAMPLES / "concat.jcl").read_text())
    assert [r for r in rows if r[1] != "SORTOUT" and r[0] != "COPY1"] == [
        ("MERGE", "SORTIN", 1, "PROD.SALES.DAILY.MON", "input"),
        ("MERGE", "SORTIN", 2, "PROD.SALES.DAILY.TUE", "input"),
        ("MERGE", "SORTIN", 3, "PROD.SALES.DAILY.WED", "input"),       # OLD: still read
        ("LOAD.RUN", "INFILE", 1, "PROD.SALES.EXTRACT.EAST", "input"),
        ("LOAD.RUN", "INFILE", 2, "PROD.SALES.EXTRACT.WEST.RERUN", "input"),
        ("PRINT", "SYSUT1", None, "PROD.SALES.WEEK", "input"),
        ("PRINT", "SYSUT2", 2, "PROD.SALES.WEEK.PRIOR", "input")]
    copy1 = {r[1]: r for r in rows if r[0] == "COPY1"}
    assert copy1["SYSUT1"][2:4] == (None, "PROD.SALES.DAILY.MON")     # referback: first
    assert ("MERGE", "PRINT", "PROD.SALES.WEEK") in _edges(job)
    assert len(job.flags) == 1 and "DDNAME=FEED" in job.flags[0]
# batch-26 ledger item 62: the resolver is told what kind of member it is asked for
# --------------------------------------------------------------------------- #

KINDS_JOB = ("//J        JOB (A),'T'\n"
             "//S1       EXEC PGM=SORT\n"
             "//SYSIN    DD DSN=PARM.LIB(CARD1),DISP=SHR\n"
             "//S2       EXEC PROC=PROC1\n"
             "//         INCLUDE MEMBER=INC1\n")


def test_the_resolver_is_told_what_kind_of_member_it_is_asked_for():
    """In the manifest's own words: the same row's kind, and what the estate request's
    type is derived from."""
    asked = []

    def resolver(name, kind=None):
        asked.append((name, kind))
        return None

    parse_jcl(KINDS_JOB, resolver=resolver)
    assert asked == [("PROC1", "proc"), ("INC1", "include-member"),
                     ("PARM.LIB(CARD1)", "control-card")]


def test_a_resolver_that_takes_only_the_name_still_resolves():
    """Every resolver written before the kind was passed keeps working."""
    def resolver(name):
        return "//PROC1    PROC\n//PS1      EXEC PGM=PROCPGM\n" if name == "PROC1" else None

    job = parse_jcl(KINDS_JOB, resolver=resolver)
    assert "PROCPGM" in [s.pgm for s in job.steps]
    assert not [f for f in job.flags if "resolver raised" in f], job.flags
