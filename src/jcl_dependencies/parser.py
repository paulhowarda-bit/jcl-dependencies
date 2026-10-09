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
fetch them - the caller passes ``resolver``, a function
``resolver(name, kind=...) -> text | None``, and this module calls it and substitutes what
it returns. ``kind`` is what the member is, in the artifact manifest's words - ``proc``,
``include-member`` or ``control-card`` - because the name alone does not say: one member
name is often a PROC, a job and a control card at once, and only this parser knows which
one an ``EXEC`` or a ``SYSIN DD`` meant. A resolver written as ``resolver(name)`` is called
that way. Anything the resolver cannot return is **flagged, never guessed** - the same
rule the COBOL side follows for an unresolved ``CALL`` or a missing copybook.

**Honest limits, all surfaced in flags rather than guessed** (the hazards are enumerated in
docs/mainframe-artifacts.md; this parser handles the common cases and flags the rest):
symbolic parameters it cannot resolve are left visible and flagged; ``OLD``/``I-O`` DISP is
direction-ambiguous and noted; dynamic allocation (SVC 99, ``BPXWDYN``) and scheduler-set
symbolics are not statically knowable and are flagged; a ``DDNAME=`` reference is followed
to the later DD that defines it, and flagged where none does; GDG relative generations are
normalized to their base (the stable identity) with the generation recorded.

**Control-M AutoEdit.** ``//* %%INCLIB lib %%INCMEM member`` is a comment to JES and an
instruction to Control-M, which reads that member's ``%%SET`` statements when it submits
the job and substitutes the ``%%variables`` they define before JES reads a line. So the
member is a dependency of the job (``Job.autoedit_members``), and a dataset name still
carrying a ``%%variable`` is not a name until Control-M has expanded it: it is flagged
with the member its value comes from, and never asked of the resolver. The member itself
is not read, so its values are not in the model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Called as resolver(name, kind=...); one written as resolver(name) still works.
Resolver = Callable[..., Optional[str]]


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #

@dataclass
class DDSegment:
    """One dataset in a DD (a DD may concatenate several)."""
    dsn: Optional[str] = None            # DSN after symbolic substitution; a referback
                                         # (*.step.dd) is the dataset it points at
    disp: List[str] = field(default_factory=list)   # [status, normal, abnormal]
    sysout: Optional[str] = None
    instream: bool = False               # DD * / DD DATA
    lines: List[str] = field(default_factory=list)  # ... and the data that follows it
    dummy: bool = False
    member: Optional[str] = None         # DSN(MEMBER)
    gdg: Optional[str] = None            # (+1) / (0) / (-1) relative generation
    ddname_ref: Optional[str] = None     # DDNAME=name: defined by a later DD of the step
    unresolved_symbols: List[str] = field(default_factory=list)
    raw: str = ""

@dataclass
class DD:
    ddname: str
    segments: List[DDSegment] = field(default_factory=list)
    control: Optional[dict] = None       # parsed control-card summary, if any
    override: bool = False               # a PROC-step DD override (//STEP.DD ...)

    @property
    def instream_lines(self) -> List[str]:
        """The DD's instream data, across its concatenation, in the order coded."""
        return [ln for seg in self.segments for ln in seg.lines]


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
    # A PROC step only: the COND= the EXEC that called the PROC applied to it (COND= for
    # every step, COND.procstep= for one). Kept apart from `cond`, the PROC's own, and it
    # is the one that holds - the calling EXEC's COND overrides the called EXEC's.
    invoked_cond: Optional[str] = None
    invoked_cond_parsed: Optional[dict] = None
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
    # Control-M AutoEdit members the job loads when it is submitted, as LIB(MEMBER) - from
    # `//* %%INCLIB lib %%INCMEM member` cards in the job member itself (_AUTOEDIT_INCLUDE).
    autoedit_members: List[str] = field(default_factory=list)
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

# A Control-M AutoEdit include rides on a comment card because JES must never see it:
# Control-M reads it when it SUBMITS the job, loads the member's %%SET statements, and
# substitutes the %%variables they define before JES reads a line. Only the job member's
# own cards count - a cataloged PROC or an INCLUDE member is expanded by JES, after
# Control-M has finished with the job.
_AUTOEDIT_INCLUDE = re.compile(r"^//\*.*?%%INCLIB\s+(\S+)\s+%%INCMEM\s+(\S+)", re.I)
# A Control-M AutoEdit variable still in the JCL (`%%DB2`). `%%.`, the concatenation
# operator, names none.
_AUTOEDIT_VAR = re.compile(r"%%([A-Z0-9_#@$]+)", re.I)


# The IF statement is the one exception to the first-blank rule: its operand is a
# relational expression that legitimately contains blanks (`(PREP.RC = 0) THEN`), so its
# operand field ends at THEN instead. ELSE and ENDIF carry no operands at all.
_IF_THEN = re.compile(r"^(.*?\bTHEN)\b", re.I)


def _operand_field(text: str, in_quote: bool = False, op: str = "") -> Tuple[str, bool]:
    """The operand field only - the inline comment and the card identification field
    removed - and whether it ended inside a quote.

    A JCL statement occupies columns 1-71; column 72 is the continuation indicator and
    columns 73-80 are a free-form identification field, conventionally a card sequence
    number, that is never part of the statement. Separately, within the statement, the
    operand field ends at the first blank that is not inside quotes and everything after
    that blank is a comment. One rule covers both: stop at that blank.

    Truncating at column 71 instead would not be equivalent - it would leave an inline
    comment on a short card in place, and it would corrupt a statement in a file whose
    lines have been reflowed and are no longer card images.

    ``in_quote`` carries the quote state along a continuation chain, because a literal may
    be split across cards: `PARM='ALPHA,` continued by `BETA GAMMA'` is one value, and a
    card that resumes a literal is operand text throughout, blanks included. Scanning such
    a card as if it began outside a quote would cut it at its first space.

    ``op`` selects the rule. Only IF differs, and only when its THEN is on this card: a
    THEN carried onto a continuation leaves the expression ending in a blank-bearing
    fragment, which is what today's code already produces."""
    if op.upper() == "IF":
        m = _IF_THEN.match(text)
        return (m.group(1) if m else text), in_quote
    out = []
    for ch in text:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif ch == " " and not in_quote:
            break
        else:
            out.append(ch)
    return "".join(out), in_quote


