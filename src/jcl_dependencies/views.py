"""Two views over a parsed JCL ``Job`` (see jcl.py):

  * ``build_jcl_lineage(job)`` - the dataflow. Each step's inputs and outputs (its DDs,
    resolved to datasets), the producer -> consumer edges across steps (step 1 writes a
    dataset that step 2 reads is a real program-to-program dataflow no single-program view
    can see), and - where a utility control card defines it - the byte-field lineage
    (which output record field comes from which input bytes).

  * ``build_jcl_artifacts(job)`` - the dependency manifest in the SAME shape as the COBOL
    artifact manifest (artifacts.py): one row per related artifact - datasets, programs,
    PROCs, control-card and INCLUDE members - each tagged ``dependency`` runtime /
    compile-time, with the identity/resolution honesty the COBOL side already carries.

Both are pure reads over the ``Job``; they invent nothing. What the parser could not
resolve (a symbolic, a PROC, an OLD/I-O direction) is already flagged on the ``Job`` and is
carried through here rather than papered over.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .parser import DD, DDSegment, Job, Step, _dd_direction


# --------------------------------------------------------------------------- #
# shared enumeration
# --------------------------------------------------------------------------- #

def _seg_kind(seg: DDSegment) -> str:
    if seg.dummy:
        return "dummy"
    if seg.sysout is not None:
        return "spool"
    if seg.instream:
        return "instream"
    if seg.dsn and seg.dsn.startswith("&&"):
        return "temp"
    return "dataset"


def _dd_io(dd: DD) -> Optional[str]:
    """'input' / 'output' / 'inout' / None across a DD's segments (concatenation = input)."""
    dirs = {d for d in (_dd_direction(s) for s in dd.segments) if d}
    if not dirs:
        return None
    if dirs == {"input"}:
        return "input"
    if dirs == {"output"}:
        return "output"
    return "inout"


def _dd_dataset(dd: DD) -> Optional[DDSegment]:
    """The first real-dataset segment of a DD (for its identity), or None."""
    for s in dd.segments:
        if s.dsn and not s.instream and s.sysout is None and not s.dummy:
            return s
    return None


def _dd_rows(job: Job):
    """Yield (step, dd, seg_or_None, io) for every DD in the job."""
    for step in job.steps:
        for dd in step.dds:
            yield step, dd, _dd_dataset(dd), _dd_io(dd)


def _step_conditions(step: Step) -> Optional[dict]:
    """The conditions under which this step runs, or None for an unconditional step.
    ``if`` is the IF/THEN/ELSE nesting (every test must hold, in its stated polarity);
    ``cond`` is the parsed COND= with its bypass sense spelt out."""
    c: dict = {}
    if step.conditions:
        c["if"] = [{"test": x["expr"], "negated": x["negated"]} for x in step.conditions]
    if step.cond:
        c["cond"] = step.cond_parsed or {"raw": step.cond}
    return c or None


# --------------------------------------------------------------------------- #
# lineage
# --------------------------------------------------------------------------- #

def _dd_descriptor(dd: DD, seg: Optional[DDSegment], io: Optional[str]) -> dict:
    d: dict = {"ddname": dd.ddname, "io": io}
    if seg is not None:
        d["dataset"] = seg.dsn
        if seg.member:
            d["member"] = seg.member
        if seg.gdg:
            d["generation"] = seg.gdg
        if seg.disp:
            d["disp"] = seg.disp
        d["kind"] = "dataset"
    else:
        # instream / spool / dummy / unresolved
        seg0 = dd.segments[0] if dd.segments else None
        d["kind"] = _seg_kind(seg0) if seg0 else "unknown"
        if seg0 and seg0.sysout is not None:
            d["sysout"] = seg0.sysout
    if dd.override:
        d["override"] = True
    return d


