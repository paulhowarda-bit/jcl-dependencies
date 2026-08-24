#!/usr/bin/env python3
"""Byte-stability ratchet: hash every view of every example, and refuse to let a
refactor change one byte of it.

Output here is a contract, not a rendering: a JCL lineage table and artifact manifest are
read, diffed and joined against a COBOL program's manifest. A refactor that should not
change output must produce identical bytes, and a green test run does not prove that.

What is hashed is the EXACT TEXT the CLI would write - ``json.dumps(obj, indent=2) +
"\\n"`` - not a normalized or re-parsed form. A view that reorders its keys, changes its
indent, or gains a trailing newline is a changed view, and this must say so.

Deliberately estate-free: every job is parsed with NO resolver, so an unresolved PROC
stays unresolved and is hashed as such. Supplying one would make the hashes depend on
what an estate happened to answer.

    python tools/byteproof.py --record goldens/views.sha256
    python tools/byteproof.py --check  goldens/views.sha256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))     # the recorded fake estate client

# mainframe-artifacts arrives installed or from the sibling mainframe-common checkout
# (override with MAINFRAME_COMMON_REPO) - the same discovery the test suite performs.
from _mainframe_common import ensure_on_path                        # noqa: E402
_missing = ensure_on_path()
if _missing is not None:
    raise SystemExit(f"error: {_missing}")

from fakes.estate import fetch_artifact                             # noqa: E402

from mainframe_artifacts.fetch import fetch_dependencies              # noqa: E402

from jcl_dependencies.parser import parse_jcl                       # noqa: E402
from jcl_dependencies.prefetch import prefetch_jcl                  # noqa: E402
from jcl_dependencies.views import (build_jcl_artifacts,            # noqa: E402
                                    build_jcl_lineage)

INDENT = 2  # the CLI default; the hashes are of what a default run would write


def normalize(text: str, run_dir: Path = None) -> str:
    """Replace this checkout's directories - and a run's own output directory - with
    stable tokens, so goldens are portable. `copiedTo` in the retrieval reports names
    the run's deps/ directory; a locally-resolved member's `source` names a checkout
    path. Both are machine-dependent before this tool touches them; nothing else is
    normalized."""
    roots = ([(run_dir, "<RUNDIR>")] if run_dir else []) + \
        [(EXAMPLES, "<EXAMPLES>"), (REPO, "<REPO>")]
    for root, token in roots:
        for form in (str(root), str(root).replace("\\", "/"),
                     str(root).replace("\\", "\\\\")):
            text = text.replace(form, token)
    return text


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def json_text(obj) -> str:
    return json.dumps(obj, indent=INDENT) + "\n"


def guarded(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception:                                    # noqa: BLE001 - deliberate
        # Recorded rather than allowed to vanish: a view that stops being produced is
        # exactly the kind of silent loss this exists to catch.
        return "ERROR:\n" + traceback.format_exc(limit=0)


def views(path: Path) -> Dict[str, str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    job = parse_jcl(source, resolver=None, source_name=path.name)
    return {
        "jcl.lineage": guarded(lambda: json_text(build_jcl_lineage(job))),
        "jcl.artifacts": guarded(lambda: json_text(build_jcl_artifacts(job))),
    }


def reports(path: Path, run_dir: Path) -> Dict[str, str]:
    """Both RETRIEVAL reports for one job, run against the recorded fake estate.

    The views above are estate-free; these two files are not - their contents depend on
    what an artifact service answered, so they are exercised against
    ``tests/fakes/estate.py``, which answers from a fixed table. This is the half of
    the ratchet that guards the record-and-replay closure (prefetch_jcl) and the fetch
    plan's row vocabulary - the machinery a refactor is most likely to shorten
    silently.
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    deps = str(run_dir / "deps")
    pre = prefetch_jcl(source, fetch_artifact, paths=[str(EXAMPLES)], dest=deps,
                       source_name=path.name, jobs=1)
    job = parse_jcl(source, resolver=pre.resolver(), source_name=path.name)
    art = build_jcl_artifacts(job)
    fetched = fetch_dependencies(art, fetch_artifact, dest=deps, prefetched=pre.store,
                                 jobs=1)
    return {
        "jcl.prefetch": guarded(lambda: json_text(pre.report())),
        "jcl.fetch": guarded(lambda: json_text(fetched)),
    }


def build_manifest() -> Dict[str, str]:
    import shutil
    import tempfile
    out: Dict[str, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="jcl-byteproof-"))
    try:
        for src in sorted(EXAMPLES.iterdir()):
            if src.suffix.lower() not in (".jcl", ".prc", ".proc"):
                continue
            for view, text in views(src).items():
                out[f"{src.name}::{view}"] = digest(text)
            run_dir = tmp / src.stem
            for view, text in reports(src, run_dir).items():
                out[f"{src.name}::{view}"] = hashlib.sha256(
                    normalize(text, run_dir).encode("utf-8")).hexdigest()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dict(sorted(out.items()))


def dump(path: Path, manifest: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{sha}  {key}" for key, sha in manifest.items()) + "\n",
                    encoding="utf-8")


def load(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sha, _, key = line.partition("  ")
            out[key] = sha
    return out


def compare(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    problems = []
    for key in sorted(set(before) - set(after)):
        problems.append(f"MISSING  {key} (was in the goldens, not produced now)")
    for key in sorted(set(after) - set(before)):
        problems.append(f"ADDED    {key} (produced now, not in the goldens)")
    for key in sorted(set(before) & set(after)):
        if before[key] != after[key]:
            problems.append(f"CHANGED  {key}\n           golden {before[key]}\n"
                            f"           now    {after[key]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="byte-stability ratchet over every view of "
                                             "every JCL example")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", metavar="FILE")
    g.add_argument("--check", metavar="FILE")
    args = ap.parse_args()

    manifest = build_manifest()
    if args.record:
        dump(Path(args.record), manifest)
        print(f"recorded {len(manifest)} view digests -> {args.record}")
        return 0

    path = Path(args.check)
    if not path.exists():
        print(f"error: no goldens at {path} - run --record first", file=sys.stderr)
        return 2
    problems = compare(load(path), manifest)
    if problems:
        print(f"BYTE-STABILITY FAILURE: {len(problems)} difference(s)\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"byte-stable: {len(manifest)} view digests match {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