def _continuation_indicator(line: str) -> bool:
    """Whether column 72 of this card carries a continuation indicator.

    JCL continues a statement by coding through column 71 and putting ANY non-blank
    character in column 72. That is the only continuation signal when the operand field
    does not end in a comma - which is exactly the case a quoted value split across cards
    produces. ``line`` is already right-stripped, so a card with nothing beyond column 71
    is simply too short and reports False."""
    return bool(line[71:72].strip())


def _statement_columns(text: str, line: str) -> str:
    """``text`` cut back to the statement columns of the card it came from.

    Columns 1-71 are the statement; 72 is the continuation indicator and 73-80 identify
    the card. `_operand_field`'s first-blank rule normally removes 73-80 for free, because
    a blank separates them from the operands - but it cannot when the operand field is
    still inside a quote at column 71, and then the scan runs to the end of the physical
    line and absorbs the identification field. Applied only in that case, so a file whose
    lines have been reflowed and are no longer card images is left alone."""
    over = len(line) - 71
    return text[:len(text) - over] if over > 0 else text


def _operands_of(text: str, line: str, op: str, in_quote: bool = False):
    """The operand field of one card, re-scanned within columns 1-71 if it ended open."""
    operands, ended_in_quote = _operand_field(text.strip(), in_quote, op)
    if ended_in_quote:
        operands, ended_in_quote = _operand_field(
            _statement_columns(text, line).strip(), in_quote, op)
    return operands, ended_in_quote


def _merge_continuations(physical: List[str], i: int, line: str, text: str,
                         op: str,
                         flags: Optional[List[str]] = None) -> Tuple[str, List[str], int]:
    """Stitch a statement's continuation cards onto it. Returns (operands, raw, index).

    Shared by `_gather` and `Parser._logical_with_data`, which carried byte-identical
    copies of this loop. A continuation is signalled two ways and BOTH are honoured: an
    operand field ending in a comma, or - for a quoted value split mid-literal, where no
    comma is possible - an open quote together with a non-blank column 72.

    Both signals are required for the open-literal case. A non-blank column 72 on its own
    is not safe: a member whose identification field is misaligned by one column would
    then swallow the statement after it, and that statement's dataset would vanish. That
    is a worse failure than the one being fixed.

    ``flags`` is where a promised continuation that never arrives is reported. Both exits
    below mean the statement is incomplete and its remaining operands - a DSN, a DISP, a
    PARM - are simply not in the model. Silence there reads afterwards as a job that did
    not name them, which is the failure this package exists to prevent."""
    n = len(physical)
    indicator = _continuation_indicator(line)
    operands, in_quote = _operands_of(text, line, op)
    raw_parts = [line]
    while operands.rstrip().endswith(",") or (in_quote and indicator):
        j = i + 1
        while j < n and _COMMENT.match(physical[j].rstrip()):
            j += 1
        if j >= n:
            if flags is not None:
                flags.append(f"{op or 'statement'} continues past the last card - its "
                             f"remaining operands are not in this model")
            break
        cont_line = physical[j].rstrip("\n").rstrip()
        cont = _CONT.match(cont_line)
        if not cont:
            if flags is not None:
                flags.append(f"{op or 'statement'} promises a continuation but the next "
                             f"card is not one - its remaining operands are not in this "
                             f"model")
            break
        indicator = _continuation_indicator(cont_line)
        part, in_quote = _operands_of(cont.group(1), cont_line, op, in_quote)
        operands = operands.rstrip() + part
        raw_parts.append(physical[j].rstrip())
        i = j
    return operands, raw_parts, i


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
        name, op = m.group(1), m.group(2)
        operands, raw_parts, i = _merge_continuations(physical, i, line,
                                                      m.group(3) or "", op, flags)
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


