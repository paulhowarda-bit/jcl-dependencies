"""JCL / PROC parser and model.

The COBOL side of this tool recovers what a program *does*; it cannot recover what it does
it *to*, because the binding ``ddname -> dataset`` is finished outside the program, in JCL
(see docs/mainframe-artifacts.md). This module reads the JCL itself: it parses a job (or a
PROC), resolves symbolic parameters, expands PROCs, substitutes the control files a step
reads where a caller-provided function can retrieve them, and produces a structured
``Job`` from which two views are built (see jcl_views.py):

  * **lineage** - the dataflow across steps (which step produces each dataset, which
    consume it, under which condition), plus real byte-field lineage where a utility
    control card (SORT/IDCAMS/IEBGENER) defines how output record fields are built from
    input fields;
  * **artifacts** - the dependency manifest (datasets, programs, PROCs, control-card and
    INCLUDE members) in the same shape as the COBOL artifact manifest.

**The resolver.** Cataloged PROCs, ``INCLUDE`` members, and control-card datasets
(``//SYSIN DD DSN=PARM.LIB(SORTCRD)``) live outside the JCL file. This module does NOT
fetch them - the caller passes ``resolver``, a function ``resolver(name) -> text | None``,
and this module calls it and substitutes what it returns. Anything the resolver cannot
return is **flagged, never guessed** - the same rule the COBOL side follows for an
unresolved ``CALL`` or a missing copybook.

**Honest limits, all surfaced in flags rather than guessed** (the hazards are enumerated in
docs/mainframe-artifacts.md; this parser handles the common cases and flags the rest):
symbolic parameters it cannot resolve are left visible and flagged; ``OLD``/``I-O`` DISP is
direction-ambiguous and noted; dynamic allocation (SVC 99, ``BPXWDYN``), scheduler-set
symbolics, and ``DDNAME=`` referbacks are not statically knowable and are flagged; GDG
relative generations are normalized to their base (the stable identity) with the generation
recorded.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

Resolver = Callable[[str], Optional[str]]


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #

@dataclass
class DDSegment:
    """One dataset in a DD (a DD may concatenate several)."""
    dsn: Optional[str] = None            # DSN as written, after symbolic substitution
    disp: List[str] = field(default_factory=list)   # [status, normal, abnormal]
    sysout: Optional[str] = None
    instream: bool = False               # DD * / DD DATA
    dummy: bool = False
    member: Optional[str] = None         # DSN(MEMBER)
    gdg: Optional[str] = None            # (+1) / (0) / (-1) relative generation
    unresolved_symbols: List[str] = field(default_factory=list)
    raw: str = ""

@dataclass
class DD:
    ddname: str
    segments: List[DDSegment] = field(default_factory=list)
    instream_lines: List[str] = field(default_factory=list)  # captured control cards
    control: Optional[dict] = None       # parsed control-card summary, if any
    override: bool = False               # a PROC-step DD override (//STEP.DD ...)


@dataclass
class Step:
    name: str
    pgm: Optional[str] = None            # EXEC PGM=
    proc: Optional[str] = None           # EXEC PROC=name / EXEC name
    proc_resolved: Optional[bool] = None # whether the PROC body was expanded
    from_proc: Optional[str] = None      # the PROC this step was expanded from
    proc_step: Optional[str] = None      # the PROC's own step name
    cond: Optional[str] = None           # COND= text (verbatim; notoriously back-to-front)
    cond_parsed: Optional[dict] = None   # structured COND= with its run-sense spelt out
    # IF/THEN/ELSE conditions governing this step, outermost first. Each is
    # {expr, negated}: the step runs when every expr holds in its stated polarity
    # (a step in an ELSE branch carries the IF's expr with negated=True).
    conditions: List[dict] = field(default_factory=list)
    parm: Optional[str] = None
    dds: List[DD] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class ProcDef:
    name: str
    defaults: Dict[str, str] = field(default_factory=dict)
    lines: List[str] = field(default_factory=list)   # raw logical statements of the body


@dataclass
class Job:
    name: str
    source_name: str = "<jcl>"
    is_proc: bool = False                # a bare PROC member, not a JOB
    steps: List[Step] = field(default_factory=list)
    symbols: Dict[str, str] = field(default_factory=dict)   # SET values (job scope)
    procs: Dict[str, ProcDef] = field(default_factory=dict)
    includes: List[str] = field(default_factory=list)
    jcllib: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# physical-line handling: continuations and instream data
# --------------------------------------------------------------------------- #

# A JCL statement: //name operation operands. Continued when the operand field ends in a
# comma and the next // line has a blank name field. Comments are //* ; the null statement
# is // alone; /* ends instream data.
_STMT = re.compile(r"^//(\S*)\s+(\S+)(?:\s+(.*))?$")
_CONT = re.compile(r"^//\s+(\S.*)$")           # blank name field -> continuation/override-less
_COMMENT = re.compile(r"^//\*")


@dataclass
class _LogLine:
    name: str
    op: str
    operands: str
    raw: str


def _gather(physical: List[str]) -> Tuple[List[object], List[str]]:
    """Return (items, flags). Each item is either a ``_LogLine`` (a statement) or a tuple
    ``("data", ddname_owner_index, [lines])`` is handled inline instead - here we return
    _LogLine items and attach instream data to the DD as we parse, so this only merges
    continuations. Instream capture is done by the caller via ``_split_data``."""
    # Kept simple: this function only stitches continuation lines into whole statements.
    out: List[_LogLine] = []
    flags: List[str] = []
    i = 0
    n = len(physical)
    while i < n:
        line = physical[i].rstrip("\n").rstrip()
        if not line:
            i += 1
            continue
        if _COMMENT.match(line):
            i += 1
            continue
        m = _STMT.match(line)
        if not m:
            i += 1
            continue
        name, op, operands = m.group(1), m.group(2), (m.group(3) or "")
        raw_parts = [line]
        # merge continuations: while operands end with a comma (ignoring trailing
        # inline comment), the following blank-name // lines continue the operand field.
        while operands.rstrip().endswith(","):
            j = i + 1
            while j < n and _COMMENT.match(physical[j].rstrip()):
                j += 1
            if j >= n:
                break
            cont = _CONT.match(physical[j].rstrip("\n").rstrip())
            if not cont:
                break
            operands = operands.rstrip() + cont.group(1).strip()
            raw_parts.append(physical[j].rstrip())
            i = j
        out.append(_LogLine(name=name.upper(), op=op.upper(), operands=operands,
                            raw="\n".join(raw_parts)))
        i += 1
    return out, flags


# --------------------------------------------------------------------------- #
# operand tokenising (comma-split respecting parens and quotes)
# --------------------------------------------------------------------------- #

def _split_operands(text: str) -> List[str]:
    """Split ``A=1,B=(X,Y),C='a,b'`` on top-level commas only."""
    out: List[str] = []
    depth = 0
    quote = False
    cur = []
    for ch in text:
        if ch == "'":
            quote = not quote
            cur.append(ch)
        elif quote:
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [o.strip() for o in out if o.strip()]


def _operand_map(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Return (keyword operands, positional operands)."""
    kw: Dict[str, str] = {}
    pos: List[str] = []
    for tok in _split_operands(text):
        if "=" in tok and not tok.startswith("("):
            k, v = tok.split("=", 1)
            kw[k.strip().upper()] = v.strip()
        else:
            pos.append(tok.strip())
    return kw, pos