def _field_lineage(step: Step) -> Optional[dict]:
    """Byte-field lineage for a utility step, from its parsed control card. Lays the BUILD
    fields out consecutively in the output record so each carries its output byte range as
    well as the input range it copies from."""
    control = None
    in_dd = out_dd = None
    for dd in step.dds:
        if dd.control:
            control = dd.control
    # SORT: SORTIN -> SORTOUT (or the concatenated inputs). IDCAMS REPRO: inDD -> outDD.
    for dd in step.dds:
        if dd.ddname in ("SORTIN", "SYSUT1"):
            in_dd = dd
        if dd.ddname in ("SORTOUT", "SYSUT2"):
            out_dd = dd
    if not control:
        return None
    result: dict = {"step": step.name, "utility": control.get("utility")}
    si = _dd_dataset(in_dd) if in_dd else None
    so = _dd_dataset(out_dd) if out_dd else None
    if si:
        result["input"] = si.dsn
    if so:
        result["output"] = so.dsn
    if control.get("filter"):
        result["filter"] = control["filter"]
    if control.get("sum"):
        result["sum"] = control["sum"]
    if control.get("build"):
        out_pos = 1
        fields = []
        for i, slot in enumerate(control["build"], start=1):
            f = {"outField": i}
            length = slot.get("inLength")
            if slot["from"] == "input":
                f.update({"from": "input",
                          "inBytes": f"{slot['inStart']}-{slot['inEnd']}",
                          "outBytes": f"{out_pos}-{out_pos + length - 1}"})
                if slot.get("edit"):
                    f["edit"] = slot["edit"]
                out_pos += length
            elif slot["from"] == "constant":
                f.update({"from": "constant", "literal": slot["literal"]})
            elif slot["from"] == "fill":
                f.update({"from": "fill", "count": slot["count"], "pad": slot["pad"]})
                out_pos += slot["count"]
            else:
                f.update(slot)
            fields.append(f)
        result["fields"] = fields
    if control.get("operations"):        # IDCAMS
        result["operations"] = control["operations"]
    return result


def build_jcl_lineage(job: Job) -> dict:
    """Dataflow across the job's steps, plus control-card byte-field lineage."""
    steps_out: List[dict] = []
    # dataset identity (DSN, generation-independent) -> producers / consumers
    datasets: Dict[str, dict] = {}

    def touch(seg: DDSegment, step: Step, dd: DD, io: Optional[str]) -> None:
        key = seg.dsn
        rec = datasets.setdefault(key, {
            "dsn": key, "producedBy": [], "consumedBy": [],
            "temporary": key.startswith("&&")})
        entry = {"step": step.name, "ddname": dd.ddname,
                 "disp": seg.disp[0] if seg.disp else None}
        if seg.gdg:
            entry["generation"] = seg.gdg
        if io == "output":
            rec["producedBy"].append(entry)
        elif io == "input":
            rec["consumedBy"].append(entry)
        else:                                   # inout / unknown -> record on both, noted
            rec["producedBy"].append({**entry, "note": "direction ambiguous (OLD/I-O)"})
            rec["consumedBy"].append({**entry, "note": "direction ambiguous (OLD/I-O)"})

    field_rows: List[dict] = []
    conds: Dict[str, dict] = {}
    for step in job.steps:
        c = _step_conditions(step)
        if c:
            conds[step.name] = c
    for step in job.steps:
        inputs, outputs = [], []
        for dd in step.dds:
            seg = _dd_dataset(dd)
            io = _dd_io(dd)
            desc = _dd_descriptor(dd, seg, io)
            if io == "output":
                outputs.append(desc)
            elif io == "input":
                inputs.append(desc)
            else:
                (outputs if desc["kind"] == "spool" else inputs).append(desc)
            if seg is not None:
                touch(seg, step, dd, io)
        srow: dict = {"step": step.name, "program": step.pgm}
        if step.from_proc:
            srow["proc"] = step.from_proc
            srow["procStep"] = step.proc_step
        if step.proc and step.proc_resolved is False:
            srow["proc"] = step.proc
            srow["procResolved"] = False
        if conds.get(step.name):
            srow["conditions"] = conds[step.name]
        srow["inputs"] = inputs
        srow["outputs"] = outputs
        steps_out.append(srow)
        fl = _field_lineage(step)
        if fl:
            field_rows.append(fl)

    # The join that resolves the COBOL side: for each step running a program, the
    # ddname -> dataset binding. A COBOL program's interface knows only `OUT-FILE ASSIGN
    # OUTDD`; this says OUTDD -> PROD.ACCT.UNLOAD, the DSN that program was missing.
    bindings: List[dict] = []
    for step in job.steps:
        if not step.pgm:
            continue
        for dd in step.dds:
            seg = _dd_dataset(dd)
            if seg is None:
                continue
            b = {"program": step.pgm, "step": step.name, "ddname": dd.ddname,
                 "dataset": seg.dsn, "io": _dd_io(dd)}
            if seg.gdg:
                b["generation"] = seg.gdg
            if seg.member:
                b["member"] = seg.member
            if conds.get(step.name):
                b["conditions"] = conds[step.name]
            bindings.append(b)

    # dataflow edges: a dataset produced by one step and consumed by another is an edge.
    # An edge holds only when BOTH its steps actually run, so a conditional endpoint's
    # conditions ride on the edge.
    dataflow: List[dict] = []
    for key, rec in datasets.items():
        for p in rec["producedBy"]:
            for c in rec["consumedBy"]:
                if p["step"] != c["step"]:
                    edge = {"from": p["step"], "to": c["step"], "dataset": key,
                            "outDD": p["ddname"], "inDD": c["ddname"]}
                    ec = {}
                    if conds.get(p["step"]):
                        ec["producer"] = conds[p["step"]]
                    if conds.get(c["step"]):
                        ec["consumer"] = conds[c["step"]]
                    if ec:
                        edge["conditions"] = ec
                    dataflow.append(edge)
        rec["intermediate"] = bool(rec["producedBy"] and rec["consumedBy"])

    return {
        "format": "jcl-dependencies-lineage",
        "job": job.name,
        "source": job.source_name,
        "note": (
            "Job-level dataflow. Each step lists its inputs and outputs (DDs resolved to "
            "datasets); 'dataflow' is the producer->consumer edges across steps that no "
            "single-program view can see (step 1 writes a dataset step 2 reads). "
            "'fieldLineage' is byte-field lineage from a utility control card: each output "
            "record field traced to the input bytes it copies (SORT BUILD/OUTREC), plus "
            "the filter (INCLUDE/OMIT COND) that decides which records survive. A DSN with "
            "a GDG relative generation is keyed on its base (the stable identity) with the "
            "generation recorded. 'conditions' on a step (and on the dataflow edges and "
            "ddBindings it contributes) say when it actually runs: 'if' is the IF/THEN/"
            "ELSE nesting (every test must hold, negated=true for an ELSE branch), 'cond' "
            "is the parsed COND= with its BYPASS sense spelt out ('runsWhen' is the "
            "negation a reader wants - COND is the back-to-front one). Nothing is invented "
            "- unresolved symbolics/PROCs and ambiguous OLD/I-O directions are in 'flags'. "
            "Where a step runs a COBOL program this tool analyses, the DD ddname is the "
            "join to that program's own interface/lineage: this view supplies the dataset "
            "its ddname was missing."
        ),
        "steps": steps_out,
        "datasets": sorted(datasets.values(), key=lambda r: r["dsn"]),
        "dataflow": dataflow,
        "fieldLineage": field_rows,
        "ddBindings": bindings,
        "flags": list(job.flags),
    }