def _symbol_value(v: str) -> str:
    """The value a symbol holds, from the text coded for it. A value with special
    characters is coded in apostrophes (``HLQ='PROD'``, ``GEN='+1'``): they delimit the
    value and are not part of it, and an apostrophe inside it is coded doubled. Kept, they
    were substituted into the DSN verbatim - ``'PROD'.A.B``, a name no catalog holds - and
    they hid a generation or a member from the patterns that split it off the name.

    Only what is stored as a symbol comes through here. `_operand_map` keeps returning
    operands as coded, because ``PARM='A,B'`` is read from the same map."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        v = v[1:-1].replace("''", "'")
    return v


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
    if "DDNAME" in kw:
        seg.ddname_ref = kw["DDNAME"].upper()
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
        lines=base.lines,
        dummy=base.dummy,
        member=ov.member if ov.member is not None else base.member,
        gdg=ov.gdg if ov.gdg is not None else base.gdg,
        ddname_ref=ov.ddname_ref if ov.ddname_ref is not None else base.ddname_ref,
        unresolved_symbols=ov.unresolved_symbols or base.unresolved_symbols,
        raw=f"{base.raw} | override: {ov.raw}",
    )


def _resolve_referbacks(steps: List[Step]) -> List[Tuple[Step, DD, DDSegment]]:
    """Replace each backward reference - ``DSN=*.ddname``, ``*.stepname.ddname``,
    ``*.stepname.procstepname.ddname`` - with the dataset the DD it points at names, and
    return the ones that point at no earlier DD carrying a dataset name, left as written.

    A referback is a pointer, not a name. Published as coded, two steps sharing one
    dataset read as two datasets, and the dataflow edge between them is never drawn. What
    is copied is the first segment's DSN, generation and member; the DD has to come
    first, because the reference is a backward one.

    Step names are matched as they stand in ``steps``, so this runs once per PROC body -
    where a sibling step is named as the PROC names it - and again on whatever EXECed
    that PROC, where the same steps are `invocation.procstep` and a reference the body
    could not resolve in its own scope gets its chance."""
    earlier: Dict[Tuple[str, str], DDSegment] = {}
    unresolved: List[Tuple[Step, DD, DDSegment]] = []
    for step in steps:
        for dd in step.dds:
            for seg in dd.segments:
                if not (seg.dsn or "").startswith("*."):
                    continue
                stepname, _, ddname = seg.dsn[2:].rpartition(".")
                target = earlier.get((stepname or step.name, ddname))
                if target is None or not target.dsn or target.dsn.startswith("*."):
                    unresolved.append((step, dd, seg))
                else:
                    seg.dsn, seg.gdg, seg.member = target.dsn, target.gdg, target.member
            if dd.segments:
                earlier.setdefault((step.name, dd.ddname), dd.segments[0])
    return unresolved


def _resolve_ddnames(steps: List[Step]) -> List[str]:
    """Give each ``DDNAME=name`` DD the definition a LATER DD statement of its step
    supplies under that name, and return what a reader has to be told about it.

    ``//SYSUT1 DD DDNAME=INPUT`` postpones SYSUT1's definition to ``//INPUT DD ...``;
    INPUT is then not a ddname the step allocates. Where INPUT is a concatenation the
    system takes only its FIRST dataset for the reference and concatenates the rest to
    the last DD statement before INPUT - which is the referencing DD only when nothing
    was coded between the two (z/OS MVS JCL Reference, "References to concatenated data
    sets"). That is what the system does, so it is what is modelled, and it is flagged
    where the rest lands on some other DD. A reference no later DD answers is flagged
    too: what that DD reads is not in this JCL."""
    flags: List[str] = []
    for step in steps:
        i = 0
        while i < len(step.dds):
            dd = step.dds[i]
            for pos, seg in enumerate(dd.segments):
                ref = seg.ddname_ref
                if not ref or seg.dsn:
                    continue
                j = next((k for k in range(i + 1, len(step.dds))
                          if step.dds[k].ddname == ref), None)
                if j is None:
                    flags.append(
                        f"step {step.name}: DD {dd.ddname} is DDNAME={ref}, and no later "
                        f"DD statement of the step is named {ref} - what it reads is not "
                        f"in this JCL")
                    continue
                target = step.dds.pop(j)
                before = step.dds[j - 1]
                dd.segments[pos] = target.segments[0]
                rest = target.segments[1:]
                before.segments.extend(rest)
                if rest and before is not dd:
                    flags.append(
                        f"step {step.name}: DD {dd.ddname} is DDNAME={ref} and {ref} is a "
                        f"concatenation - {dd.ddname} takes the first dataset only, and "
                        f"the rest ({len(rest)}) are concatenated to DD {before.ddname}, "
                        f"the last DD statement before {ref} (how the system binds a "
                        f"forward reference to a concatenation)")
            i += 1
    return flags


def _dummies_in_concatenation(steps: List[Step]) -> List[str]:
    """A DUMMY anywhere but last in a concatenation ends the data there: reading a dummy
    data set takes the end-of-data exit at once, and the system ignores whatever is
    concatenated after it. Those later datasets are still allocated, so they stay
    published as coded - with this said about them."""
    flags: List[str] = []
    for step in steps:
        for dd in step.dds:
            at = next((n for n, seg in enumerate(dd.segments[:-1], start=1) if seg.dummy),
                      None)
            if at:
                flags.append(
                    f"step {step.name}: DD {dd.ddname} has DUMMY at position {at} of a "
                    f"concatenation of {len(dd.segments)} - the system ignores the data "
                    f"sets concatenated after a dummy one, so the later positions are "
                    f"allocated but never read; they are published as coded")
    return flags


def _dd_direction(seg: DDSegment) -> Optional[str]:
    """'input' / 'output' / None for a single segment, from DISP / SYSOUT /
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
#: The Db2 utility driver: its SYSIN is utility control statements (LOAD, UNLOAD, REORG,
#: RUNSTATS ...), and LOAD / UNLOAD name the TABLES a job moves data into and out of.
_DB2_UTILS = ("DSNUTILB", "DSNUTILS", "DSNUTILU")
#: The TSO batch monitor. Its SYSTSIN carries the DSN command processor's commands, and
#: `RUN PROGRAM(x) PLAN(y)` is where the program a step REALLY runs is named - the EXEC
#: says IKJEFT01, which is never the dependency anyone is looking for.
_TSO_PGMS = ("IKJEFT01", "IKJEFT1A", "IKJEFT1B")
#: Db2's sample SQL processors: run under DSN, they read SQL statements from SYSIN.
_DB2_SQL_PGMS = ("DSNTEP2", "DSNTEP4", "DSNTIAUL", "DSNTIAD")
_SQL_SNIFF = re.compile(r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|"
                        r"CREATE|DROP|ALTER|LOCK\s+TABLE|TRUNCATE)\b")


def _classify_utility(pgm: Optional[str], lines: List[str],
                      ddname: Optional[str] = None) -> Optional[str]:
    body = " ".join(lines).upper()
    up = (pgm or "").upper()
    if up in ("IDCAMS",):
        return "idcams"
    if up in _SORT_UTILS:
        return "sort"
    if up in ("IEBGENER", "ICEGENER"):
        return "iebgener"
    if up in _DB2_UTILS:
        return "db2util"
    if up in _TSO_PGMS:
        # SYSTSIN is the TSO command stream. SYSIN is whatever the program RUN under DSN
        # reads - SQL for DSNTEP2/DSNTIAUL, its own input for anything else - so it is
        # read as SQL only when it looks like SQL, never on the strength of the EXEC.
        if ddname == "SYSTSIN":
            return "tso"
        return "sql" if _SQL_SNIFF.search(body) else "data"
    if up in _DB2_SQL_PGMS:
        return "sql"
    if re.search(r"\bREPRO\b|\bDEFINE\s+CLUSTER\b|\bDELETE\b", body):
        return "idcams"
    if re.search(r"\bSORT\s+FIELDS\b|\bMERGE\s+FIELDS\b|\bOUTREC\b|\bINREC\b", body):
        return "sort"
    if re.search(r"\bINTO\s+TABLE\b|\bFROM\s+TABLE\b|"
                 r"\b(?:REORG|RUNSTATS|COPY|CHECK\s+DATA|QUIESCE)\s+TABLESPACE\b", body):
        return "db2util"
    if re.search(r"\bDSN\s+SYSTEM\s*\(|\bRUN\s+PROGRAM\s*\(", body):
        return "tso"
    return None


# --------------------------------------------------------------------------- #
# Db2: utility statements, DSN commands, SQL - the TABLES a job depends on
# --------------------------------------------------------------------------- #

_DB2_NAME = r"[A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+){0,2}"
_DB2_UTIL_VERBS = ("LOAD", "UNLOAD", "REORG", "RUNSTATS", "COPY", "MERGECOPY", "CHECK",
                   "RECOVER", "REBUILD", "MODIFY", "QUIESCE", "REPORT", "EXEC")
#: Words that can follow FROM / JOIN / INTO / UPDATE without being a table name.
_NOT_A_TABLE = {"SELECT", "TABLE", "FINAL", "OLD", "NEW", "LATERAL", "XMLTABLE", "UNNEST",
                "OF", "SET", "WHERE", "VALUES", "ONLY", "(", ")"}


def _strip_sql_comments(lines: List[str]) -> List[str]:
    return [re.sub(r"--.*$", "", ln) for ln in lines]


def _sql_table_refs(text: str) -> List[dict]:
    """The tables SQL statements name, with the operation and whether it reads or writes.

    Deliberately shallow - standard SQL keywords, not a grammar: FROM / JOIN read,
    INSERT INTO / UPDATE / DELETE FROM / MERGE INTO / TRUNCATE write, DDL is ``ddl``. A
    subselect's ``FROM (`` and ``FROM TABLE(`` / ``FROM FINAL TABLE`` name no table and
    are skipped; a correlation name after the table is ignored. What this cannot see is
    the column list, and it does not pretend to.
    """
    out: List[dict] = []
    seen = set()

    def add(op: str, table: str, io: str) -> None:
        if not table or table.upper() in _NOT_A_TABLE:
            return
        key = (op, table)
        if key not in seen:
            seen.add(key)
            out.append({"op": op, "table": table, "io": io})

    for stmt in text.upper().split(";"):
        toks = re.findall(r"[A-Z0-9_$#@.]+|\(|\)|:", stmt)
        if not toks:
            continue
        verb = toks[0]
        i = 0
        while i < len(toks):
            w = toks[i]
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            nxt2 = toks[i + 2] if i + 2 < len(toks) else ""
            if w == "INSERT" and nxt == "INTO":
                add("INSERT", nxt2, "write")
                i += 3
                continue
            if w == "MERGE" and nxt == "INTO":
                add("MERGE", nxt2, "write")
                i += 3
                continue
            if w == "DELETE" and nxt == "FROM":
                add("DELETE", nxt2, "write")
                i += 3
                continue
            if w == "UPDATE" and nxt not in _NOT_A_TABLE and nxt:
                add("UPDATE", nxt, "write")
                i += 2
                continue
            if w == "TRUNCATE":
                add("TRUNCATE", nxt2 if nxt == "TABLE" else nxt, "write")
                i += 2
                continue
            if w in ("CREATE", "DROP", "ALTER") and nxt == "TABLE":
                add(w, nxt2, "ddl")
                i += 3
                continue
            if w == "LOCK" and nxt == "TABLE":
                add("LOCK", nxt2, "read")
                i += 3
                continue
            if w in ("FROM", "JOIN", "USING"):      # USING: MERGE's source table
                add(verb, nxt, "read")
                i += 2
                continue
            i += 1
    return out


def _parse_db2_utility_cards(lines: List[str]) -> dict:
    """DSNUTILB control statements -> the tables and tablespaces each one touches.

    ``LOAD ... INTO TABLE t`` writes t from INDDN (default SYSREC); ``UNLOAD ... FROM
    TABLE t`` reads t into UNLDDN (default SYSREC); an embedded ``EXEC SQL ... ENDEXEC``
    is read as SQL. REORG / RUNSTATS / COPY / CHECK / QUIESCE and friends work on a
    TABLESPACE - a different identity, reported apart - and RUNSTATS' ``TABLE(t)`` names
    a table it reads. A name written under a Db2 ALIAS or SYNONYM stays as written here;
    the catalog's synonym knowledge, when the host supplies it, is applied by the
    artifacts view.
    """
    body = " ".join(_strip_sql_comments(lines)).upper()
    summary: dict = {"utility": "DB2 utility"}
    tables: List[dict] = []
    spaces: List[dict] = []
    seen_t, seen_s = set(), set()

    def add_table(op: str, table: str, io: str, ddname: Optional[str] = None) -> None:
        key = (op, table, ddname)
        if key in seen_t:
            return
        seen_t.add(key)
        row = {"op": op, "table": table, "io": io}
        if ddname:
            row["ddname"] = ddname
        tables.append(row)

    def add_space(op: str, space: str) -> None:
        if (op, space) not in seen_s:
            seen_s.add((op, space))
            spaces.append({"op": op, "tablespace": space})

    verbs = "|".join(_DB2_UTIL_VERBS)
    parts = [p for p in re.split(rf"(?=\b(?:{verbs})\b)", body) if p.strip()]
    for seg in parts:
        verb = seg.split(None, 1)[0]
        if verb == "EXEC":
            sql = re.sub(r"^\s*EXEC\s+SQL\b", "", seg, flags=re.I)
            sql = re.sub(r"\bENDEXEC\b.*$", "", sql, flags=re.I | re.S)
            for ref in _sql_table_refs(sql):
                add_table(ref["op"], ref["table"], ref["io"])
            continue
        if verb == "LOAD":
            m = re.search(r"\bINDDN\s*\(?\s*([A-Z0-9#@$]+)", seg)
            ddname = m.group(1) if m else "SYSREC"
            for t in re.findall(rf"\bINTO\s+TABLE\s+({_DB2_NAME})", seg):
                add_table("LOAD", t, "write", ddname)
            continue
        if verb == "UNLOAD":
            m = re.search(r"\bUNLDDN\s*\(?\s*([A-Z0-9#@$]+)", seg)
            ddname = m.group(1) if m else "SYSREC"
            found = re.findall(rf"\bFROM\s+TABLE\s+({_DB2_NAME})", seg)
            for t in found:
                add_table("UNLOAD", t, "read", ddname)
            m = re.search(rf"\bTABLESPACE\s+({_DB2_NAME})", seg)
            if m and not found:
                add_space("UNLOAD", m.group(1))
            continue
        for m in re.finditer(rf"\bTABLESPACE\s+({_DB2_NAME})", seg):
            if m.group(1) != "LIST":
                add_space(verb, m.group(1))
        for m in re.finditer(rf"\bTABLE\s*\(\s*({_DB2_NAME})\s*\)", seg):
            if m.group(1) != "ALL":
                add_table(verb, m.group(1), "read")
    if tables:
        summary["tables"] = tables
    if spaces:
        summary["tablespaces"] = spaces
    if not tables and not spaces:
        summary["note"] = "no LOAD/UNLOAD table or TABLESPACE operand was recognised"
    return summary


def _parse_tso_cards(lines: List[str]) -> dict:
    """SYSTSIN under IKJEFT01: the DSN subsystem and every ``RUN PROGRAM(p) PLAN(q)``.

    A TSO command continues on the next line when it ends in ``-`` or ``+``; the
    continuation is joined before the command is read."""
    joined: List[str] = []
    for ln in lines:
        s = ln.strip()
        if joined and joined[-1].endswith(("-", "+")):
            joined[-1] = joined[-1][:-1].rstrip() + " " + s
        else:
            joined.append(s)
    summary: dict = {"utility": "TSO/DSN"}
    runs: List[dict] = []
    for cmd in joined:
        up = cmd.upper()
        m = re.search(r"\bDSN\b.*?\bSYSTEM\s*\(\s*([A-Z0-9#@$]+)", up)
        if m:
            summary["subsystem"] = m.group(1)
            continue
        m = re.search(r"\bRUN\b.*?\bPROGRAM\s*\(\s*([A-Z0-9#@$]+)", up)
        if m:
            run = {"program": m.group(1)}
            pm = re.search(r"\bPLAN\s*\(\s*([A-Z0-9#@$]+)", up)
            if pm:
                run["plan"] = pm.group(1)
            lm = re.search(r"\bLIB\s*\(\s*'?([A-Z0-9#@$.]+)", up)
            if lm:
                run["lib"] = lm.group(1)
            runs.append(run)
    if runs:
        summary["runs"] = runs
    if "subsystem" not in summary and not runs:
        summary = {"utility": "TSO", "commandCount": len(joined)}
    return summary


def _parse_sql_cards(lines: List[str]) -> dict:
    """SYSIN read as SQL (DSNTEP2 / DSNTIAUL / DSNTIAD under DSN): the tables named."""
    text = "\n".join(_strip_sql_comments(lines))
    stmts = [s for s in text.split(";") if s.strip()]
    summary: dict = {"utility": "SQL", "statementCount": len(stmts)}
    refs = _sql_table_refs(text)
    if refs:
        summary["tables"] = refs
    return summary




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


def _parse_control_cards(pgm: Optional[str], lines: List[str],
                         ddname: Optional[str] = None) -> Optional[dict]:
    lines = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("*")]
    if not lines:
        return None
    util = _classify_utility(pgm, lines, ddname)
    if util == "data":
        return None          # a program's own SYSIN input, not anybody's control cards
    if util == "sort":
        return _parse_sort_cards(lines)
    if util == "idcams":
        return _parse_idcams_cards(lines)
    if util == "db2util":
        return _parse_db2_utility_cards(lines)
    if util == "tso":
        return _parse_tso_cards(lines)
    if util == "sql":
        return _parse_sql_cards(lines)
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
    """Parse a JCL job or PROC member into a ``Job``. ``resolver(name, kind=...) -> text |
    None`` is the caller-provided retrieval for cataloged PROCs, INCLUDE members, and
    control-card datasets; anything it cannot return is flagged, never guessed."""
    physical = text.splitlines()
    return _Parser(physical, resolver, source_name).parse()


