"""Db2 steps: the tables a job LOADs / UNLOADs / SQLs, the program a TSO step RUNs, and
the Db2 SYNONYM/ALIAS knowledge that turns an alias into the table it stands for."""

import json
from pathlib import Path

from mainframe_artifacts.synonyms import SynonymLookup

from jcl_dependencies.parser import (_parse_db2_utility_cards, _sql_table_refs,
                                     parse_jcl)
from jcl_dependencies.views import build_jcl_artifacts, build_jcl_lineage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _job(name: str):
    return parse_jcl((EXAMPLES / name).read_text(), resolver=None, source_name=name)


def _db2_rows(manifest):
    return {r["artifact"]: r for r in manifest["artifacts"]
            if r["kind"] in ("db2-table", "db2-tablespace")}


def _fl(job, step):
    return next(r for r in build_jcl_lineage(job)["fieldLineage"] if r["step"] == step)


class _Recorder:
    def __init__(self, answers=None, raises=None):
        self.answers, self.raises, self.calls = answers or {}, raises, []

    def __call__(self, name):
        self.calls.append(name)
        if self.raises is not None:
            raise self.raises
        return self.answers.get(name)


# --------------------------------------------------------------------------- #
# DSNUTILB
# --------------------------------------------------------------------------- #

def test_a_load_names_the_table_it_writes_and_the_dataset_it_reads():
    job = _job("db2load.jcl")
    step = next(s for s in job.steps if s.name == "STEP01")
    ctl = next(dd.control for dd in step.dds if dd.ddname == "SYSIN")
    assert ctl["utility"] == "DB2 utility"
    assert ctl["tables"] == [{"op": "LOAD", "table": "ACCT_DAILY", "io": "write",
                              "ddname": "SYSREC"}]
    fl = _fl(job, "STEP01")
    assert fl["tables"][0]["dataset"] == "PROD.ACCT.SORTED"
    # DSNUTILB's SYSUT1 / SORTOUT are LOAD's sort work files, not the step's dataflow
    assert "input" not in fl and "output" not in fl


def test_an_unload_reads_the_table_and_a_runstats_names_only_the_tablespace():
    job = _job("db2load.jcl")
    rows = _db2_rows(build_jcl_artifacts(job))
    assert rows["POSN_MASTER"]["io"] == "read"
    assert rows["POSN_MASTER"]["touchedBy"] == [{"step": "STEP02", "ddname": "SYSIN",
                                                 "op": "UNLOAD"}]
    assert rows["DBPOSN.TSPOSN"]["kind"] == "db2-tablespace"
    assert rows["DBPOSN.TSPOSN"]["touchedBy"][0]["op"] == "RUNSTATS"
    assert "ALL" not in rows
    assert _fl(job, "STEP02")["tables"][0]["dataset"] == "PROD.POSN.UNLOAD"


def test_a_db2_utility_statement_can_carry_embedded_sql_and_many_into_tables():
    ctl = _parse_db2_utility_cards([
        "  EXEC SQL",
        "    DELETE FROM T_STAGE",
        "  ENDEXEC",
        "  LOAD DATA INDDN INFILE REPLACE",
        "    INTO TABLE T_ONE WHEN (1:1) = 'A'",
        "    INTO TABLE T_TWO WHEN (1:1) = 'B'",
        "  REORG TABLESPACE DB1.TS1 SORTDATA",
        "  COPY TABLESPACE DB1.TS1 FULL YES"])
    assert ctl["tables"] == [
        {"op": "DELETE", "table": "T_STAGE", "io": "write"},
        {"op": "LOAD", "table": "T_ONE", "io": "write", "ddname": "INFILE"},
        {"op": "LOAD", "table": "T_TWO", "io": "write", "ddname": "INFILE"}]
    assert ctl["tablespaces"] == [{"op": "REORG", "tablespace": "DB1.TS1"},
                                  {"op": "COPY", "tablespace": "DB1.TS1"}]


def test_a_manifest_row_aggregates_every_touch_and_its_direction():
    art = build_jcl_artifacts(parse_jcl(
        "//J JOB X\n"
        "//S1 EXEC PGM=DSNUTILB\n"
        "//SYSIN DD *\n"
        "  UNLOAD TABLESPACE DB1.TS1 FROM TABLE T_X\n"
        "/*\n"
        "//S2 EXEC PGM=DSNUTILB\n"
        "//SYSIN DD *\n"
        "  LOAD DATA INTO TABLE T_X\n"
        "/*\n", resolver=None, source_name="j.jcl"))
    row = _db2_rows(art)["T_X"]
    assert row["io"] == "read+write"
    assert [t["step"] for t in row["touchedBy"]] == ["S1", "S2"]
    kinds = [r["kind"] for r in art["artifacts"]]
    assert kinds == sorted(kinds, key=["dataset", "control-card", "db2-table",
                                       "db2-tablespace", "program", "proc",
                                       "include-member"].index)


# --------------------------------------------------------------------------- #
# IKJEFT01 / DSN
# --------------------------------------------------------------------------- #

def test_a_tso_step_names_the_program_it_runs_not_just_ikjeft01():
    art = build_jcl_artifacts(_job("tsobatch.jcl"))
    progs = {r["artifact"]: r for r in art["artifacts"] if r["kind"] == "program"}
    assert progs["DAILYPOST"]["runVia"] == "TSO/DSN"
    assert progs["DAILYPOST"]["plans"] == ["DAILYPST"]
    assert progs["DAILYPOST"]["steps"] == ["STEP02"]
    assert progs["DSNTIAUL"]["plans"] == ["DSNTIB12"]
    assert progs["IKJEFT01"]["steps"] == ["STEP01", "STEP02"]
    assert "runVia" not in progs["IKJEFT01"]


