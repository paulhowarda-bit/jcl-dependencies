"""This package must never depend on the COBOL ones.

They are peers: the COBOL tools say what a program does, this one says what dataset it
does it to, and they meet at a plain manifest dict. Nothing about the source layout
enforces that - a single stray import would erase it while every other test still passed,
and the cost is not abstract: a JCL box would start carrying a COBOL modelling engine
(``cobol_xstate``) or a COBOL parse front-end (``cobol_parser``, the mainframe-common
parser/ distribution) it never executes, and this repository could no longer be released
on its own. An artifacts+jcl install must be unable to find either package.

A note on how, because getting it wrong is easy and silent: ``sys.meta_path`` finders are
consulted through ``find_spec``. ``find_module`` was REMOVED in Python 3.12, so a blocker
that only defines it is ignored entirely and every test here passes vacuously.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

_PREAMBLE = textwrap.dedent("""
    import sys
    sys.path.insert(0, %r)

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ("cobol_xstate", "cobol_parser"):
                raise ImportError("BLOCKED " + name)
            return None

    sys.meta_path.insert(0, Blocker())
""")


def _isolated(body):
    """A fresh interpreter: blocking a module already in sys.modules does nothing."""
    return subprocess.run([sys.executable, "-c",
                           _PREAMBLE % (str(SRC),) + textwrap.dedent(body)],
                          capture_output=True, text=True)


@pytest.mark.parametrize("package", ["cobol_xstate", "cobol_parser"])
def test_the_blocker_actually_blocks(package):
    """Guard the guard. If this passes when it should not, everything below is vacuous."""
    proc = _isolated(f"import {package}")
    assert proc.returncode != 0
    assert f"BLOCKED {package}" in proc.stderr


def test_the_package_works_with_the_cobol_package_unavailable():
    proc = _isolated("""
        from jcl_dependencies import parse_jcl, build_jcl_lineage, build_jcl_artifacts
        from jcl_dependencies.api import analyze
        a = analyze("//J JOB\\n//S EXEC PGM=IEFBR14\\n//D DD DSN=A.B,DISP=SHR\\n",
                    retrieve=False)
        assert len(a.job.steps) == 1
        assert a.lineage()["datasets"]
        assert a.artifacts()["artifacts"]
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_the_cli_works_with_the_cobol_package_unavailable(tmp_path):
    examples = Path(__file__).resolve().parents[1] / "examples"
    proc = _isolated(f"""
        import io, contextlib, os
        from jcl_dependencies.cli import run
        out = {str(tmp_path / "o")!r}
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run([{str(examples / "acctunld.jcl")!r}, "--outdir", out, "-q"])
        assert rc == 0, rc
        print("FILES", len([f for f in os.listdir(out) if f.endswith(".json")]))
    """)
    assert proc.returncode == 0, proc.stderr
    assert "FILES 4" in proc.stdout       # both views + both retrieval reports


def test_the_bind_join_takes_a_dict_and_needs_no_cobol_types():
    """bind_cobol_artifacts is the one function that serves a COBOL feature. It consumes
    a plain manifest dict, which is precisely what keeps this dependency from existing."""
    proc = _isolated("""
        import json, pathlib
        from jcl_dependencies import bind_cobol_artifacts, parse_jcl
        manifest = json.loads(pathlib.Path(%r).read_text(encoding="utf-8"))
        job = parse_jcl(pathlib.Path(%r).read_text(), source_name="acctunld.jcl")
        out = bind_cobol_artifacts(manifest, [job])
        row = next(a for a in out["artifacts"] if a.get("ddname") == "OUTDD")
        print("DATASET", row["dataset"])
    """ % (str(Path(__file__).resolve().parent / "fixtures" / "sqlunld.artifacts.json"),
           str(SRC.parent / "examples" / "acctunld.jcl")))
    assert proc.returncode == 0, proc.stderr
    assert "DATASET PROD.ACCT.UNLOAD" in proc.stdout


@pytest.mark.parametrize("module", ["parser", "views", "prefetch", "api", "cli",
                                    "__init__"])
def test_no_module_imports_the_cobol_package(module):
    """Read the source too: an import inside a rarely-taken branch would not show up in
    a passing import test."""
    src = (SRC / "jcl_dependencies" / f"{module}.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")):
            for blocked in ("cobol_xstate", "cobol_parser"):
                assert (f"{blocked}." not in s
                        and s != f"import {blocked}"
                        and not s.startswith(f"from {blocked} ")), (
                    f"jcl_dependencies/{module}.py imports the COBOL package: {s}")
