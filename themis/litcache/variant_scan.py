"""A deterministic scan of a paper's rendering for strings that look like variant mentions.

The extraction agent is given the scan's hits as a checklist: each must end up printed by a mention it records, or
dismissed with a reason. The scan is deliberately loose, and finds only notation a pattern can see; prose, figures and
garbled tables stay the agent's to find. Two kinds of pattern:

- HGVS: wherever `c.`, `g.`, `m.`, `n.`, `r.` or `p.` starts a word, the longest following string weaver parses, with
  the identifier printed before it (`NM_000059.4:`, `BRCA2:`) when there is one.
- Loose: compact protein changes (R337H, Arg337His), compact nucleotide changes (1691G>A), IVS forms and rsIDs.
"""

from __future__ import annotations

import dataclasses
import re

import weaver

# How far past an anchor an HGVS expression may run, and how far before it a printed identifier may start.
_HGVS_SPAN = 120
_PREFIX_SPAN = 60
_ANCHOR = re.compile(r'(?<![A-Za-z0-9])[cgmnrp]\.')
# What follows an anchor in a description weaver cannot parse but a reader would take for a variant: a position, or a
# residue and a position, straight after the anchor (so "p. 23" is not one, nor a bare number such as "p.23").
_HGVS_LIKE = re.compile(r'[cgmnrp]\.[(\[]?(?:[-*]?\d+[^\s\d.,;:)\]-]|[A-Z][a-z]{2}\W?\d|[A-Z]\d)')
_TRAILING = '.,;:'
_PREFIX = re.compile(r'(?<![A-Za-z0-9_.\-])([A-Za-z][A-Za-z0-9_.\-]*(?:\([A-Za-z0-9_.\-]+\))?):$')
# weaver parses only an expression with a reference; this one stands in where the paper prints none.
_PLACEHOLDER = 'NM_000000.1:'
# Typography a renderer substitutes into a description: dashes for a hyphen or minus, an arrow for '>'.
_DASHES = '\u2010\u2011\u2012\u2013\u2212'
_TYPOGRAPHY = str.maketrans({**dict.fromkeys(_DASHES, '-'), '\u2192': '>'})
_AA3 = 'Ala|Arg|Asn|Asp|Cys|Gln|Glu|Gly|His|Ile|Leu|Lys|Met|Phe|Pro|Ser|Thr|Trp|Tyr|Val|Ter|Sec'
_AA1 = 'ACDEFGHIKLMNPQRSTVWY'
_STOP = r'stop|Stop|STOP'
# A frameshift's tail as legacy and HGVS print it: fs, fsX10, fs*10, fsTer10.
_FS_TAIL = r'(?:fs(?:Ter|X|\*)?\d*)?'
# In order: a match inside an earlier pattern's match is not flagged again.
_LOOSE = {
    'IVS': re.compile(r'IVS\s?\d+\s?[+-]\s?\d+\s?[ACGT]>[ACGT]'),
    # Legacy intron numbering, roman or arabic, with or without its change: 'IVS-II-654', 'IVS-I-110 (G>A)'.
    'IVS, legacy': re.compile(
        r'IVS[-\s]?(?:[IVX]{1,4}|\d{1,2})(?:[-\s][+-]?|[+-])\d{1,4}(?:\s?\(?[ACGT]\s?>\s?[ACGT]\)?)?(?![0-9])'
    ),
    # An HGVS description with its coordinate letter capitalised, which the anchor (lower case) does not see.
    'HGVS, capitalised': re.compile(
        r'(?<![A-Za-z0-9])[CGMNR]\.[-*]?\d+(?:[+-]\d+)?(?:_[-*]?\d+(?:[+-]\d+)?)?'
        r'(?:[ACGTU]>[ACGTU]|delins[ACGT]+|del[ACGT]*|ins[ACGT]+|dup[ACGT]*)(?![A-Za-z])'
    ),
    # A deletion of one residue as the cystic fibrosis literature prints it: 'ΔF508', 'delF508', 'delta F508'.
    # Two digits at least: 'ΔR2', 'ΔT1' and 'delta C1' are regression, imaging and isotope notation, not variants.
    'protein, residue deletion': re.compile(
        rf'(?<![A-Za-z0-9])(?:(?:\u0394|[Dd]elta[\s-]?|del)\s?(?:[{_AA1}]|{_AA3})\d{{2,4}}(?![0-9])'
        rf'|(?:[{_AA1}]|{_AA3})\d{{2,4}}\u0394)'
    ),
    # A '*' read as a stop only when another does not follow it: markdown bolds with '**'.
    'protein, one-letter': re.compile(
        rf'(?<![A-Za-z0-9.])[{_AA1}]\d{{1,4}}(?:(?:[{_AA1}X]|\*(?!\*)){_FS_TAIL}|{_STOP}|fs\S*?|del|ins)(?![A-Za-z0-9])'
    ),
    'protein, three-letter': re.compile(
        rf'(?<![A-Za-z0-9.])(?:{_AA3})\d{{1,4}}(?:(?:{_AA3}|\*(?!\*)|X){_FS_TAIL}|{_STOP}|fs|del)(?![a-z])'
    ),
    # A nucleotide change as base, position, base, the position past what a residue change's would be: 'G20210A'.
    'nucleotide, base-position-base': re.compile(r'(?<![A-Za-z0-9.])[ACGTU]\d{1,6}[ACGTU](?![A-Za-z0-9])'),
    # A table row that prints the position in one cell and the change in the next: '157 | Arg→His', '470 | G→A'.
    'table, position and change': re.compile(
        rf'(?<![A-Za-z0-9.])\d+\s*\|\s*(?:(?:{_AA3})\s?(?:→|-+>)\s?(?:{_AA3}|stop|Stop|STOP|\*)'
        r'|[ACGTU]\s?(?:>|→|-+>)\s?[ACGTU]|del[ACGT]*|ins[ACGT]+|dup[ACGT]*)(?![A-Za-z])'
    ),
    # Legacy tables often print the position in a cell of its own: 'Asp→Asn', 'Arg→stop'.
    'protein, with an arrow': re.compile(
        rf'(?<![A-Za-z0-9.])(?:\d+\s?)?(?:{_AA3})(?:\s?\d+)?\s?(?:→|-+>)\s?(?:{_AA3}|stop|Stop|STOP|\*)(?![a-z])'
    ),
    'nucleotide': re.compile(r'(?<![A-Za-z0-9.>])-?\d+(?:[+-]\d+)?\s?[ACGTU]\s?(?:>|→|-+>)\s?[ACGTU](?![A-Za-z])'),
    'nucleotide, deletion or insertion': re.compile(
        r'(?<![A-Za-z0-9.])\d+(?:[+-]\d+)?(?:_\d+)?\s?(?:del[ACGT]*|ins[ACGT]+|dup[ACGT]*)(?![A-Za-z])'
    ),
    'rsID': re.compile(r'(?<![A-Za-z0-9])rs\d{3,}'),
    # A variant id as gnomAD and VCF-derived tables print it: chromosome, position, reference and alternate bases.
    'chromosome-position': re.compile(
        r'(?<![A-Za-z0-9.:\-])(?:chr)?(?:[1-9]|1\d|2[0-2]|X|Y|MT?)[-:]\d{3,9}[-:][ACGT]+[-:][ACGT]+(?![A-Za-z0-9\-])'
    ),
}


