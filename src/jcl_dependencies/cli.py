"""Command-line entry point: a JCL job or PROC -> its dataflow and its dependencies."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from mainframe_artifacts.artifact_service import decode_member, load_fetcher
from mainframe_artifacts.bundle import open_bundle
from mainframe_artifacts.cliargs import (add_logging_args, add_output_args,
                                       add_dependents_args, add_retrieval_args,
                                       add_synonym_args, dependents_lookup,
                                       jobs as _jobs, synonym_lookup)
from mainframe_artifacts.detect import looks_like_jcl
from mainframe_artifacts.errors import CobolXstateError
from mainframe_artifacts.logging_setup import PACKAGE_LOGGER as CORE_LOGGER
from mainframe_artifacts.logging_setup import configure_logging
from mainframe_artifacts.output import make_run_dir, run_dir, write_json
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.report import report_stages

from . import PACKAGE_LOGGER
from .api import analyze, gather

# Explicit name, NOT __name__: this module is also run as `python -m jcl_dependencies.cli`,
# where __name__ == "__main__" would put the logger outside the package hierarchy and out
# of configure_logging's reach (so INFO/progress would be silently dropped).
_log = logging.getLogger("jcl_dependencies.cli")

# The dependents view is not a --target choice: it is not a view you ask for, it is an
# answer you were given, so it is written exactly when a lookup supplied one.
_SUFFIXES = (".jcl.artifacts.json", ".jcl.lineage.json", ".jcl.dependents.json",
             ".jcl.prefetch.json", ".jcl.fetch.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jcl-dependencies",
        description="Parse a JCL job or PROC and emit its dataset dataflow, its "
                    "control-card field lineage, and the manifest of everything it "
                    "depends on - following cataloged PROCs, INCLUDE members and "
                    "control-card datasets so the steps inside them are in the model.",
    )
    p.add_argument("source", help="path to a JCL / PROC file ('-' for stdin)")
    p.add_argument("--target", choices=["both", "artifacts", "lineage"], default="both",
                   help="which views to write (default: both). lineage = the step-to-step "
                        "dataset dataflow and control-card field lineage; artifacts = the "
                        "manifest of datasets, programs, PROCs and INCLUDE members the "
                        "job names.")
    p.add_argument("-I", "--path", "--copybook-path", dest="path", action="append",
                   default=[], metavar="DIR",
                   help="directory to search for PROCs / INCLUDE members / control cards "
                        "before asking the estate (repeatable)")
    p.add_argument("--max-rounds", type=int, default=12, metavar="N",
                   help="how deep to follow PROC/INCLUDE nesting when closing over the "
                        "job (default: 12). The closure is bounded because a member set "
                        "deeper than this is more likely a resolver loop than a real job; "
                        "hitting the bound is REPORTED, never silently treated as a "
                        "complete closure.")
    add_retrieval_args(p)
    # Db2 catalog knowledge: a LOAD / UNLOAD / SQL control card written under a
    # SYNONYM/ALIAS names the alias; the base table it stands for lives in the catalog.
    add_synonym_args(p)
    add_dependents_args(p)
    add_output_args(p, outdir_help=(
        "directory for output (default: ./out). EVERY file this run produces goes here, "
        "exactly as given with nothing appended - both views, both retrieval reports, and "
        "the members retrieved from the estate (under deps/). Created with parents if it "
        "does not exist."))
    add_logging_args(p)
    return p


def _service(args, source_name: str):
    """The estate artifact service for this run, and why it is missing if it is.

    Never fatal. A run without the service still parses whatever is on the local search
    path and still writes its reports - they simply say, per member, that nothing was
    ever looked for."""
    fetcher, why = load_fetcher(args.copybook_fetcher)
    if fetcher is None:
        _log.warning(f"[{source_name}] WARNING: {why}")
    return fetcher, why


def run(argv: Optional[List[str]] = None, timing_sink=None) -> int:
    """Parse args, configure logging, and dispatch, behind the top-level error boundary."""
    args = build_parser().parse_args(argv)
    # BOTH roots: retrieval logs from mainframe_artifacts.*, everything else from
    # jcl_dependencies.*. A root nobody configures propagates to the root logger, or
    # prints WARNING+ via logging's lastResort - which would end -qq's silence.
    configure_logging(verbose=args.verbose or (1 if args.debug else 0), quiet=args.quiet,
                      loggers=(CORE_LOGGER, PACKAGE_LOGGER))
    try:
        return _run(args, timing_sink=timing_sink)
    except CobolXstateError as exc:
        _log.error("%s", exc)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        _log.error("interrupted")
        return 130
    except Exception:
        if args.debug:
            raise
        _log.critical("internal error while processing %r - re-run with --debug for the "
                      "full traceback", args.source)
        _log.debug("internal error traceback", exc_info=True)
        return 1


def _run(args, timing_sink=None) -> int:
    paths = list(args.path)
    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
        default_stem = None
    else:
        path = Path(args.source)
        if not path.exists():
            _log.error(f"error: no such file: {path}")
            return 2
        source = decode_member(path.read_bytes())
        source_name = path.name
        default_stem = path.stem
        # A cataloged PROC or INCLUDE member most often sits beside the job itself.
        paths.append(str(path.parent))

    if not looks_like_jcl(source_name, source):
        _log.warning(f"[{source_name}] WARNING: this does not look like JCL (no //NAME "
                     f"JOB or //NAME PROC statement). If it is COBOL, use cobol-xstate.")

    timer = StageTimer(_log, args.timing, source_name, sink=timing_sink)

    if args.gather_only and args.from_bundle:
        _log.error("error: --gather-only writes a bundle and --from-bundle reads one; "
                   "they cannot both apply to a single run")
        return 2

    bundle = None
    if args.from_bundle:
        try:
            bundle = open_bundle(args.from_bundle)
        except CobolXstateError as exc:
            _log.error(f"error: {exc}")
            return 2

    lookup, why_synonyms = synonym_lookup(args)
    if why_synonyms:
        _log.error(f"error: {why_synonyms}")
        return 2

    fetcher, why = (None, None) if bundle is not None else _service(args, source_name)

    out_dir = run_dir(args.outdir)
    err = make_run_dir(out_dir)
    if err:
        _log.error(f"error: {err}")
        return 2
    deps = str(out_dir / "deps")

    # Before the gather branch, not after: --gather-only is the run that happens where the
    # INDEX is reachable, so it is the run that has to ask it. Built later, the door was
    # silently dropped from every bundle - which a modelling box has no way to notice.
    reverse, why_dependents = dependents_lookup(args)
    if why_dependents:
        _log.error("error: {0}".format(why_dependents))
        return 2

    if args.gather_only:
        gathered = gather(source, source_name=source_name, fetcher=fetcher, paths=paths,
                          dest=args.gather_only, unavailable=why,
                          max_rounds=args.max_rounds, jobs=_jobs(args),
                          dependents=reverse.mapping if reverse is not None else None,
                          dependents_resolver=(reverse.resolver if reverse is not None
                                               else None))
        _log.info(f"[{source_name}] wrote estate bundle {gathered}")
        _log.info(f"[{source_name}] model from it with: --from-bundle {args.gather_only}")
        timer.report()
        return 0

    analysis = analyze(source, source_name=source_name, bundle=bundle, fetcher=fetcher,
                       retrieve=not args.no_fetch, paths=paths, dest=deps,
                       unavailable=why, max_rounds=args.max_rounds, jobs=_jobs(args),
                       timer=timer,
                       synonyms=lookup.mapping if lookup is not None else None,
                       synonym_resolver=lookup.resolver if lookup is not None else None,
                       dependents=reverse.mapping if reverse is not None else None,
                       dependents_resolver=(reverse.resolver if reverse is not None
                                            else None))
    job = analysis.job
    base = default_stem or job.name or "job"

    wanted = {"both": ("artifacts", "lineage")}.get(args.target, (args.target,))
    written = {
        ".jcl.artifacts.json": analysis.artifacts() if "artifacts" in wanted else None,
        ".jcl.lineage.json": analysis.lineage() if "lineage" in wanted else None,
        # None when no door was opened, and the loop below writes nothing for a None.
        ".jcl.dependents.json": analysis.dependents(),
        ".jcl.prefetch.json": analysis.prefetch.report(),
        ".jcl.fetch.json": analysis.fetch,
    }
    for suffix in _SUFFIXES:
        obj = written.get(suffix)
        if obj is None:
            continue
        path = out_dir / f"{base}{suffix}"
        write_json(path, obj, args.indent)
        _log.info(f"[{source_name}] wrote {path}")
    report_stages(_log, source_name, analysis.prefetch, analysis.fetch)

    if args.summary:
        lineage = analysis.lineage()
        _log.info(f"[{job.name or 'JOB'}] {len(job.steps)} step(s), "
                  f"{len(lineage['datasets'])} dataset(s), "
                  f"{len(lineage['dataflow'])} dataflow edge(s), "
                  f"{len(lineage['fieldLineage'])} field-lineage step(s), "
                  f"{len(job.flags)} flag(s)")
        for f in job.flags:
            _log.info(f"  FLAG {f}")

    timer.report()
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
