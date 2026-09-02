"""The synonym doors reach a jcl-dependencies run."""

import json
from pathlib import Path

from jcl_dependencies.cli import run

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _db2_rows(out, stem):
    doc = json.loads((out / (stem + ".jcl.artifacts.json")).read_text(encoding="utf-8"))
    return {r["artifact"]: r for r in doc["artifacts"] if r["kind"] == "db2-table"}


def test_synonym_map_and_resolver_flags_reach_the_manifest(tmp_path, monkeypatch):
    smap = tmp_path / "syn.json"
    smap.write_text(json.dumps({"ACCT_DAILY": "T_ACCT_MASTER"}), encoding="utf-8")
    out = tmp_path / "o1"
    assert run([str(EXAMPLES / "db2load.jcl"), "--outdir", str(out), "--no-fetch",
                "--target", "artifacts", "--synonym-map", str(smap), "-qq"]) == 0
    rows = _db2_rows(out, "db2load")
    assert rows["ACCT_DAILY"]["baseTable"] == "T_ACCT_MASTER"
    assert rows["ACCT_DAILY"]["resolvedVia"] == "synonym map"

    (tmp_path / "synres_ok.py").write_text(
        "def resolve(name):\n"
        "    return {'ACCT_DAILY': 'T_ACCT_MASTER'}.get(name)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    out = tmp_path / "o2"
    assert run([str(EXAMPLES / "db2load.jcl"), "--outdir", str(out), "--no-fetch",
                "--target", "artifacts", "--synonym-resolver", "synres_ok:resolve",
                "-qq"]) == 0
    assert _db2_rows(out, "db2load")["ACCT_DAILY"]["resolvedVia"] == "catalog resolver"

    out = tmp_path / "o3"
    assert run([str(EXAMPLES / "db2load.jcl"), "--outdir", str(out), "--no-fetch",
                "--target", "artifacts", "-qq"]) == 0
    assert "baseTable" not in _db2_rows(out, "db2load")["ACCT_DAILY"]


def test_a_synonym_door_that_will_not_open_is_exit_2(tmp_path):
    base = [str(EXAMPLES / "db2load.jcl"), "--outdir", str(tmp_path / "o"), "--no-fetch",
            "-qq"]
    assert run(base + ["--synonym-map", str(tmp_path / "absent.json")]) == 2
    assert run(base + ["--synonym-resolver", "no_such_module_xyz:fn"]) == 2
    assert run(base + ["--synonym-resolver", "notaspec"]) == 2