# A transcript or protein accession as a paper prints one. Genomic accessions name a build, not a gene's numbering.
_PRINTED_ACCESSION = re.compile(
    r'(?<![A-Za-z0-9_])((?:NM|NR|NP|XM|XR|XP)_\d{6,9}|ENS[TP]\d{11}|LRG_\d+[tp]\d+)(?:\.(\d+))?(?![0-9])'
)


@dataclasses.dataclass(frozen=True)
class PrintedAccession:
    """A transcript or protein accession the paper prints.

    Attributes:
        base: The accession without its version.
        versions: The versions printed with it, in order; empty when it is only printed unversioned.
        unversioned: Whether it is printed without a version anywhere.
        line: The line of the rendering it is first printed on, from 1.
        printings: How many times the rendering prints it.
    """

    base: str
    versions: tuple[str, ...]
    unversioned: bool
    line: int
    printings: int


@dataclasses.dataclass(frozen=True)
class Hit:
    """One distinct string the scan flagged.

    Attributes:
        id: The hit's number, from 1, in order of first printing.
        text: The string as printed.
        kind: Which pattern found it: "HGVS", or one of the loose patterns.
        line: The line of the rendering it is first printed on, from 1.
        printings: How many times the rendering prints it.
    """

    id: int
    text: str
    kind: str
    line: int
    printings: int