def _paren_list(v: str) -> List[str]:
    """``(NEW,CATLG,DELETE)`` -> ['NEW','CATLG','DELETE']; a bare word -> [word]."""
    v = v.strip()
    if v.startswith("(") and v.endswith(")"):
        return _split_operands(v[1:-1])
    return [v]


# --------------------------------------------------------------------------- #
# symbolic substitution
# --------------------------------------------------------------------------- #

_SYMREF = re.compile(r"&([A-Z0-9#@$]+)\.?", re.I)


def _substitute(text: str, symbols: Dict[str, str]) -> Tuple[str, List[str]]:
    """Substitute ``&SYM`` / ``&SYM.`` from ``symbols``. Returns (text, unresolved names).
    An unresolved symbol is LEFT VISIBLE (``&SYM``) and reported, never blanked - a wrong
    DSN silently is far worse than an obviously-unresolved one."""
    unresolved: List[str] = []

    def repl(m: "re.Match") -> str:
        name = m.group(1).upper()
        if name in symbols:
            return symbols[name]
        if name not in unresolved:
            unresolved.append(name)
        return m.group(0)

    # `&&` is a temporary-dataset marker, not a symbolic - protect it.
    text = text.replace("&&", "\x00")
    out = _SYMREF.sub(repl, text)
    out = out.replace("\x00", "&&")
    return out, unresolved


# --------------------------------------------------------------------------- #
# DD parsing
# --------------------------------------------------------------------------- #

_GDG = re.compile(r"\((\+\d+|-\d+|0)\)\s*$")


def _parse_dd_segment(operands: str, symbols: Dict[str, str]) -> DDSegment:
    seg = DDSegment(raw=operands)
    kw, pos = _operand_map(operands)
    if "DUMMY" in (p.upper() for p in pos):
        seg.dummy = True
    if "*" in pos or "DATA" in (p.upper() for p in pos):
        seg.instream = True
    if "SYSOUT" in kw:
        seg.sysout = kw["SYSOUT"]
    dsn = kw.get("DSN") or kw.get("DSNAME")
    if dsn:
        dsn, unresolved = _substitute(dsn, symbols)
        seg.unresolved_symbols = unresolved
        # member: DSN(MEMBER) where MEMBER is not a GDG generation
        gm = _GDG.search(dsn)
        if gm:
            seg.gdg = gm.group(1)
            dsn = _GDG.sub("", dsn).strip()
        else:
            mm = re.search(r"\(([A-Z0-9#@$]+)\)\s*$", dsn, re.I)
            if mm:
                seg.member = mm.group(1).upper()
                dsn = re.sub(r"\([A-Z0-9#@$]+\)\s*$", "", dsn, flags=re.I).strip()
        seg.dsn = dsn.upper()
    if "DISP" in kw:
        seg.disp = [d.upper() for d in _paren_list(kw["DISP"])]
    return seg


