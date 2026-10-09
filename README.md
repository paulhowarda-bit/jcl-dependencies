# jcl-dependencies

Parse IBM JCL jobs and PROCs, and recover what they actually do: which programs run in
what order under what conditions, which datasets flow from one step to the next, how
control cards reshape the bytes on the way through, and what the job depends on.

The COBOL says *what a program does*. It does not say *what dataset it does it to* —
that binding lives in JCL, and so does the rest of the operational truth a modernization
needs.

## Install

```bash
pip install jcl-dependencies
```

It depends on `mainframe-artifacts` (the estate boundary and the two-stage dependency
retrieval, shared with the COBOL tool) and on nothing else. Pure Python standard
library, Python ≥ 3.9.

`mainframe-artifacts` ships from the
[mainframe-common](https://github.com/paulhowarda-bit/mainframe-common) repository (one
repo, several distributions; its `mainframe-artifacts/` subdirectory). Until it is on an index,
install it straight from that repo:

```bash
pip install "mainframe-artifacts @ git+https://github.com/paulhowarda-bit/mainframe-common#subdirectory=mainframe-artifacts"
```

**It does not depend on `cobol-xstate`.** The two are peers: a JCL box carries no COBOL
modelling engine. `tests/test_boundaries.py` enforces that.

## Use

```bash
jcl-dependencies job.jcl                      # 2 views + both retrieval reports -> ./out
jcl-dependencies job.jcl --target lineage     # just the dataflow
jcl-dependencies job.jcl --summary            # + a human summary on stderr

# Gather where the estate is reachable, model where it is not
jcl-dependencies job.jcl --gather-only ./bundle
jcl-dependencies job.jcl --from-bundle ./bundle    # no network at all
```

As a library:

```python
from jcl_dependencies import analyze

job = analyze(open("job.jcl").read(), source_name="job.jcl", retrieve=False)
job.lineage()      # step-to-step dataset dataflow + control-card field lineage
job.artifacts()    # every dataset, program, PROC, INCLUDE member, control card, Db2 table
job.bind(manifest) # close a COBOL program's ddname -> dataset join
```

## Db2 steps: the tables are in the control cards

A job's Db2 dependencies are never on the `EXEC`. `PGM=DSNUTILB` says only "a Db2
utility"; its SYSIN says `LOAD DATA ... INTO TABLE T` (writes T from `INDDN`, default
`SYSREC`) or `UNLOAD ... FROM TABLE T` (reads T into `UNLDDN`), or works on a
`TABLESPACE`, which is a different identity and is reported apart (`db2-tablespace`).
`PGM=IKJEFT01` says even less: the program the step actually runs is the `RUN
PROGRAM(p) PLAN(q)` in SYSTSIN, and when that program is `DSNTIAUL`/`DSNTEP2` the
step's SYSIN is SQL whose `FROM`/`JOIN`/`INSERT INTO`/`UPDATE`/`DELETE FROM` name the
tables. Each becomes a `db2-table` row (`io` read / write / read+write, `touchedBy`
step, DD and operation) and, in the lineage view, a `fieldLineage` entry that ties the
table to the dataset the DD binds. A program run under DSN is a `program` row with
`runVia: "TSO/DSN"` and its `plans`. The column list is not known here and is not
pretended; nor is the SQL parsed beyond its table references.

A table written under a Db2 **ALIAS or SYNONYM** is reported as written - the join to
the base table lives only in the catalog, so it is never guessed. Supply it and the row
gains `baseTable` and `resolvedVia`:

```bash
jcl-dependencies job.jcl --synonym-map synonyms.json            # a {"ALIAS": "TABLE"} file
jcl-dependencies job.jcl --synonym-resolver mycatalog:resolve    # FUNC(name) -> table | None
```

Both flags come from `mainframe_artifacts.cliargs.add_synonym_args`, shared with the
COBOL and Easytrieve front-ends; the map answers first, the resolver answers what the map
does not hold, and a resolver that raises is a flagged failed lookup, never "not a
synonym". In Python: `analyze(src, synonyms=..., synonym_resolver=...)`.

## What it follows, and what it does not

The crawl is an **assembly** chain, not a scheduling one. A job pulls in cataloged
PROCs, `INCLUDE` members and control-card datasets, each of which can carry `EXEC PGM=`
steps and DD statements that appear nowhere in the JCL file itself. Parsed without them,
those steps do not show up as programs, as datasets, or at all — and the job reads as far
simpler than it is. So stage 1 retrieves them before the parse, by replaying the parse
until it stops asking for members it has not got.

Nothing here follows job-to-job scheduling references (INTRDR, TWS, CA-7). That is a
different graph, and this package does not pretend to model it.