def normalise(text: str) -> str:
    """The text's letters and digits, lower-cased: how a hit is matched against a recorded mention."""
    return re.sub(r'[^0-9a-z]', '', text.lower())


def _parses(expression: str) -> bool:
    try:
        weaver.parse(expression)
    except weaver.HGVSError:
        return False
    return True


def _readings(printed: str) -> list[str]:
    """The ways to read a printed description: as printed, then with the renderer's typography undone.

    A dash is read as a hyphen or minus, or dropped; an arrow is read as '>'.
    """
    typed = printed.translate(_TYPOGRAPHY)
    dropped = re.sub(f'[{_DASHES}]', '', printed).translate(_TYPOGRAPHY)
    return list(dict.fromkeys((printed, typed, dropped)))


def _longest(window: str, prefix: str | None) -> int | None:
    """The end of the longest description at the start of `window` that weaver parses.

    Its reference is `prefix` where the paper prints one, else the placeholder.
    weaver accepts whitespace inside a description, so a longest match could take in the next word ("c.100del A
    novel" read as c.100delA): an end with no whitespace before it is preferred, and one inside whitespace is never
    taken.
    """
    for spaced in (False, True):
        for end in range(len(window), 2, -1):
            if window[end - 1].isspace() or (not spaced and any(c.isspace() for c in window[:end])):
                continue
            for printed in _readings(window[:end]):
                if (prefix is not None and _parses(f'{prefix}:{printed}')) or _parses(_PLACEHOLDER + printed):
                    return end
    return None


def parses_as_hgvs(text: str) -> bool:
    """Whether weaver parses the whole of `text` as one description, read past the renderer's typography.

    The reference is optional: a description printed without one is parsed against a placeholder.
    """
    return any(_parses(r) or _parses(_PLACEHOLDER + r) for r in _readings(text.strip()))


def _like(window: str) -> int | None:
    """The end of a description weaver cannot parse at the start of `window`, where it looks like one.

    It runs to the first whitespace, without trailing punctuation or a closing bracket it did not open.
    """
    if _HGVS_LIKE.match(window) is None:
        return None
    found = window.split(maxsplit=1)[0].rstrip(_TRAILING)
    while found and found[-1] in ')]' and found.count(found[-1]) > found.count('(' if found[-1] == ')' else '['):
        found = found[:-1].rstrip(_TRAILING)
    return len(found)