# What each _resolve caller is asking for, in the artifact manifest's kind words: the
# resolver's `kind=`, which jcl_dependencies.prefetch turns into the estate request's type.
_RESOLVE_KIND = {"PROC": "proc", "INCLUDE": "include-member",
                 "control-card dataset": "control-card"}


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
        # The DD the last NAMED DD statement addressed, the position the next unnamed DD
        # statement takes in it, the step that holds it, and whether a PROC override
        # addressed it. An unnamed DD continues THAT DD - not whichever DD is last in the
        # last step, which is a different one whenever the named statement was an
        # override. None straight after an EXEC.
        self._concat: Optional[Tuple[DD, int, Step, bool]] = None
        # The PROC step the last `//procstep.dd DD` of this invocation named. A DD that
        # names no step modifies THAT step, and the FIRST step of the PROC when none has
        # named one yet (z/OS MVS JCL Reference, "Location in the JCL") - never the last
        # step, which is where they all once went. None straight after an EXEC.
        self._modify_step: Optional[Step] = None
        # Until a TypeError says otherwise - see _call_resolver.
        self._resolver_takes_kind = True

    # -- resolver plumbing --------------------------------------------------
    def _resolve(self, name: str, what: str) -> Optional[str]:
        if self.resolver is None:
            self.job.flags.append(
                f"{what} {name}: no resolver supplied - its content is not in the model")
            return None
        try:
            got = self._call_resolver(name, _RESOLVE_KIND[what])
        except Exception as exc:               # a bad resolver must not crash the parse
            self.job.flags.append(f"{what} {name}: resolver raised {exc!r}")
            logger.debug("JCL resolver raised for %s %r", what, name, exc_info=True)
            return None
        if got is None:
            self.job.flags.append(
                f"{what} {name}: resolver returned nothing - content not in the model")
        return got

    def _call_resolver(self, name: str, kind: str) -> Optional[str]:
        """``resolver(name, kind=...)``, or ``resolver(name)`` for one written before the
        kind was passed. A ``TypeError`` is read as that older signature - once, and then
        remembered, the bargain ``mainframe_artifacts.dependents`` strikes with its own
        resolvers. So a TypeError raised from inside a newer resolver costs one retry
        without the kind, and if that fails too it is reported like any other failure."""
        if self._resolver_takes_kind:
            try:
                return self.resolver(name, kind=kind)
            except TypeError:
                self._resolver_takes_kind = False
        return self.resolver(name)

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
            # Say so. The expansion uses PROC defaults ONLY - no invoking job's SET, no EXEC
            # overrides - so every DSN below is a default that a real invocation may replace.
            self.job.flags.append(
                f"PROC ({', '.join(self.job.procs)}): expanded from its own defaults only - "
                f"this member is a PROC, not a job, and no invoking job's SET or EXEC "
                f"overrides are applied")
            for pname in list(self.job.procs):
                self.job.steps.extend(self._expand_proc(pname, pname, {}, None))
        # Before the referbacks: a DD defined through DDNAME= is one they may point at.
        for msg in (_resolve_ddnames(self.job.steps)
                    + _dummies_in_concatenation(self.job.steps)):
            if msg not in self.job.flags:
                self.job.flags.append(msg)
        # Before the control cards: a card DD may itself be a referback.
        for step, dd, seg in _resolve_referbacks(self.job.steps):
            msg = (f"step {step.name}: DD {dd.ddname} refers back to {seg.dsn}, which "
                   f"names no earlier DD with a dataset name - the dataset is not known")
            if msg not in self.job.flags:
                self.job.flags.append(msg)
        self._attach_control_cards()
        self._flag_autoedit_names()
        return self.job

    def _attach_control_cards(self) -> None:
        """Parse instream control cards into ``dd.control``; resolve a control-card DATASET
        (``//SYSIN DD DSN=PARM.LIB(SORTCRD)``) via the resolver and parse that too."""
        # DDs that carry CONTROL CARDS (not data): SYSIN for SORT/IDCAMS/most utilities,
        # TOOLIN for ICETOOL, SYSTSIN for TSO. SORTIN/SORTOUT are the sort DATA, not cards.
        card_dds = ("SYSIN", "TOOLIN", "SYSTSIN", "DFSPARM")
        for step in self.job.steps:
            for dd in step.dds:
                # Card DDs ONLY. The allow-list used to gate just the dataset-resolution
                # branch below, while INSTREAM lines were content-sniffed on every DD -
                # so transaction data on `//INDATA DD *` containing action words
                # (`DELETE ACCT001 FROM MASTER`) was classified as an IDCAMS control
                # card, and the lineage view then published a phantom destructive
                # operation. Instream DATA is data; only the utilities' own card DDs
                # carry syntax.
                if dd.ddname not in card_dds:
                    continue
                # A card DD may concatenate instream cards and card members; the
                # utility reads them as ONE stream, in the order coded. Reading only the
                # first member lost every card after it - a DSN RUN PROGRAM among them.
                lines: List[str] = []
                for seg in dd.segments:
                    if seg.instream:
                        lines.extend(seg.lines)
                    elif seg.dsn and not seg.sysout:
                        name = seg.dsn + (f"({seg.member})" if seg.member else "")
                        # Not a name yet while it carries a %%variable: Control-M has not
                        # expanded it, so there is nothing to ask for, and
                        # _flag_autoedit_names says where its value lives instead.
                        if not _AUTOEDIT_VAR.search(name):
                            got = self._resolve(name, "control-card dataset")
                            if got is not None:
                                lines.extend(got.splitlines())
                if lines:
                    ctl = _parse_control_cards(step.pgm, lines, dd.ddname)
                    if ctl:
                        dd.control = ctl

    def _flag_autoedit_names(self) -> None:
        """Flag every dataset name a Control-M AutoEdit variable is still in, naming the
        member this job loads its AutoEdit values from. Unflagged, ``LIB(XX%%DB2)`` reads
        as a control-card member nobody could supply - and a job whose whole gap is one
        AutoEdit library reads as many missing members."""
        if self.job.autoedit_members:
            where = (f"from the %%SET statements in "
                     f"{', '.join(self.job.autoedit_members)}, which this job's "
                     f"%%INCLIB/%%INCMEM loads")
        else:
            where = ("and this job loads no AutoEdit member (%%INCLIB/%%INCMEM), so "
                     "where its value is defined is not in this source")
        for step in self.job.steps:
            for dd in step.dds:
                for seg in dd.segments:
                    name = (seg.dsn or "") + (f"({seg.member})" if seg.member else "")
                    found = list(dict.fromkeys(
                        v.upper() for v in _AUTOEDIT_VAR.findall(name)))
                    if not found:
                        continue
                    one = len(found) == 1
                    msg = (f"step {step.name}: DD {dd.ddname} dataset {name} carries "
                           f"Control-M AutoEdit variable{'' if one else 's'} "
                           f"{', '.join('%%' + v for v in found)} - Control-M sets "
                           f"{'it' if one else 'them'} when it submits the job, {where}; "
                           f"the dataset name is not known until then")
                    if msg not in self.job.flags:
                        self.job.flags.append(msg)

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
                inc = _AUTOEDIT_INCLUDE.match(line)
                if inc:
                    ref = f"{inc.group(1).upper()}({inc.group(2).upper()})"
                    if ref not in self.job.autoedit_members:
                        self.job.autoedit_members.append(ref)
                i += 1
                continue
            m = _STMT.match(line)
            if not m:
                i += 1
                continue
            name, op = m.group(1).upper(), m.group(2).upper()
            operands, raw_parts, i = _merge_continuations(self.physical, i, line,
                                                          m.group(3) or "", op,
                                                          self.job.flags)
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
        cur_seg: Optional[DDSegment] = None
        collecting_proc: Optional[ProcDef] = None

        idx = 0
        while idx < len(items):
            item = items[idx]
            if item["kind"] == "data":
                if cur_seg is not None:
                    cur_seg.lines.extend(l.rstrip("\n") for l in item["lines"])
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
                pd = ProcDef(name=log.name,
                             defaults={k: _symbol_value(v) for k, v in defaults.items()})
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
                    sub, _ = _substitute(_symbol_value(v), self.job.symbols)
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
                cur_step, cur_seg = None, None
                self._concat = None
                self._modify_step = None
                new_steps = self._make_steps(log)
                self._attach_step_context(new_steps)
                self.job.steps.extend(new_steps)
                self._invocation = new_steps    # overrides bind to THIS invocation
                cur_step = new_steps[0] if new_steps else None
                idx += 1
                continue
            if op == "DD":
                cur_seg = self._handle_dd(log, cur_step)
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
        # COND.procstep= is the invocation's condition for one PROC step, not a symbolic
        # parameter - read as one it was silently dropped.
        step_conds = {k.split(".", 1)[1]: v for k, v in kw.items() if k.startswith("COND.")}
        overrides = {k: v for k, v in kw.items()
                     if k not in ("PROC", "COND", "PARM", "PGM") and not k.startswith("COND.")}
        return self._expand_proc(log.name, procname, overrides, kw.get("COND"), step_conds)

    def _expand_proc(self, invoke_name: str, procname: str, overrides: Dict[str, str],
                     cond: Optional[str],
                     step_conds: Optional[Dict[str, str]] = None) -> List[Step]:
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
            sub, _ = _substitute(_symbol_value(v), symbols)
            symbols[k] = sub

        self._expanding.add(procname)
        sub = _Parser(pd.lines, self.resolver, self.job.source_name,
                      expanding=self._expanding)
        sub.job.procs = self.job.procs          # inline PROCs are visible to nested EXECs
        sub_job = sub.parse_body(symbols)
        self._expanding.discard(procname)

        step_conds = step_conds or {}
        steps: List[Step] = []
        for st in sub_job.steps:
            st.from_proc = procname
            st.proc_step = st.name
            st.proc_resolved = True
            st.name = f"{invoke_name}.{st.name}"
            # The PROC's own COND= stays in `cond`; what this invocation applied is a
            # separate fact. A nested step (`S1.RUN`) takes the condition aimed at the
            # step that called its PROC (`S1`), which overrides everything under it.
            invoked = step_conds.get(st.proc_step.split(".")[0], cond)
            if invoked:
                st.invoked_cond = invoked
                st.invoked_cond_parsed = _parse_cond(invoked)
            steps.append(st)
        named = {st.proc_step.split(".")[0] for st in steps}
        for pstep in sorted(set(step_conds) - named):
            self.job.flags.append(
                f"EXEC {invoke_name}: COND.{pstep}= names no step of PROC {procname} - "
                f"that condition is not applied to any step")
        if cond and set(step_conds) & named:
            self.job.flags.append(
                f"EXEC {invoke_name}: both COND= and COND.procstep= are coded; each named "
                f"step's invokedCond is its COND.procstep= and every other step's is COND= "
                f"- verify, because how the two combine is not modelled from the reference")
        self.job.flags.extend(f for f in sub_job.flags if f not in self.job.flags)
        return steps

    def parse_body(self, symbols: Dict[str, str]) -> Job:
        """Parse a PROC body (already a list of logical statement texts) with the given
        symbol table pre-loaded. Used by _expand_proc."""
        self.job.symbols = dict(symbols)
        stmts = self._logical_with_data()
        self._build(stmts)
        # In the body's own scope; what is left is the invoking member's to resolve or flag.
        _resolve_referbacks(self.job.steps)
        return self.job

    def _expand_include(self, member: str, cur_step: Optional[Step]) -> None:
        """Inline a resolved INCLUDE member. It may carry SET/JCLLIB, whole steps, or - the
        common case - bare DD statements meant to attach to the step open at the INCLUDE
        point. We dispatch its statements minimally so none are silently dropped."""
        text = self._resolve(member, "INCLUDE")
        if text is None:
            return
        merged, gathered_flags = _gather(text.splitlines())
        self.job.flags.extend(f"INCLUDE {member}: {f}" for f in gathered_flags)
        step = cur_step
        for log in merged:
            op = log.op
            if op == "SET":
                kw, _ = _operand_map(log.operands)
                for k, v in kw.items():
                    sub, _ = _substitute(_symbol_value(v), self.job.symbols)
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
                self._concat = None
                self._modify_step = None
                steps = self._make_steps(log)
                self._attach_step_context(steps)
                self.job.steps.extend(steps)
                self._invocation = steps
                step = steps[0] if steps else step
            elif op == "DD":
                self._handle_dd(log, step)

    def _handle_dd(self, log: _LogLine, cur_step: Optional[Step]) -> Optional[DDSegment]:
        """Apply one DD statement. Returns the segment it produced - the one any instream
        data that follows belongs to - or None for a DD with no step to belong to."""
        if cur_step is None:
            return None
        # A DD naming no PROC step goes to the step the previous override named.
        step = self._modify_step or cur_step
        ddname = log.name
        # concatenation: a DD with a BLANK name is the next dataset of the DD the last
        # named DD statement addressed.
        if ddname == "":
            if self._concat is None and step.dds:
                last = step.dds[-1]
                self._concat = (last, len(last.segments), step, False)
            if self._concat is not None:
                return self._concatenate(log)
        # a PROC-step override: //procstep.ddname DD ...
        if "." in ddname:
            return self._apply_override(ddname, log)
        seg = _parse_dd_segment(log.operands, self.job.symbols)
        self._note_symbols(seg, step)
        if step.proc_step:
            # Coded after the EXEC of a PROC it modifies the PROC, exactly as a qualified
            # one does: an override where the step has a DD of that name, else added.
            return self._modify_dd(step, ddname, seg)
        dd = DD(ddname=ddname)
        dd.segments.append(seg)
        step.dds.append(dd)
        self._concat = (dd, 1, step, False)
        return seg

    def _concatenate(self, log: _LogLine) -> DDSegment:
        """An unnamed DD statement. After a PROC override it overrides, in order, the
        dataset at the same position of the PROC's concatenation - a blank operand field
        leaves that one as the PROC coded it - and past the end of the PROC's
        concatenation it adds to it. Anywhere else it is simply the next dataset."""
        dd, pos, step, overriding = self._concat
        seg = _parse_dd_segment(log.operands, self.job.symbols)
        self._note_symbols(seg, step)
        if overriding and pos < len(dd.segments):
            if log.operands.strip():
                dd.segments[pos] = _merge_dd_segment(dd.segments[pos], seg)
            seg = dd.segments[pos]
        else:
            dd.segments.append(seg)
        self._concat = (dd, pos + 1, step, overriding)
        return seg

    def _apply_override(self, dotted: str, log: _LogLine) -> DDSegment:
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
            # Nor is there one for the unnamed DD statements that continue it: they go
            # with it, to a DD no step holds, rather than onto whichever DD came before.
            self._concat = (DD(ddname=ddname, segments=[seg]), 1, Step(name=procstep),
                            False)
            return seg
        self._modify_step = target
        return self._modify_dd(target, ddname, seg)

    def _modify_dd(self, target: Step, ddname: str, seg: DDSegment) -> DDSegment:
        """Apply a modifying DD statement to the PROC step it addresses: an override of
        the step's DD of that name, or a DD added to the step when it has none."""
        for dd in target.dds:
            if dd.ddname == ddname:
                # An override MERGES: the parameters it names replace the PROC DD's, and
                # the ones it omits are kept. Replacing the whole DD (as this did) dropped
                # the PROC DD's DISP whenever the override only changed the DSN, and DISP
                # is what the lineage reads for input-vs-output - so the direction went
                # null and the dataflow edge vanished.
                #
                # And it overrides the FIRST dataset only. Where the PROC DD is a
                # concatenation the rest stay as the PROC coded them, unless unnamed DD
                # statements follow the override to address them in turn.
                if dd.segments:
                    dd.segments[0] = _merge_dd_segment(dd.segments[0], seg)
                else:
                    dd.segments.append(seg)
                dd.override = True
                self._concat = (dd, 1, target, True)
                return dd.segments[0]
        newdd = DD(ddname=ddname, override=True)
        newdd.segments.append(seg)
        target.dds.append(newdd)           # additive override
        self._concat = (newdd, 1, target, False)
        return seg

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
            defaults.update({k: _symbol_value(v) for k, v in kw.items()})
            continue
        if log.op == "PEND":
            continue
        body.append(log_line_text(log))
    return ProcDef(name=procname, defaults=defaults, lines=body)


# --------------------------------------------------------------------------- #
# after-parse enrichment: classify control cards on utility steps
# --------------------------------------------------------------------------- #
