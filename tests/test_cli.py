"""The jcl-dependencies command line.

Moved here from the origin repository when this one became the single source: these
tests drive THIS package's CLI and nothing else. (The origin repo keeps only the tests
that compare its deprecated auto-fork against this command - those need both packages.)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so --fetcher fakes.… loads

from jcl_dependencies.cli import run                                # noqa: E402

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
FAKE = "fakes.estate:fetch_artifact"


def test_it_writes_both_views_and_both_reports(tmp_path):
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "acctunld.jcl"), "--outdir", str(out),
                "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
    assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == {
        "jcl.artifacts.json", "jcl.lineage.json",
        "jcl.prefetch.json", "jcl.fetch.json"}


def test_target_selects_views_but_never_drops_the_retrieval_account(tmp_path):
    """The reports are not a view you can opt out of: what was retrieved decides
    whether the model is right."""
    for target, expected in [
        ("artifacts", {"jcl.artifacts.json", "jcl.prefetch.json", "jcl.fetch.json"}),
        ("lineage", {"jcl.lineage.json", "jcl.prefetch.json", "jcl.fetch.json"}),
    ]:
        out = tmp_path / target
        assert run([str(EXAMPLES / "acctunld.jcl"), "--outdir", str(out),
                    "--target", target, "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
        assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == expected


def test_a_cobol_source_is_flagged_rather_than_parsed_as_a_job(tmp_path, capsys):
    src = tmp_path / "t.cbl"
    src.write_text("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n")
    run([str(src), "--outdir", str(tmp_path / "o"), "-q"])
    assert "does not look like JCL" in capsys.readouterr().err


def test_max_rounds_is_exposed_and_the_bound_is_reported(tmp_path):
    """The closure bound must stay visible when hit: a silently truncated closure looks
    exactly like a job with no more members."""
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "dailypost.jcl"), "--outdir", str(out),
                "--max-rounds", "1", "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
    pre = json.loads(next(out.glob("*.jcl.prefetch.json")).read_text())
    closure = [r for r in pre["members"] if r["member"] == "<closure>"]
    assert closure and "1 resolution rounds" in closure[0]["reason"]


def test_gather_then_replay_reproduces_the_views(tmp_path):
    bundle, live, offline = tmp_path / "b", tmp_path / "live", tmp_path / "off"
    job = str(EXAMPLES / "acctunld.jcl")
    assert run([job, "--outdir", str(tmp_path / "g"), "--fetcher", FAKE,
                "--gather-only", str(bundle), "--jobs", "1", "-q"]) == 0
    assert run([job, "--outdir", str(live), "--fetcher", FAKE,
                "--jobs", "1", "-q"]) == 0
    # No --fetcher at all on the replay: the bundle is the service.
    assert run([job, "--outdir", str(offline), "--from-bundle", str(bundle),
                "--jobs", "1", "-q"]) == 0
    for name in ("acctunld.jcl.artifacts.json", "acctunld.jcl.lineage.json"):
        assert (offline / name).read_text() == (live / name).read_text()


def test_gather_records_the_reverse_direction_it_was_given(tmp_path):
    """--gather-only is the run that happens where the INDEX is reachable, so a door the
    CLI forgets to hand it is a bundle that replays the estate and not the reverse
    direction - and the modelling box has no way to notice."""
    from mainframe_artifacts.bundle import open_bundle

    bundle, out = tmp_path / "b", tmp_path / "o"
    job = str(EXAMPLES / "acctunld.jcl")
    assert run([job, "--outdir", str(tmp_path / "g"), "--fetcher", FAKE, "--jobs", "1",
                "--gather-only", str(bundle),
                "--dependents-resolver", "fakes.index:dependents", "-q"]) == 0
    assert open_bundle(bundle).has_dependents()

    assert run([job, "--outdir", str(out), "--from-bundle", str(bundle), "--jobs", "1",
                "-q"]) == 0
    dep = json.loads(next(out.glob("*.jcl.dependents.json")).read_text())
    row = next(r for r in dep["provides"] if r["name"] == "PROD.ACCT.UNLOAD")
    assert [d["name"] for d in row["dependents"]] == ["ACCTLOAD"]


def test_python_dash_m_works():
    import os
    import subprocess
    # PREPEND to the inherited PYTHONPATH rather than replacing it, and hand the child
    # the sibling mainframe-artifacts tree the parent found the same way (conftest's
    # sys.path insertion does not survive into a subprocess; a nonexistent path is
    # inert when the distribution is pip-installed instead).
    from _mainframe_common import CHECKOUT
    inherited = os.environ.get("PYTHONPATH", "")
    pypath = os.pathsep.join(p for p in (
        str(REPO / "src"), str(CHECKOUT / "mainframe-artifacts" / "src"),
        inherited) if p)
    proc = subprocess.run(
        [sys.executable, "-m", "jcl_dependencies", "--help"],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": pypath})
    assert proc.returncode == 0
    assert "jcl-dependencies" in proc.stdout
