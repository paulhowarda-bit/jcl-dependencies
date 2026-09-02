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
repo, two distributions; its `mainframe-artifacts/` subdirectory). Until it is on an index,
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