def _merge_dd_segment(base: DDSegment, ov: DDSegment) -> DDSegment:
    """A PROC-DD override overlaid on the PROC's DD: every parameter the override names
    wins; every one it omits is inherited from the base. This is JCL's rule, and it is
    why a `//STEP.OUT DD DSN=REAL.NAME` override keeps the PROC DD's DISP (and therefore
    its input/output direction) instead of nulling it.

    DUMMY and instream are treated as dataset REPLACEMENTS: naming either on the override
    supersedes the base dataset outright.
    """
    if ov.dummy or ov.instream:
        return ov
    return DDSegment(
        dsn=ov.dsn if ov.dsn is not None else base.dsn,
        disp=ov.disp if ov.disp else base.disp,
        sysout=ov.sysout if ov.sysout is not None else base.sysout,
        instream=base.instream,
        dummy=base.dummy,
        member=ov.member if ov.member is not None else base.member,
        gdg=ov.gdg if ov.gdg is not None else base.gdg,
        unresolved_symbols=ov.unresolved_symbols or base.unresolved_symbols,
        raw=f"{base.raw} | override: {ov.raw}",
    )


def _dd_direction(seg: DDSegment) -> Optional[str]:
    """'input' / 'output' / 'inout' / None for a single segment, from DISP / SYSOUT /
    instream. DISP status is the primary signal; OLD/I-O are ambiguous and noted by the
    caller via a flag."""
    if seg.dummy:
        return None
    if seg.sysout is not None:
        return "output"
    if seg.instream:
        return "input"
    status = seg.disp[0] if seg.disp else ""
    if status == "NEW":
        return "output"
    if status == "MOD":
        return "output"      # append; still a producer edge
    if status in ("SHR", "OLD"):
        return "input"
    # No DISP and a DSN: default DISP is (NEW) for a new dataset, but that is a guess;
    # treat as unknown so the caller can flag it rather than assert a direction.
    return None


# --------------------------------------------------------------------------- #
# step conditions: COND= and IF/THEN/ELSE
# --------------------------------------------------------------------------- #

def _parse_cond(text: str) -> dict:
    """Parse a ``COND=`` value into a structure whose RUN sense is spelt out.

    COND is the notorious back-to-front one (docs/mainframe-artifacts.md): each test says
    when to *skip* the step - ``COND=(4,LT)`` bypasses the step if 4 is less than any
    preceding step's return code. Presenting only the raw text invites the classic
    misreading, so the structure states both directions: ``bypassedWhen`` (the literal
    semantics) and ``runsWhen`` (the negation a reader actually wants). EVEN/ONLY are the
    abend modifiers. An unrecognized form is kept raw and marked, never guessed."""
    out: dict = {"raw": text, "sense": "bypass-when-true", "tests": []}
    items = _paren_list(text.strip())

    def add_test(parts: List[str]) -> None:
        test: dict = {"code": int(parts[0])}
        if len(parts) > 1:
            test["op"] = parts[1].strip().upper()
        if len(parts) > 2:
            test["step"] = parts[2].strip().upper()
        out["tests"].append(test)

    if items and items[0].strip().isdigit():
        add_test(items)                              # COND=(code,op[,step])
    else:
        for it in items:
            u = it.strip().upper()
            if u == "EVEN":
                out["even"] = True
            elif u == "ONLY":
                out["only"] = True
            elif it.strip().startswith("("):
                sub = _paren_list(it)
                if sub and sub[0].strip().isdigit():
                    add_test(sub)
                else:
                    out.setdefault("unparsed", []).append(it)
            elif u:
                out.setdefault("unparsed", []).append(it)

    bypass = [f"{t['code']} {t.get('op', '?')} "
              f"{('RC of ' + t['step']) if t.get('step') else 'the RC of any preceding step'}"
              for t in out["tests"]]
    if bypass:
        out["bypassedWhen"] = " OR ".join(bypass)
        run = f"runs unless {out['bypassedWhen']}"
    else:
        run = "runs"
    if out.get("only"):
        run += "; ONLY after a preceding step abended"
    elif out.get("even"):
        run += "; even after a preceding step abended"
    out["runsWhen"] = run
    if out.get("unparsed"):
        out["note"] = "unrecognized COND form kept raw - verify against the JCL Reference"
    return out


# --------------------------------------------------------------------------- #
# control-card parsing (utility programs)
# --------------------------------------------------------------------------- #

_SORT_UTILS = ("SORT", "MERGE", "ICEMAN", "DFSORT", "SYNCSORT")


