"""The JCL front-end as a library: analyze a job, get its dataflow and its dependencies.

The same shape as the COBOL side's :mod:`cobol_xstate.api`, and for the same reason:
driving this from another Python program should be the code path the command line takes,
not a second one that drifts from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

from mainframe_artifacts.bundle import EstateBundle, recording_fetcher, write_bundle
from mainframe_artifacts.fetch import fetch_dependencies
from mainframe_artifacts.prefetch import PrefetchResult
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.synonyms import SynonymLookup

from . import PRODUCER
from .parser import Job, parse_jcl
from .prefetch import prefetch_jcl
from .views import bind_cobol_artifacts, build_jcl_artifacts, build_jcl_lineage

_log = logging.getLogger(__name__)


@dataclass
class JobAnalysis:
    """One analyzed JCL job or PROC, and the views projected from it."""

    job: Job
    prefetch: PrefetchResult
    source_name: str = "<jcl>"
    fetch: Optional[dict] = None
    #: Db2 SYNONYM/ALIAS knowledge (a map, a host resolver, or both) - None when the
    #: run opened neither door, and then every table is reported as written.
    synonyms: Optional[SynonymLookup] = None

    _lineage: Optional[dict] = field(default=None, repr=False)
    _artifacts: Optional[dict] = field(default=None, repr=False)

    def lineage(self) -> dict:
        """Step-to-step dataset dataflow, plus control-card byte-field lineage."""
        if self._lineage is None:
            self._lineage = build_jcl_lineage(self.job)
        return self._lineage

    def artifacts(self) -> dict:
        """Every dataset, program, PROC, INCLUDE member, control card, and Db2 table this
        job names."""
        if self._artifacts is None:
            self._artifacts = build_jcl_artifacts(self.job, synonyms=self.synonyms)
        return self._artifacts

    def bind(self, cobol_manifest: dict) -> dict:
        """Close the ddname->dataset join on a COBOL program's artifact manifest.

        Takes a plain dict, and returns one - this package never imports the COBOL one.
        """
        return bind_cobol_artifacts(cobol_manifest, [self.job])


def analyze(source: str, *, source_name: str = "<jcl>",
            bundle: Optional[EstateBundle] = None,
            fetcher: Optional[Any] = None,
            retrieve: bool = True,
            paths: Sequence[str] = (), dest: Optional[str] = None,
            unavailable: Optional[str] = None,
            max_rounds: int = 12, jobs: int = 1,
            timer: Optional[StageTimer] = None,
            synonyms: Optional[Dict[str, str]] = None,
            synonym_resolver: Optional[Callable[[str], Optional[str]]] = None,
            ) -> JobAnalysis:
    """Retrieve, parse and model one JCL job or PROC.

    ``synonyms`` (a ``{"SYNONYM": "BASE"}`` map) and ``synonym_resolver`` (a
    ``(name) -> base | None`` callable the host supplies, see
    ``mainframe_artifacts.protocol.SynonymResolver``) are the two doors Db2 catalog
    knowledge arrives by; the map answers first. With either open, a ``db2-table``
    artifact row written under an alias also names its base table. Neither is a
    default: a table stays as written, never guessed.

    Stage 1 is not optional decoration here. A cataloged PROC, an INCLUDE member and a
    control-card dataset each carry ``EXEC PGM=`` steps and DD statements that appear
    nowhere in the JCL file itself; parsed without them, those steps do not show up as
    programs, as datasets, or at all - and the job reads as far simpler than it is.

    The estate is reached the same four ways as on the COBOL side: through ``fetcher``,
    not at all (``fetcher=None``), deliberately off (``retrieve=False``), or replayed
    from a gathered ``bundle``.
    """
    timer = timer or StageTimer(_log, False, source_name)

    if bundle is not None:
        fetcher = bundle.fetcher()
        unavailable = unavailable or bundle.unavailable
    elif not retrieve:
        fetcher = None
        unavailable = unavailable or ("retrieval was disabled for this run, so this "
                                      "member was never looked for")

    with timer.stage("prefetch"):
        pre = prefetch_jcl(source, fetcher, paths=list(paths), dest=dest,
                           source_name=source_name, unavailable=unavailable,
                           max_rounds=max_rounds, jobs=jobs, producer=PRODUCER)
    with timer.stage("parse"):
        job = parse_jcl(source, resolver=pre.resolver(), source_name=source_name)

    lookup = (SynonymLookup(synonyms, synonym_resolver)
              if (synonyms or synonym_resolver is not None) else None)
    analysis = JobAnalysis(job=job, prefetch=pre, source_name=source_name,
                           synonyms=lookup)
    with timer.stage("jcl-lineage"):
        analysis.lineage()
    with timer.stage("jcl-artifacts"):
        art = analysis.artifacts()
    with timer.stage("fetch"):
        analysis.fetch = fetch_dependencies(art, fetcher, dest=dest,
                                            prefetched=pre.store,
                                            unavailable=unavailable, jobs=jobs,
                                            producer=PRODUCER)
    return analysis


def gather(source: str, *, source_name: str = "<jcl>",
           fetcher: Optional[Any] = None,
           paths: Sequence[str] = (), dest: str,
           unavailable: Optional[str] = None,
           max_rounds: int = 12, jobs: int = 1) -> str:
    """Run the retrieval half where the estate is reachable; return the bundle manifest."""
    recorder, answers = recording_fetcher(fetcher) if fetcher is not None else (None, [])
    analysis = analyze(source, source_name=source_name, fetcher=recorder, paths=paths,
                       dest=dest, unavailable=unavailable, max_rounds=max_rounds,
                       jobs=jobs)
    return write_bundle(dest, subject_name=source_name, subject_text=source,
                        kind="jcl", prefetch=analysis.prefetch, answers=answers,
                        fetch=analysis.fetch)
