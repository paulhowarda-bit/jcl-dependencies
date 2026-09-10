"""The reverse direction: what depends on what this job PROVIDES.

A job's own text says what it reads, runs and is assembled from. It cannot say which
OTHER job reads the dataset this one writes - that is a scheduling fact held in an estate
index - so the answer arrives through a door and is reported as given.

What is asked about is deliberately narrow: the datasets this job WRITES, and the PROC it
is when it is one. Asking the reverse question about what the job depends on would be
asking who else runs PGM=IEFBR14 - true, and useless.
"""

import json

from mainframe_artifacts.dependents import DependentsLookup

from jcl_dependencies.api import analyze
from jcl_dependencies.parser import parse_jcl
from jcl_dependencies.views import FORMAT_DEPENDENTS, build_jcl_dependents

JOB = ("//ACCTUNLD JOB\n"
       "//S1 EXEC PGM=IKJEFT01\n"
       "//OUTDD  DD DSN=PROD.ACCT.EXTRACT,DISP=(NEW,CATLG)\n"
       "//INDD   DD DSN=PROD.ACCT.MASTER,DISP=SHR\n"
       "//WORK   DD DSN=&&TEMP,DISP=(NEW,PASS)\n")

PROC = ("//UNLDPRC PROC\n"
        "//S1 EXEC PGM=IKJEFT01\n"
        "//OUTDD DD DSN=PROD.ACCT.EXTRACT,DISP=(NEW,CATLG)\n")

#: What a host index holds: one downstream job reads the extract.
_INDEX = {
    "PROD.ACCT.EXTRACT|dataset": [
        {"name": "ACCTLOAD", "kind": "JOB", "via": "DD DSN=... DISP=SHR",
         "match_strength": "qualified", "detail": "step LOAD reads it as INDD"},
    ],
}


def _view(source=JOB, mapping=None, resolver=None):
    job = parse_jcl(source, source_name="acctunld.jcl")
    return build_jcl_dependents(job, DependentsLookup(mapping, resolver))


# --- what is asked about --------------------------------------------------------------

def test_only_what_the_job_provides_is_asked_about():
    asked = []

    def index(name, kind=None):
        asked.append((name, kind))
        return []

    view = _view(resolver=index)
    # The dataset it WRITES - not the one it reads, and not the temporary.
    assert asked == [("PROD.ACCT.EXTRACT", "dataset")]
    assert [row["name"] for row in view["provides"]] == ["PROD.ACCT.EXTRACT"]
    assert view["provides"][0]["provides"] == "written by step S1"


def test_a_proc_is_asked_about_as_itself_as_well():
    asked = []

    def index(name, kind=None):
        asked.append((name, kind))
        return []

    _view(PROC, resolver=index)
    # In (kind, name) order, which is what makes the view stable - not source order.
    assert asked == [("PROD.ACCT.EXTRACT", "dataset"), ("UNLDPRC", "proc")]


def test_dependents_attach_to_the_dataset_that_was_named():
    row = _view(mapping=_INDEX)["provides"][0]
    assert [d["name"] for d in row["dependents"]] == ["ACCTLOAD"]
    assert row["dependents"][0]["manifestKind"] == "proc"       # a JOB reduces to proc
    assert row["dependents"][0]["matchStrength"] == "qualified"
    assert row["count"] == 1


def test_the_view_has_the_family_keys_and_the_job_subject_key():
    view = _view(mapping=_INDEX)
    assert list(view) == ["format", "formatVersion", "job", "isProc", "source", "note",
                          "suppliedBy", "provides", "unanswered", "flags"]
    assert view["format"] == FORMAT_DEPENDENTS
    assert (view["job"], view["isProc"]) == ("ACCTUNLD", False)
    # A PROC member has no JOB card, so `job` is empty and `isProc` is the signal - the
    # reason that key exists at all.
    assert _view(PROC, mapping=_INDEX)["isProc"] is True


def test_rows_are_sorted_so_the_hosts_order_cannot_change_the_bytes():
    two = {"PROD.ACCT.EXTRACT|dataset": [
        {"name": "ZLAST", "kind": "JOB", "via": "DD"},
        {"name": "AFIRST", "kind": "JOB", "via": "DD"}]}
    flipped = {"PROD.ACCT.EXTRACT|dataset": list(
        reversed(two["PROD.ACCT.EXTRACT|dataset"]))}
    assert json.dumps(_view(mapping=two)) == json.dumps(_view(mapping=flipped))


# --- the three answers ----------------------------------------------------------------

def test_no_lookup_supplied_is_no_view_at_all():
    job = parse_jcl(JOB, source_name="acctunld.jcl")
    assert build_jcl_dependents(job, None) is None
    assert build_jcl_dependents(job, DependentsLookup()) is None


def test_a_dataset_the_index_does_not_cover_is_unanswered_not_empty():
    view = _view(resolver=lambda name, kind=None: None)
    assert view["provides"] == []
    assert view["unanswered"] == [
        {"name": "PROD.ACCT.EXTRACT", "kind": "dataset",
         "reason": "the lookup does not cover this name"}]


def test_asked_and_nothing_reads_it_is_said_with_an_empty_list():
    view = _view(resolver=lambda name, kind=None: [])
    assert view["provides"][0]["dependents"] == []
    assert view["unanswered"] == []


def test_a_lookup_that_breaks_is_flagged():
    def boom(name, kind=None):
        raise RuntimeError("index unreachable")

    view = _view(resolver=boom)
    assert view["provides"] == []
    assert "index unreachable" in view["flags"][0]
    assert view["unanswered"][0]["reason"].startswith("the lookup failed earlier")


def test_a_reported_fan_out_cap_reaches_the_view():
    def capped(name, kind=None):
        return {"rows": _INDEX["PROD.ACCT.EXTRACT|dataset"], "truncated": True,
                "total": 812}

    row = _view(resolver=capped)["provides"][0]
    assert (row["truncated"], row["total"]) == (True, 812)


# --- through analyze() and the bundle -------------------------------------------------

def test_analyze_without_the_parameter_is_the_run_it_always_was():
    analysis = analyze(JOB, source_name="acctunld.jcl", retrieve=False)
    assert analysis.dependents_lookup is None
    assert analysis.dependents() is None


def test_a_gathered_bundle_replays_the_reverse_direction(tmp_path):
    from mainframe_artifacts.bundle import open_bundle

    from jcl_dependencies.api import gather

    def index(name, kind=None):
        return _INDEX.get("{0}|{1}".format(name, kind))

    live = analyze(JOB, source_name="acctunld.jcl", retrieve=False,
                   dependents_resolver=index).dependents()
    root = tmp_path / "bundle"
    gather(JOB, source_name="acctunld.jcl", dest=str(root), dependents_resolver=index)

    bundle = open_bundle(root)
    assert bundle.has_dependents()
    replayed = analyze(bundle.source(), source_name="acctunld.jcl",
                       bundle=bundle).dependents()
    assert replayed == live