def _classify_utility(pgm: Optional[str], lines: List[str]) -> Optional[str]:
    body = " ".join(lines).upper()
    if pgm and pgm.upper() in ("IDCAMS",):
        return "idcams"
    if pgm and pgm.upper() in _SORT_UTILS:
        return "sort"
    if pgm and pgm.upper() in ("IEBGENER", "ICEGENER"):
        return "iebgener"
    if re.search(r"\bREPRO\b|\bDEFINE\s+CLUSTER\b|\bDELETE\b", body):
        return "idcams"
    if re.search(r"\bSORT\s+FIELDS\b|\bMERGE\s+FIELDS\b|\bOUTREC\b|\bINREC\b", body):
        return "sort"
    return None




_INT = re.compile(r"^\d+$")
_CONST = re.compile(r"^\d*[Cc]'")
_FILL = re.compile(r"^\d+[XxZz]$")


def _parse_build(spec: str) -> List[dict]:
    """Parse a SORT ``BUILD=(...)`` / ``OUTREC=(...)`` field list into output slots, each
    tracing to an input byte range, a constant, spaces, or (unparsed) an opaque edit.
    Enough to show 'output field N comes from input bytes p..p+l-1' - the field lineage.

    The list is comma-separated at BOTH levels: a copied field is itself ``p,l[,fmt]``, so
    ``BUILD=(1,5,6,20,28,8)`` is three fields (1..5), (6..25), (28..35), not six tokens.
    We walk the tokens left to right and re-pair a ``position,length`` when we see one."""
    inner = spec.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    tokens = [t.strip() for t in _split_operands(inner)]
    out: List[dict] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _INT.match(t) and i + 1 < len(tokens) and _INT.match(tokens[i + 1]):
            start, length = int(t), int(tokens[i + 1])
            slot = {"from": "input", "inStart": start, "inLength": length,
                    "inEnd": start + length - 1}
            i += 2
            # optional trailing format/edit tokens (ZD, PD, TO=..., JFY=..., an edit mask):
            # anything that is NOT the start of a new field.
            edits: List[str] = []
            while i < len(tokens):
                nt = tokens[i]
                if _INT.match(nt) or _CONST.match(nt) or _FILL.match(nt) or \
                        nt.upper() in ("SEQNUM", "DATE", "TIME"):
                    break
                edits.append(nt)
                i += 1
            if edits:
                slot["edit"] = ",".join(edits)
            out.append(slot)
        elif _FILL.match(t):                              # nX -> n blanks, nZ -> n zeros
            out.append({"from": "fill", "count": int(t[:-1]), "pad": t[-1].upper()})
            i += 1
        elif _CONST.match(t):
            out.append({"from": "constant", "literal": t})
            i += 1
        elif t.upper() in ("SEQNUM", "DATE", "TIME"):
            out.append({"from": "generated", "kind": t.upper()})
            i += 1
        else:
            out.append({"from": "opaque", "spec": t})
            i += 1
    return out


def _parse_sort_cards(lines: List[str]) -> dict:
    body = "\n".join(lines)
    up = body.upper()
    summary: dict = {"utility": "SORT/DFSORT"}
    m = re.search(r"\bSORT\s+FIELDS=\(([^)]*)\)", up)
    if m:
        summary["sortFields"] = m.group(1)
    fm = re.search(r"\b(INCLUDE|OMIT)\s+COND=(\(.*?\))\s*$", up, re.M)
    if fm:
        summary["filter"] = {"kind": fm.group(1), "cond": fm.group(2)}
    bm = re.search(r"\b(?:OUTREC|INREC|OUTFIL)\b[^=]*\bBUILD=(\(.*\))", body, re.I)
    if not bm:
        bm = re.search(r"\b(?:OUTREC|INREC)=(\(.*\))", body, re.I)
    if bm:
        summary["build"] = _parse_build(bm.group(1))
    sm = re.search(r"\bSUM\s+FIELDS=(\([^)]*\)|NONE)", up)
    if sm:
        summary["sum"] = sm.group(1)
    return summary


def _parse_idcams_cards(lines: List[str]) -> dict:
    body = " ".join(lines)
    summary: dict = {"utility": "IDCAMS"}
    ops: List[dict] = []
    for m in re.finditer(r"\bREPRO\b(.*?)(?=\bREPRO\b|\bDELETE\b|\bDEFINE\b|$)", body,
                         re.I | re.S):
        seg = m.group(1)
        inf = re.search(r"\b(?:INFILE|INDD)\s*\(\s*([A-Z0-9#@$]+)", seg, re.I)
        outf = re.search(r"\b(?:OUTFILE|OUTDD)\s*\(\s*([A-Z0-9#@$]+)", seg, re.I)
        op = {"op": "REPRO"}
        if inf:
            op["inDD"] = inf.group(1).upper()
        if outf:
            op["outDD"] = outf.group(1).upper()
        ops.append(op)
    for m in re.finditer(r"\bDELETE\s+([A-Z0-9#@$.]+)", body, re.I):
        ops.append({"op": "DELETE", "target": m.group(1).upper()})
    for m in re.finditer(r"\bDEFINE\s+(CLUSTER|GDG|AIX|PATH)\b", body, re.I):
        ops.append({"op": "DEFINE", "kind": m.group(1).upper()})
    if ops:
        summary["operations"] = ops
    return summary