**A PROC step's condition comes from two artifacts, and both are kept.** A step's
`conditions.cond` is the `COND=` coded on its own `EXEC` — for a PROC step, the one in the
PROC member, the same in every job that runs it. `conditions.invokedCond` is the one the
calling `EXEC` applied to it: `COND=` for every step of the PROC, `COND.procstep=` for one
step (and, for a nested PROC, the condition aimed at the step that called it). Where both
exist **`invokedCond` is the one that holds** — the calling EXEC's COND overrides the
called EXEC's — so the condition a step actually runs under is `invokedCond` if present,
else `cond`. A `COND.procstep=` naming no step of the PROC, or `COND=` and
`COND.procstep=` coded together, is flagged. See `examples/proccond.jcl`.

**A DD that names its dataset indirectly is followed to the name.** A symbol's value
coded in apostrophes (`HLQ='PROD'`, `GEN='+1'`) holds what is between them — the
apostrophes delimit it — so `&HLQ..A.B` is `PROD.A.B` and `X.GDG(&GEN)` is still split
into its base and generation. A backward reference (`DSN=*.ddname`, `*.stepname.ddname`,
`*.stepname.procstepname.ddname`) is a pointer to the dataset an earlier DD names, and
the binding carries that dataset, generation and member, which is what lets `dataflow`
see two steps sharing it. Inside a PROC a sibling step is named as the PROC names it. A
referback naming no earlier DD with a dataset name is left as written and flagged. See
`examples/refback.jcl`.

**Every dataset of a concatenated DD is published.** A named DD followed by unnamed ones
is one ddname reading several datasets in turn, and each is a row — in `ddBindings`, in
`datasets`, in the step's inputs and in the artifact manifest — carrying **`concatIndex`**,
its 1-based position among the DD's statements. So a `ddBindings` row is keyed on
`(step, ddname, concatIndex)`; `(step, ddname)` alone no longer names one row
(`formatVersion` 4). A DD of one statement carries no `concatIndex` and reads exactly as
before. The rules, each the system's own:

- A dataset at position 2 or later is always **read**, whatever its `DISP` — `OLD` or
  `MOD` there says how it is allocated, not that the step writes it.
- A statement that names no dataset (`DD *`, `DUMMY`) has no `ddBindings` row,
  concatenated or not; the datasets around it keep the position they really hold, and the
  step's inputs list it by `kind`. A temporary (`&&`) dataset is a dataset, marked
  `temporary` as ever. A `DUMMY` anywhere but last is flagged: the system ignores what is
  concatenated after it.
- A backward reference to a concatenated DD is its **first** dataset only.
- A PROC override of a concatenated DD overrides the first dataset and keeps the rest;
  unnamed DDs after it address the following positions in order, one with nothing coded
  leaving its position as the PROC had it.
- `DDNAME=name` takes the definition a later DD of the step supplies. Where that DD is a
  concatenation the reference takes its first dataset, and the rest are concatenated to
  the last DD statement before it — the referencing DD only when nothing is coded
  between the two. Anywhere else that is flagged, as is a reference no later DD answers.
- A concatenated control-card DD (`SYSIN`, `SYSTSIN`, …) is read as one stream, every
  member in order.

`bind_cobol_artifacts` gives a file whose ddname is concatenated **`datasets`**, the list
in read order, in place of `dataset`. See `examples/concat.jcl`.

## The one place it meets the COBOL tool

`bind_cobol_artifacts(manifest, jobs)` joins a COBOL program's file ddnames to the
datasets a job binds. It takes a plain **dict** and returns one, so this package imports
nothing from the COBOL side. `tests/fixtures/sqlunld.artifacts.json` is a committed copy
of a real COBOL manifest — this repository's half of that contract, so its tests need no
COBOL install. The COBOL repository regenerates it and fails if the shape drifts, which
is how a schema change is caught before release rather than after.

`BIND_API_VERSION` in `jcl_dependencies/__init__.py` is the contract version. The COBOL
side checks it at import time, because a skewed pair fails invisibly otherwise: an
unbound manifest looks fine, since its file rows say exactly what an unbound run's rows
say.

## Development

```bash
# mainframe-artifacts comes from a sibling mainframe-common checkout (or the git+ line above)
python -m pip install -e ../mainframe-common/mainframe-artifacts -e .
python -m pytest -q
python tools/byteproof.py --check goldens/views.sha256   # byte-stability ratchet
```

From a bare dual-checkout - mainframe-common beside this repo, nothing installed - the
suite and the ratchet find `../mainframe-common/mainframe-artifacts` automatically
(override with `MAINFRAME_COMMON_REPO`); without either, the suite ends as one clean
skip naming the exact pip command.

Output is byte-stable and deterministic: a refactor that should not change output must
produce identical bytes, and a green test run does not prove that. The ratchet hashes
every view of every example under two `PYTHONHASHSEED` values. Re-record only when an
output change is intended and reviewed.