# --------------------------------------------------------------------------- #
# artifacts  (same shape as the COBOL artifact manifest)
# --------------------------------------------------------------------------- #

def _io_from_dirs(dirs: set) -> str:
    r = "input" in dirs or "inout" in dirs
    w = "output" in dirs or "inout" in dirs
    if r and w:
        return "read-write"
    return "read" if r else "write"


def build_jcl_artifacts(job: Job) -> dict:
    """The related-artifact manifest for a JCL job, mirroring the COBOL manifest: datasets,
    programs, PROCs, control-card and INCLUDE members, each with dependency/identity and
    the resolution chain still needed."""
    # datasets (keyed by DSN base), aggregating direction + which steps/DDs touch them
    ds: Dict[str, dict] = {}
    control_members: Dict[str, dict] = {}
    for step, dd, seg, io in _dd_rows(job):
        # a control-card DATASET (SYSIN DD DSN=...): a parameter file, not plain data
        is_card_dd = dd.ddname in ("SYSIN", "TOOLIN", "SYSTSIN", "DFSPARM")
        if seg is not None and is_card_dd:
            rec = control_members.setdefault(seg.dsn, {
                "artifact": seg.dsn + (f"({seg.member})" if seg.member else ""),
                "kind": "control-card", "dependency": "runtime", "io": "read",
                "identity": "global", "touchedBy": []})
            rec["touchedBy"].append({"step": step.name, "ddname": dd.ddname})
            continue
        if seg is None:
            continue
        rec = ds.setdefault(seg.dsn, {
            "dsn": seg.dsn, "_dirs": set(), "touchedBy": [],
            "temporary": seg.dsn.startswith("&&"),
            "generations": set()})
        rec["_dirs"].add(io or "unknown")
        touched = {"step": step.name, "ddname": dd.ddname,
                   "disp": seg.disp[0] if seg.disp else None}
        if _step_conditions(step):
            touched["conditional"] = True     # the touch happens only if the step runs
        rec["touchedBy"].append(touched)
        if seg.gdg:
            rec["generations"].add(seg.gdg)

    artifacts: List[dict] = []
    for key, rec in ds.items():
        dirs = rec.pop("_dirs")
        temporary = rec.pop("temporary")
        gens = sorted(rec.pop("generations"))
        row = {
            "artifact": key,
            "kind": "dataset",
            "dependency": "runtime",
            "io": _io_from_dirs(dirs),
            "identity": "job-scoped" if temporary else "global",
            "touchedBy": rec["touchedBy"],
        }
        if gens:
            row["generations"] = gens
            row["gdg"] = True
        if "unknown" in dirs or "inout" in dirs:
            row["directionAmbiguous"] = True
        if temporary:
            row["temporary"] = True
            row["needs"] = ("a temporary (&&) dataset - scratch, scoped to this job; no "
                            "estate-wide identity")
        else:
            row["resolvedBy"] = None      # a real DSN already IS the estate identity
            row["needs"] = ("none - the DSN is the catalog-global identity; DDL/DCLGEN or "
                            "the record layout gives its fields")
        artifacts.append(row)

    artifacts.extend(control_members.values())

    # programs EXECed
    seen_prog: Dict[str, dict] = {}
    for step in job.steps:
        if not step.pgm:
            continue
        row = seen_prog.setdefault(step.pgm, {
            "artifact": step.pgm, "kind": "program", "dependency": "runtime",
            "identity": "global", "steps": [],
            "resolvedBy": "binder / link-edit control (STEPLIB/JOBLIB/LINKLIST)",
            "needs": ("the load library (STEPLIB/JOBLIB or LINKLIST) that provides this "
                      "module; a utility name (SORT/IDCAMS) resolves to the installed "
                      "utility")})
        row["steps"].append(step.name)
    artifacts.extend(seen_prog.values())

    # PROCs invoked (compile-time: assembled into the job before it runs, like a copybook)
    seen_proc: Dict[str, dict] = {}
    for step in job.steps:
        name = step.from_proc or (step.proc if step.proc else None)
        if not name:
            continue
        resolved = bool(step.from_proc) or step.proc_resolved is True
        row = seen_proc.setdefault(name, {
            "artifact": name, "kind": "proc", "dependency": "compile-time",
            "identity": "program-local", "status": "expanded" if resolved else "unresolved",
            "resolvedBy": "PROCLIB / JCLLIB ORDER",
            "needs": ("the PROC library (JCLLIB ORDER / system PROCLIB concatenation) that "
                      "holds this member; SYSLIB-order-style ambiguity applies")})
        if not resolved:
            row["status"] = "unresolved"
    artifacts.extend(seen_proc.values())

    # INCLUDE members (compile-time)
    for member in dict.fromkeys(job.includes):
        artifacts.append({
            "artifact": member, "kind": "include-member", "dependency": "compile-time",
            "identity": "program-local", "resolvedBy": "JCLLIB ORDER / system PROCLIB",
            "needs": ("the library that holds this INCLUDE member; its content is part of "
                      "the effective JCL")})

    # spool (SYSOUT) and DUMMY are noted, not treated as related artifacts
    excluded: List[dict] = []
    spool_seen, dummy_seen = set(), set()
    for step, dd, seg, io in _dd_rows(job):
        seg0 = dd.segments[0] if dd.segments else None
        if seg0 and seg0.sysout is not None and dd.ddname not in spool_seen:
            spool_seen.add(dd.ddname)
            excluded.append({"name": dd.ddname, "kind": "spool",
                             "reason": "SYSOUT spool (job log / print), not a related "
                                       "dataset"})
        elif seg0 and seg0.dummy and dd.ddname not in dummy_seen:
            dummy_seen.add(dd.ddname)
            excluded.append({"name": dd.ddname, "kind": "dummy",
                             "reason": "DUMMY - no dataset"})

    _CLASS_ORDER = {"dataset": 0, "control-card": 1, "program": 2, "proc": 3,
                    "include-member": 4}
    artifacts.sort(key=lambda r: (_CLASS_ORDER.get(r["kind"], 9), r["artifact"]))

    return {
        "format": "jcl-dependencies-artifacts",
        "job": job.name,
        "source": job.source_name,
        "note": (
            "One row per artifact this job is related to: datasets (dependency: runtime), "
            "the programs its steps EXEC, and the PROCs / INCLUDE / control-card members it "
            "is assembled from (dependency: compile-time) - the same shape as the COBOL "
            "artifact manifest. A real DSN is already the catalog-global identity "
            "(resolvedBy: null); a temporary (&&) dataset is job-scoped scratch; a GDG is "
            "keyed on its base with the generation recorded. Programs resolve via the load "
            "library, PROCs/INCLUDE via the JCLLIB/PROCLIB concatenation (the SYSLIB-order "
            "hazard again). SYSOUT and DUMMY are excluded with the reason. Nothing is "
            "invented - unresolved symbolics/PROCs are in 'flags'."
        ),
        "artifacts": artifacts,
        "excluded": excluded,
        "flags": list(job.flags),
    }