def _hgvs(text: str) -> list[tuple[int, int, str, str]]:
    """Each match at an anchor as its start, its end, the string as printed, and its kind.

    The kind is "HGVS" where weaver parses it, and "HGVS-like" where it does not but the string looks like a
    description, as a renderer's damage leaves one ('*' printed as 'a', a misspelt residue).
    """
    found = []
    for anchor in _ANCHOR.finditer(text):
        start = anchor.start()
        window = re.split(r'[\n|]', text[start : start + _HGVS_SPAN])[0]
        prefix = _PREFIX.search(text, max(0, start - _PREFIX_SPAN), start)
        end = _longest(window, prefix.group(1) if prefix is not None else None)
        if end is None:
            end = _like(window)
            if end is not None:
                found.append((start, start + end, window[:end], 'HGVS-like'))
            continue
        begin = start
        if prefix is not None and any(_parses(f'{prefix.group(1)}:{r}') for r in _readings(window[:end])):
            begin = prefix.start(1)
        found.append((begin, start + end, text[begin : start + end], 'HGVS'))
    return found


def scan(text: str) -> list[Hit]:
    """Flag the strings in a rendering that look like variant mentions.

    A match inside a match at an anchor, or inside an earlier loose pattern's match, is not flagged again.

    Args:
        text: The paper's rendering.

    Returns:
        One hit per distinct string, in order of first printing.
    """
    matches = _hgvs(text)
    for kind, pattern in _LOOSE.items():
        for match in pattern.finditer(text):
            if not any(start <= match.start() and match.end() <= end for start, end, _, _ in matches):
                matches.append((match.start(), match.end(), match.group(0), kind))
    first: dict[str, tuple[int, str]] = {}
    printings: dict[str, int] = {}
    for at, _, found, kind in sorted(matches):
        if found not in first:
            first[found] = (at, kind)
        printings[found] = printings.get(found, 0) + 1
    return [
        Hit(id=n, text=found, kind=kind, line=text.count('\n', 0, at) + 1, printings=printings[found])
        for n, (found, (at, kind)) in enumerate(first.items(), start=1)
    ]


# A hit this short in letters and digits is matched as printed, whitespace aside, and as a whole word: by letters and
# digits alone, or inside a longer number, it would match by chance ("G→A" inside "c.123G>A", "1G>A" in "c.11G>A").
_SHORT = 5


def surface() -> list[object]:
    """The scan's patterns and bounds: what decides its hits, which the agent is given and the screen settles on.

    What the extraction version digests: a change here changes which papers the agent reads and what it must cover.
    """
    return [
        _ANCHOR.pattern,
        _HGVS_LIKE.pattern,
        _PREFIX.pattern,
        _PLACEHOLDER,
        _DASHES,
        _HGVS_SPAN,
        _PREFIX_SPAN,
        _SHORT,
        {kind: pattern.pattern for kind, pattern in _LOOSE.items()},
        _PRINTED_ACCESSION.pattern,
    ]


def printed_accessions(text: str) -> list[PrintedAccession]:
    """The transcript and protein accessions a rendering prints, in order of first printing."""
    found: dict[str, list[re.Match[str]]] = {}
    for match in _PRINTED_ACCESSION.finditer(text):
        found.setdefault(match[1], []).append(match)
    return [
        PrintedAccession(
            base=base,
            versions=tuple(dict.fromkeys(m[2] for m in matches if m[2] is not None)),
            unversioned=any(m[2] is None for m in matches),
            line=text.count('\n', 0, matches[0].start()) + 1,
            printings=len(matches),
        )
        for base, matches in found.items()
    ]


def covers(verbatim: str, hit: Hit) -> bool:
    """Whether a mention printed as `verbatim` accounts for a hit: one prints the other, letters and digits alone.

    A mention that records part of an HGVS hit (its change without the printed accession) covers it.
    """
    mention, flagged = normalise(verbatim), normalise(hit.text)
    if len(mention) < _SHORT:
        # A short mention inside a longer hit would cover it by chance: "G→A" inside "470 | G→A".
        return mention == flagged
    if len(flagged) < _SHORT:
        printed = re.escape(''.join(hit.text.split()))
        return re.search(f'(?<![A-Za-z0-9]){printed}(?![A-Za-z0-9])', ''.join(verbatim.split())) is not None
    return bool(mention) and (flagged in mention or mention in flagged)