def _parse_control_cards(pgm: Optional[str], lines: List[str]) -> Optional[dict]:
    lines = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("*")]
    if not lines:
        return None
    util = _classify_utility(pgm, lines)
    if util == "sort":
        return _parse_sort_cards(lines)
    if util == "idcams":
        return _parse_idcams_cards(lines)
    if util == "iebgener":
        return {"utility": "IEBGENER",
                "note": "SYSUT1 -> SYSUT2 copy" + (
                    "; SYSIN reformat present" if lines else "")}
    return {"utility": "unknown", "cardLineCount": len(lines)}


# --------------------------------------------------------------------------- #
# the parser
# --------------------------------------------------------------------------- #

def parse_jcl(text: str, resolver: Optional[Resolver] = None,
              source_name: str = "<jcl>") -> Job:
    """Parse a JCL job or PROC member into a ``Job``. ``resolver(name) -> text | None`` is
    the caller-provided retrieval for cataloged PROCs, INCLUDE members, and control-card
    datasets; anything it cannot return is flagged, never guessed."""
    physical = text.splitlines()
    return _Parser(physical, resolver, source_name).parse()


class _Parser:
    def __init__(self, physical: List[str], resolver: Optional[Resolver],
                 source_name: str, expanding: Optional[set] = None):
        self.physical = physical
        self.resolver = resolver
        self.job = Job(name="", source_name=source_name)
        # Shared across nested PROC expansions so a cycle A->B->A is caught, not looped.
        self._expanding: set = expanding if expanding is not None else set()
        # The open IF/THEN/ELSE nesting at the current point of the member; every step
        # created gets a snapshot (a step inside ELSE carries the expr negated).
        self._ifstack: List[dict] = []
        # The steps of the PROC invocation currently in scope. A `//procstep.dd DD`
        # override applies to THIS invocation's step of that name - not, as it once did,
        # to the first step anywhere in the job with a matching proc-step name, which
        # sent the override to the wrong copy when a PROC was invoked more than once.
        self._invocation: List[Step] = []

    # -- resolver plumbing --------------------------------------------------
    def _resolve(self, name: str, what: str) -> Optional[str]:
        if self.resolver is None:
            self.job.flags.append(
                f"{what} {name}: no resolver supplied - its content is not in the model")
            return None
        try:
            got = self.resolver(name)
        except Exception as exc:               # a bad resolver must not crash the parse
            self.job.flags.append(f"{what} {name}: resolver raised {exc!r}")
            logger.debug("JCL resolver raised for %s %r", what, name, exc_info=True)
            return None
        if got is None:
            self.job.flags.append(
                f"{what} {name}: resolver returned nothing - content not in the model")
        return got

    # -- instream data ------------------------------------------------------
    def _collect_instream(self, start: int, dlm: str,
                          data_mode: bool) -> Tuple[List[str], int]:
        """From physical line ``start`` (the line after a ``DD *`` / ``DD DATA``), collect
        data lines until the delimiter. For ``DD *`` a ``//`` statement also ends the
        stream; for ``DD DATA`` only the delimiter (default ``/*``) does, so ``//`` may
        appear in the data. Returns (lines, index of the line after the block)."""
        data: List[str] = []
        i = start
        while i < len(self.physical):
            ln = self.physical[i]
            s = ln.rstrip()
            # The delimiter is recognized in columns 1-2; anything after it on the line is
            # ignored. Matching the WHOLE line missed a delimiter that carried a trailing
            # comment or blanks, so equality is too strict - anchor at the start instead.
            if s[:len(dlm)] == dlm and (dlm != "/*" or s == "/*"):
                return data, i + 1
            if not data_mode and s.startswith("//") and not s.startswith("//*"):
                return data, i          # DD *: the next // statement ends the stream
            data.append(ln)
            i += 1
        return data, i

    # -- main pass ----------------------------------------------------------
    def parse(self) -> Job:
        # We need instream data (which is NOT // lines), so we walk physical lines and,
        # for DD * / DD DATA, capture the following data block, then feed the // statements
        # through continuation-merging. Simplest correct approach: single pass with a small
        # lookahead.
        stmts = self._logical_with_data()
        self._build(stmts)
        # A bare PROC member (a .prc that only DEFINES a PROC, never EXECs it): analyse its
        # body directly, expanded with its own defaults, so the member is not empty.
        if self.job.is_proc and not self.job.steps and self.job.procs:
            for pname in list(self.job.procs):
                self.job.steps.extend(self._expand_proc(pname, pname, {}, None))
        self._attach_control_cards()
        return self.job

    def _attach_control_cards(self) -> None:
        """Parse instream control cards into ``dd.control``; resolve a control-card DATASET
        (``//SYSIN DD DSN=PARM.LIB(SORTCRD)``) via the resolver and parse that too."""
        # DDs that carry CONTROL CARDS (not data): SYSIN for SORT/IDCAMS/most utilities,
        # TOOLIN for ICETOOL, SYSTSIN for TSO. SORTIN/SORTOUT are the sort DATA, not cards.
        card_dds = ("SYSIN", "TOOLIN", "SYSTSIN", "DFSPARM")
        for step in self.job.steps:
            for dd in step.dds:
                lines = list(dd.instream_lines)
                if not lines and dd.ddname in card_dds and dd.segments:
                    seg = dd.segments[0]
                    if seg.dsn and not seg.sysout and not seg.instream:
                        name = seg.dsn + (f"({seg.member})" if seg.member else "")
                        got = self._resolve(name, "control-card dataset")
                        if got is not None:
                            lines = got.splitlines()
                if lines:
                    ctl = _parse_control_cards(step.pgm, lines)
                    if ctl:
                        dd.control = ctl

    def _logical_with_data(self) -> List[dict]:
        """Merge continuations AND capture instream data. Each item is a dict:
        {kind:'stmt', line:_LogLine} or {kind:'data', ddname, lines}."""
        items: List[dict] = []
        i = 0
        n = len(self.physical)
        while i < n:
            raw = self.physical[i].rstrip("\n")
            line = raw.rstrip()
            if not line:
                i += 1
                continue
            if _COMMENT.match(line):
                i += 1
                continue
            m = _STMT.match(line)
            if not m:
                i += 1
                continue
            name, op, operands = m.group(1).upper(), m.group(2).upper(), (m.group(3) or "")
            raw_parts = [line]
            while operands.rstrip().endswith(","):
                j = i + 1
                while j < n and _COMMENT.match(self.physical[j].rstrip()):
                    j += 1
                if j >= n:
                    break
                cont = _CONT.match(self.physical[j].rstrip("\n").rstrip())
                if not cont:
                    break
                operands = operands.rstrip() + cont.group(1).strip()
                raw_parts.append(self.physical[j].rstrip())
                i = j
            log = _LogLine(name=name, op=op, operands=operands, raw="\n".join(raw_parts))
            items.append({"kind": "stmt", "line": log})
            # DD * / DD DATA: capture the instream block that follows.
            if op == "DD":
                kw, pos = _operand_map(operands)
                posu = [p.upper() for p in pos]
                if "*" in posu or "DATA" in posu:
                    data_mode = "DATA" in posu
                    # DLM names a 2-character delimiter and is nearly always quoted
                    # (`DLM='$$'`); the quotes are JCL syntax, not part of the delimiter.
                    # Keeping them meant the parser looked for a line equal to `'$$'` and
                    # never found it, so the instream ran to end-of-file and swallowed
                    # every step after this one.
                    dlm = (kw.get("DLM") or "/*").strip("'")
                    data, nxt = self._collect_instream(i + 1, dlm, data_mode)
                    items.append({"kind": "data", "ddname": name, "lines": data})
                    i = nxt
                    continue
            i += 1
        return items

    def _build(self, items: List[dict]) -> None:
        cur_step: Optional[Step] = None
        cur_dd: Optional[DD] = None
        collecting_proc: Optional[ProcDef] = None

        idx = 0
        while idx < len(items):
            item = items[idx]
            if item["kind"] == "data":
                if cur_dd is not None:
                    cur_dd.instream_lines.extend(
                        l.rstrip("\n") for l in item["lines"])
                idx += 1
                continue
            log: _LogLine = item["line"]
            op = log.op

            # Inside an inline PROC definition: accumulate its body until PEND.
            if collecting_proc is not None and op != "PEND":
                collecting_proc.lines.append(log_line_text(log))
                idx += 1
                continue

            if op == "JOB":
                self.job.name = log.name
                idx += 1
                continue
            if op == "PROC" and log.name:
                # //NAME PROC ... PEND  (definition). Capture defaults.
                defaults, _ = _operand_map(log.operands)
                pd = ProcDef(name=log.name, defaults={k: v for k, v in defaults.items()})
                collecting_proc = pd
                self.job.procs[log.name] = pd
                # a bare PROC member (no JOB) - remember, so callers know it is a PROC.
                if not self.job.name:
                    self.job.is_proc = True
                idx += 1
                continue
            if op == "PEND":
                collecting_proc = None
                idx += 1
                continue
            if op == "SET":
                kw, _ = _operand_map(log.operands)
                for k, v in kw.items():
                    sub, _ = _substitute(v, self.job.symbols)
                    self.job.symbols[k] = sub
                idx += 1
                continue
            if op == "JCLLIB":
                kw, _ = _operand_map(log.operands)
                order = kw.get("ORDER", "")
                self.job.jcllib.extend(_paren_list(order))
                idx += 1
                continue
            if op == "INCLUDE":
                kw, _ = _operand_map(log.operands)
                member = kw.get("MEMBER", "").upper()
                if member:
                    self.job.includes.append(member)
                    self._expand_include(member, cur_step)
                idx += 1
                continue
            if op == "IF":
                self._if_push(log.operands)
                idx += 1
                continue
            if op == "ELSE":
                self._if_else()
                idx += 1
                continue
            if op == "ENDIF":
                self._if_pop()
                idx += 1
                continue
            if op == "EXEC":
                cur_step, cur_dd = None, None
                new_steps = self._make_steps(log)
                self._attach_step_context(new_steps)
                self.job.steps.extend(new_steps)
                self._invocation = new_steps    # overrides bind to THIS invocation
                cur_step = new_steps[-1] if new_steps else None
                cur_dd = None
                idx += 1
                continue
            if op == "DD":
                self._handle_dd(log, cur_step)
                cur_dd = self._last_dd(cur_step, log)
                idx += 1
                continue
            idx += 1
        if self._ifstack:
            self.job.flags.append(
                f"{len(self._ifstack)} IF without ENDIF at end of member - conditions on "
                f"later steps may be wrong")

    # -- IF/THEN/ELSE nesting ------------------------------------------------
    def _if_push(self, operands: str) -> None:
        expr = re.sub(r"\bTHEN\s*$", "", operands or "", flags=re.I).strip()
        self._ifstack.append({"expr": expr, "negated": False})

    def _if_else(self) -> None:
        if self._ifstack:
            top = self._ifstack[-1]
            self._ifstack[-1] = {"expr": top["expr"], "negated": True}
        else:
            self.job.flags.append("ELSE without a matching IF - conditions may be wrong")

    def _if_pop(self) -> None:
        if self._ifstack:
            self._ifstack.pop()
        else:
            self.job.flags.append("ENDIF without a matching IF - conditions may be wrong")

    def _attach_step_context(self, steps: List[Step]) -> None:
        """Stamp the current IF nesting onto newly created steps (outer conditions first,
        before any the step already carries from inside a PROC body), and parse COND=."""
        snapshot = [dict(c) for c in self._ifstack]
        for st in steps:
            if snapshot:
                st.conditions = snapshot + st.conditions
            if st.cond and st.cond_parsed is None:
                st.cond_parsed = _parse_cond(st.cond)

    # -- helpers ------------------------------------------------------------
    def _last_dd(self, step: Optional[Step], log: _LogLine) -> Optional[DD]:
        if step is None or not step.dds:
            return None
        return step.dds[-1]

    def _make_steps(self, log: _LogLine) -> List[Step]:
        kw, pos = _operand_map(log.operands)
        if "PGM" in kw:
            step = Step(name=log.name, pgm=kw["PGM"].upper(), cond=kw.get("COND"),
                        parm=kw.get("PARM"))
            return [step]
        # EXEC procname  or  EXEC PROC=procname  -> a PROC invocation.
        procname = kw.get("PROC") or (pos[0] if pos else None)
        if not procname:
            step = Step(name=log.name, cond=kw.get("COND"))
            step.flags.append("EXEC with neither PGM= nor a PROC name")
            return [step]
        procname = procname.upper()
        overrides = {k: v for k, v in kw.items()
                     if k not in ("PROC", "COND", "PARM", "PGM")}
        return self._expand_proc(log.name, procname, overrides, kw.get("COND"))

    def _expand_proc(self, invoke_name: str, procname: str, overrides: Dict[str, str],
                     cond: Optional[str]) -> List[Step]:
        if procname in self._expanding:
            s = Step(name=invoke_name, proc=procname, proc_resolved=False, cond=cond)
            s.flags.append(f"PROC {procname}: recursive invocation - not expanded")
            return [s]
        pd = self.job.procs.get(procname)
        text = None
        if pd is None:
            text = self._resolve(procname, "PROC")
            if text is None:
                s = Step(name=invoke_name, proc=procname, proc_resolved=False, cond=cond)
                s.flags.append(f"PROC {procname}: not resolved - its steps/DDs are not in "
                               f"the model")
                return [s]
            pd = _parse_proc_member(text, procname)
            self.job.procs[procname] = pd

        # symbol scope for this expansion: PROC defaults < job SET < EXEC overrides.
        symbols = dict(pd.defaults)
        symbols.update(self.job.symbols)
        for k, v in overrides.items():
            sub, _ = _substitute(v, symbols)
            symbols[k] = sub

        self._expanding.add(procname)
        sub = _Parser(pd.lines, self.resolver, self.job.source_name,
                      expanding=self._expanding)
        sub.job.procs = self.job.procs          # inline PROCs are visible to nested EXECs
        sub_job = sub.parse_body(symbols)
        self._expanding.discard(procname)

        steps: List[Step] = []
        for st in sub_job.steps:
            st.from_proc = procname
            st.proc_step = st.name
            st.proc_resolved = True
            st.name = f"{invoke_name}.{st.name}"
            if cond and not st.cond:
                st.cond = cond
            steps.append(st)
        self.job.flags.extend(f for f in sub_job.flags if f not in self.job.flags)
        return steps

    def parse_body(self, symbols: Dict[str, str]) -> Job:
        """Parse a PROC body (already a list of logical statement texts) with the given
        symbol table pre-loaded. Used by _expand_proc."""
        self.job.symbols = dict(symbols)
        stmts = self._logical_with_data()
        self._build(stmts)
        return self.job

    def _expand_include(self, member: str, cur_step: Optional[Step]) -> None:
        """Inline a resolved INCLUDE member. It may carry SET/JCLLIB, whole steps, or - the
        common case - bare DD statements meant to attach to the step open at the INCLUDE
        point. We dispatch its statements minimally so none are silently dropped."""
        text = self._resolve(member, "INCLUDE")
        if text is None:
            return
        merged, _ = _gather(text.splitlines())
        step = cur_step
        for log in merged:
            op = log.op
            if op == "SET":
                kw, _ = _operand_map(log.operands)
                for k, v in kw.items():
                    sub, _ = _substitute(v, self.job.symbols)
                    self.job.symbols[k] = sub
            elif op == "JCLLIB":
                kw, _ = _operand_map(log.operands)
                self.job.jcllib.extend(_paren_list(kw.get("ORDER", "")))
            elif op == "IF":
                self._if_push(log.operands)
            elif op == "ELSE":
                self._if_else()
            elif op == "ENDIF":
                self._if_pop()
            elif op == "EXEC":
                steps = self._make_steps(log)
                self._attach_step_context(steps)
                self.job.steps.extend(steps)
                self._invocation = steps
                step = steps[-1] if steps else step
            elif op == "DD":
                self._handle_dd(log, step)

    def _handle_dd(self, log: _LogLine, cur_step: Optional[Step]) -> None:
        if cur_step is None:
            return
        ddname = log.name
        # concatenation: a DD with a BLANK name adds a segment to the previous DD.
        if ddname == "" and cur_step.dds:
            seg = _parse_dd_segment(log.operands, self.job.symbols)
            cur_step.dds[-1].segments.append(seg)
            self._note_symbols(seg, cur_step)
            return
        # a PROC-step override: //procstep.ddname DD ...
        if "." in ddname:
            self._apply_override(ddname, log)
            return
        dd = DD(ddname=ddname)
        seg = _parse_dd_segment(log.operands, self.job.symbols)
        dd.segments.append(seg)
        self._note_symbols(seg, cur_step)
        cur_step.dds.append(dd)

    def _apply_override(self, dotted: str, log: _LogLine) -> None:
        procstep, ddname = dotted.split(".", 1)
        seg = _parse_dd_segment(log.operands, self.job.symbols)
        # Bind to the invocation this override follows, not to the first step in the whole
        # job with a matching proc-step name (which was wrong whenever a PROC ran twice).
        scope = self._invocation or self.job.steps
        target = next((st for st in scope
                       if st.proc_step == procstep or st.name.endswith("." + procstep)),
                      None)
        if target is None:
            self.job.flags.append(
                f"DD override {dotted}: no PROC step {procstep} to apply it to")
            return
        for dd in target.dds:
            if dd.ddname == ddname:
                # An override MERGES: the parameters it names replace the PROC DD's, and
                # the ones it omits are kept. Replacing the whole DD (as this did) dropped
                # the PROC DD's DISP whenever the override only changed the DSN, and DISP
                # is what the lineage reads for input-vs-output - so the direction went
                # null and the dataflow edge vanished.
                dd.segments = [_merge_dd_segment(dd.segments[0], seg)] if dd.segments \
                    else [seg]
                dd.override = True
                return
        newdd = DD(ddname=ddname, override=True)
        newdd.segments.append(seg)
        target.dds.append(newdd)           # additive override

    def _note_symbols(self, seg: DDSegment, step: Step) -> None:
        for s in seg.unresolved_symbols:
            msg = (f"step {step.name}: DD DSN uses unresolved symbolic &{s} - the dataset "
                   f"name is not fully known (set by a PROC/SET/EXEC override or the "
                   f"scheduler)")
            if msg not in self.job.flags:
                self.job.flags.append(msg)


def log_line_text(log: "_LogLine") -> str:
    """Re-render a logical statement as a single // line for PROC-body re-parsing."""
    head = f"//{log.name} {log.op}".rstrip()
    return f"{head} {log.operands}".rstrip()


def _parse_proc_member(text: str, procname: str) -> ProcDef:
    """Parse a cataloged PROC member's text: its ``//NAME PROC`` defaults + body lines."""
    physical = text.splitlines()
    merged, _ = _gather(physical)
    defaults: Dict[str, str] = {}
    body: List[str] = []
    for log in merged:
        if log.op == "PROC":
            kw, _ = _operand_map(log.operands)
            defaults.update(kw)
            continue
        if log.op == "PEND":
            continue
        body.append(log_line_text(log))
    return ProcDef(name=procname, defaults=defaults, lines=body)


# --------------------------------------------------------------------------- #
# after-parse enrichment: classify control cards on utility steps
# --------------------------------------------------------------------------- #