# --------------------------------------------------------------------------- #
# the join: resolve a COBOL manifest's file ddnames against JCL ddBindings
# --------------------------------------------------------------------------- #

def bind_cobol_artifacts(cobol_manifest: dict, jobs) -> dict:
    """Enrich a COBOL program's artifact manifest (artifacts.build_artifacts output) with
    the dataset each file's ddname binds to in the supplied JCL job(s).

    This is the join both sides were built for: a COBOL file row carries ``ddname`` and
    says *"the DSN is in the JCL"*; a JCL job's ``ddBindings`` says ``OUTDD ->
    PROD.ACCT.UNLOAD`` for the step running this program. Matching on
    ``(program, ddname)``, each matched file row gains ``dataset`` and ``boundBy`` (which
    job/step made the binding, with the step's run conditions where the JCL is
    conditional), and its ``resolvedBy`` becomes the ACTUAL DD statement rather than the
    category "JCL DD statement".

    Honesty rules: the same program bound to DIFFERENT datasets across the supplied jobs
    is a fact, not an error - the row lists ``datasetCandidates`` instead of picking one,
    and a flag says each ``boundBy`` entry names which job uses which. An unmatched ddname
    is left exactly as it was (still needing JCL). Returns a new manifest; the input is
    not mutated.
    """
    import copy
    out = copy.deepcopy(cobol_manifest)
    program = out.get("program")
    flags: List[str] = out.setdefault("flags", [])

    by_ddname: Dict[str, List[dict]] = {}
    bound_jobs: List[dict] = []
    for job in jobs:
        lin = build_jcl_lineage(job)
        bound_jobs.append({"job": job.name or None, "source": job.source_name})
        for b in lin["ddBindings"]:
            if b["program"] != program:
                continue
            entry = {"job": job.name or job.source_name, "step": b["step"],
                     "dataset": b["dataset"], "io": b["io"]}
            for k in ("generation", "member", "conditions"):
                if b.get(k):
                    entry[k] = b[k]
            by_ddname.setdefault(b["ddname"], []).append(entry)

    matched = 0
    for row in out.get("artifacts", []):
        if row.get("kind") != "file" or not row.get("ddname"):
            continue
        found = by_ddname.get(row["ddname"])
        if not found:
            continue
        matched += 1
        row["boundBy"] = found
        datasets = sorted({e["dataset"] for e in found})
        if len(datasets) == 1:
            row["dataset"] = datasets[0]
            row["resolvedBy"] = "JCL DD statement: " + ", ".join(
                sorted({f"{e['job']}.{e['step']}" for e in found}))
            # The chain is closed: the ddname now has its DSN, so nothing further is
            # needed to identify the dataset. (Its record layout is a different question.)
            row.pop("needs", None)
        else:
            row["datasetCandidates"] = datasets
            flags.append(
                f"file {row['artifact']} (ddname {row['ddname']}): bound to "
                f"{len(datasets)} different datasets across the supplied JCL - the same "
                f"program runs against different data in different jobs/steps; 'boundBy' "
                f"says which job uses which. Not collapsed.")

    out["jclBinding"] = {"jobs": bound_jobs, "boundFiles": matched}
    if matched:
        out["note"] = out.get("note", "") + (
            " File rows with 'dataset'/'boundBy' were resolved against the supplied JCL: "
            "the ddname -> DSN binding is closed, and 'boundBy' names the job/step that "
            "closed it (with the step's run conditions where the JCL is conditional).")
    return out
