"""Stage 1 for JCL: close over the PROCs, INCLUDE members and control cards a job needs.

**Discovered by record-and-replay, not by scanning.** Parse the job with a resolver that
fetches nothing and merely records what it was asked for, retrieve those, then re-parse
with the retrieved members in hand - repeating until the parse stops asking for anything
new.

It would have been easy to write a lexical scanner for ``EXEC PROC=`` and
``INCLUDE MEMBER=`` here, and it would have been wrong: a PROC name can arrive through a
symbolic parameter, an INCLUDE can be nested inside an expanded PROC body, and a
control-card DSN can be built from a JCL symbol. Only the JCL parser resolves symbols and
folds continuations correctly, and it already funnels every external member it needs
through one call (``parser._Parser._resolve``). Replaying that parse asks exactly the
right questions; a scanner would ask approximately the right ones.

That is also the contract this file depends on, and it is easy to break from the other
side: anything that memoizes resolution inside the parser, short-circuits when the
resolver returns ``None``, or adds a second resolution path will silently shorten the
closure - no error, just a job that reads as though it had fewer steps than it runs.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional

from cobol_xstate_core.prefetch import (PrefetchResult, Prefetcher,  # noqa: F401
                                        member_key)

from .parser import parse_jcl


def prefetch_jcl(source: str, fetcher: Optional[Callable],
                 paths: Optional[List[str]] = None, dest: Optional[str] = None,
                 source_name: str = "<jcl>", max_rounds: int = 12,
                 unavailable: Optional[str] = None,
                 result: Optional[PrefetchResult] = None,
                 jobs: int = 1,
                 seen: Optional[Iterable[str]] = None) -> PrefetchResult:
    """Close over the cataloged PROCs, ``INCLUDE`` members and control-card datasets a
    job needs, by replaying the parse until it stops asking for members it has not got.

    No type hint is passed: the estate service auto-detects, and its ``detected_type`` is
    a better answer than anything we could infer from the DD that referenced the member.
    """
    pf = Prefetcher(fetcher, paths, dest, unavailable, result, seen=seen)
    pf.name_source(source_name)

    for _ in range(max_rounds):
        asked: List[str] = []

        def recording(name: str, _asked=asked) -> Optional[str]:
            _asked.append(name)
            return pf.store_text(name) or pf.result.resolver()(name)

        parse_jcl(source, resolver=recording, source_name=source_name)
        fresh = [n for n in asked if member_key(n) not in pf.seen]
        if not fresh:
            break
        # One round IS a level: everything the parse asked for this time round was asked
        # for before any of it came back, so it can all be retrieved together.
        pf.obtain_wave(
            [(n, "referenced by the job (PROC / INCLUDE / control card)") for n in fresh],
            None, jobs)
    else:
        pf.note_closure_bound(max_rounds)
    return pf.result