def test_sql_under_dsntiaul_names_its_tables_with_read_and_write_told_apart():
    job = _job("tsobatch.jcl")
    rows = _db2_rows(build_jcl_artifacts(job))
    assert rows["ACCT_DAILY"]["io"] == "read" and rows["POSN_MASTER"]["io"] == "read"
    assert rows["ACCT_STAGE"]["io"] == "write"
    assert rows["ACCT_STAGE"]["touchedBy"][0]["op"] == "DELETE"
    # correlation names and SQL keywords are not tables
    assert not {"A", "P", "SELECT", "WHERE"} & set(rows)
    fl = _fl(job, "STEP01")
    assert fl["utility"] == "TSO/DSN + SQL" and fl["subsystem"] == "DB2P"
    assert fl["runs"][0] == {"program": "DSNTIAUL", "plan": "DSNTIB12",
                             "lib": "DB2P.RUNLIB.LOAD"}


def test_a_tso_steps_own_sysin_data_is_not_read_as_control_cards():
    job = _job("tsobatch.jcl")
    step = next(s for s in job.steps if s.name == "STEP02")
    assert next(dd for dd in step.dds if dd.ddname == "SYSIN").control is None
    assert _fl(job, "STEP02")["utility"] == "TSO/DSN"


def test_sql_table_extraction_is_shallow_but_not_gullible():
    refs = _sql_table_refs(
        "INSERT INTO T_A (C) SELECT C FROM T_B B JOIN T_C ON T_C.K = B.K;\n"
        "UPDATE T_D SET X = 1 WHERE K IN (SELECT K FROM T_E);\n"
        "SELECT * FROM (SELECT K FROM T_F) AS S FOR UPDATE OF X;\n"
        "MERGE INTO T_G USING T_H ON 1=1 WHEN MATCHED THEN UPDATE SET X = 2;\n"
        "CREATE TABLE T_I (K INT); LOCK TABLE T_J IN SHARE MODE; TRUNCATE TABLE T_K;")
    ops = {(r["op"], r["table"]): r["io"] for r in refs}
    assert ops[("INSERT", "T_A")] == "write" and ops[("INSERT", "T_B")] == "read"
    assert ops[("INSERT", "T_C")] == "read"
    assert ops[("UPDATE", "T_D")] == "write" and ops[("UPDATE", "T_E")] == "read"
    assert ops[("SELECT", "T_F")] == "read" and ("UPDATE", "OF") not in ops
    assert ops[("MERGE", "T_G")] == "write" and ops[("MERGE", "T_H")] == "read"
    assert ops[("CREATE", "T_I")] == "ddl" and ops[("LOCK", "T_J")] == "read"
    assert ops[("TRUNCATE", "T_K")] == "write"
    assert not {t for _, t in ops} & {"(", "S", "SELECT", "X"}


# --------------------------------------------------------------------------- #
# the synonym doors
# --------------------------------------------------------------------------- #

def test_a_synonym_stamps_the_base_table_and_the_alias_stays_the_artifact():
    rows = _db2_rows(build_jcl_artifacts(
        _job("db2load.jcl"), synonyms=SynonymLookup({"ACCT_DAILY": "T_ACCT_MASTER"})))
    assert rows["ACCT_DAILY"]["baseTable"] == "T_ACCT_MASTER"
    assert rows["ACCT_DAILY"]["resolvedVia"] == "synonym map"
    assert "baseTable" not in rows["POSN_MASTER"]


def test_the_resolver_answers_what_the_map_does_not_and_a_failure_is_flagged():
    job = _job("tsobatch.jcl")
    r = _Recorder({"ACCT_STAGE": "MMD1DBO.T_ACCT_STAGE"})
    art = build_jcl_artifacts(job, synonyms=SynonymLookup({"ACCT_DAILY": "T_ACCT"}, r))
    rows = _db2_rows(art)
    assert rows["ACCT_STAGE"]["baseTable"] == "MMD1DBO.T_ACCT_STAGE"
    assert rows["ACCT_STAGE"]["resolvedVia"] == "catalog resolver"
    assert rows["ACCT_DAILY"]["resolvedVia"] == "synonym map"
    assert sorted(r.calls) == ["ACCT_STAGE", "POSN_MASTER"]     # once per table, map wins
    assert not any("synonym resolver failed" in f for f in art["flags"])

    r = _Recorder(raises=RuntimeError("catalog down"))
    art = build_jcl_artifacts(job, synonyms=SynonymLookup(None, r))
    assert not any("baseTable" in row for row in _db2_rows(art).values())
    assert [f for f in art["flags"] if "synonym resolver failed" in f
            and "RuntimeError: catalog down" in f]
    assert len(r.calls) == 1


def test_a_resolver_returning_none_leaves_the_manifest_byte_identical():
    job = _job("db2load.jcl")
    plain = json.dumps(build_jcl_artifacts(job), sort_keys=True)
    asked = json.dumps(build_jcl_artifacts(job, synonyms=SynonymLookup(None, _Recorder())),
                       sort_keys=True)
    assert plain == asked
