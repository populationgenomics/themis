r"""Run the variant extraction agent over a sample of papers in each reading, and compare the readings.

The experiment in docs/design/variant-extraction.md ("What the agent reads"): the same papers, read as (a) markdown,
(b) markdown and page images, (c) page images alone. Each run writes, per paper, the mention set, the verdicts and
the run's measurements under ``--out``; nothing is written to the store. The outputs hold text from licensed papers,
so ``--out`` belongs on local disk, never in the repository.

    uv run --group litcache python -m tools.litcache.variant_experiment sample \\
        --october ~/.cache/themis-variant-exp/october --per-era 1 --out ~/.cache/themis-variant-exp/pilot
    ANTHROPIC_API_KEY=… uv run --group litcache python -m tools.litcache.variant_experiment run \\
        --references gs://cpg-themis-dev-resources --out ~/.cache/themis-variant-exp/pilot --arm markdown
    uv run --group litcache python -m tools.litcache.variant_experiment report --out ~/.cache/themis-variant-exp/pilot

``report`` prints measurements only: counts, shares, similarities and cost, never a mention's text.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import json
import pathlib
import random
import sys
import time
import traceback
from collections.abc import Sequence

import anthropic
import anthropic.types
import pyarrow.parquet as pq
import weaver
from google.cloud import storage

from themis.litcache import pdf, records, variant_extract, variant_fit, variant_reference, variant_validate, writer
from themis.litcache.models import litcache_pb2, variants_pb2

_PROJECT = 'cpg-themis-dev'
_BUCKET = f'{_PROJECT}-fulltext'
_SONNET, _HAIKU, _OPUS = 'claude-sonnet-5-5', 'claude-haiku-4-5', 'claude-opus-5-5'
_REFERENCES_HELP = f"the pinned reference builds' root: gs://{_PROJECT}-resources, or a local copy of its layout"


def _references(root: str) -> variant_reference.References:
    resolved = root if root.startswith('gs://') else str(pathlib.Path(root).expanduser())
    return variant_reference.References(resolved, variant_reference.BUILDS)


@dataclasses.dataclass(frozen=True)
class _Arm:
    reading: variant_extract.Reading
    model: str
    effort: variant_extract.Effort | None
    scan: bool = False


# Each arm is a reading on a model. A _repeat arm runs its arm again, so run-to-run noise can be told from a
# difference between arms; the haiku and opus arms vary the model on the cheapest reading, and the scan arms give the
# agent the markdown's scan hits to cover or dismiss, and the low arms run that at low effort.
_ARMS = {
    'markdown': _Arm('markdown', _SONNET, 'medium'),
    'markdown_and_pages': _Arm('markdown_and_pages', _SONNET, 'medium'),
    'pages': _Arm('pages', _SONNET, 'medium'),
    'markdown_repeat': _Arm('markdown', _SONNET, 'medium'),
    'markdown_haiku': _Arm('markdown', _HAIKU, None),
    'markdown_opus': _Arm('markdown', _OPUS, 'medium'),
    'markdown_scan': _Arm('markdown', _SONNET, 'medium', scan=True),
    'markdown_scan_repeat': _Arm('markdown', _SONNET, 'medium', scan=True),
    'markdown_scan_low': _Arm('markdown', _SONNET, 'low', scan=True),
    'markdown_scan_low_repeat': _Arm('markdown', _SONNET, 'low', scan=True),
}
_ERAS = (('pre-2000', 0, 1999), ('2000-2009', 2000, 2009), ('2010-on', 2010, 9999))
# Per million tokens (the claude-api skill, 2026-10-30; cache writes at 1.25x input, reads at 0.1x, Opus's at 0.05x).
_PRICES = {
    _SONNET: {'input': 2.0, 'output': 10.0, 'cache_write': 2.5, 'cache_read': 0.2},
    _HAIKU: {'input': 1.0, 'output': 5.0, 'cache_write': 1.25, 'cache_read': 0.1},
    _OPUS: {'input': 4.0, 'output': 20.0, 'cache_write': 5.0, 'cache_read': 0.2},
}


class _Tokens:
    """A `TokenCounter` that sums a run's usage."""

    def __init__(self, model: str) -> None:
        self.totals: collections.Counter[str] = collections.Counter()
        self._prices = _PRICES[model]

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:  # noqa: ARG002
        self.totals['input'] += usage.input_tokens
        self.totals['output'] += usage.output_tokens
        self.totals['cache_write'] += usage.cache_creation_input_tokens or 0
        self.totals['cache_read'] += usage.cache_read_input_tokens or 0

    def cost(self) -> float:
        return sum(self.totals[k] * price for k, price in self._prices.items()) / 1e6


@dataclasses.dataclass(frozen=True)
class _Paper:
    doc_id: str
    year: int
    era: str
    converter: str
    pages: int
    october_mentions: int


def _manifest(bucket: storage.Bucket, doc_id: str) -> litcache_pb2.Manifest:
    return litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())


def _pdf_revision(manifest: litcache_pb2.Manifest) -> tuple[litcache_pb2.Source, litcache_pb2.Revision] | None:
    for source in manifest.sources:
        if source.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF and source.revisions:
            return source, max(source.revisions, key=lambda r: r.captured_at.ToDatetime())
    return None


def _inputs(bucket: storage.Bucket, doc_id: str) -> tuple[str, bytes]:
    """The paper's current markdown and its pdf."""
    manifest = _manifest(bucket, doc_id)
    found = records.markdown(manifest)
    held = _pdf_revision(manifest)
    if found is None or held is None:
        raise ValueError(f'{doc_id} lacks a markdown rendering or a pdf')
    key, _ = found
    markdown = bucket.blob(f'{writer.paper_dir(doc_id)}/renderings/{key}.md').download_as_text()
    source, revision = held
    pdf_bytes = bucket.blob(
        writer.source_revision_path(doc_id, source.handle, revision.hash, source.media_type)
    ).download_as_bytes()
    return markdown, pdf_bytes


def _sample(args: argparse.Namespace) -> None:
    october = pathlib.Path(args.october).expanduser()
    papers = pq.read_table(october / 'papers.parquet', columns=['arm', 'doc_id', 'year']).to_pylist()
    mentions = pq.read_table(october / 'mentions.parquet', columns=['arm', 'doc_id', 'section']).to_pylist()
    counts = collections.Counter(m['doc_id'] for m in mentions if m['arm'] == 'fulltext')
    in_tables = collections.Counter(m['doc_id'] for m in mentions if m['arm'] == 'fulltext' and m['section'] == 'table')
    excluded = {p['doc_id'] for path in args.exclude for p in json.loads(pathlib.Path(path).expanduser().read_text())}
    pool = [
        p
        for p in papers
        if p['arm'] == 'fulltext'
        and p['doc_id']
        and p['doc_id'] not in excluded
        and args.min_mentions <= counts[p['doc_id']] <= args.max_mentions
        and in_tables[p['doc_id']] >= args.min_table_mentions
    ]
    rng = random.Random(args.seed)  # noqa: S311 — a reproducible sample, not a secret
    rng.shuffle(pool)
    bucket = storage.Client(project=_PROJECT).bucket(_BUCKET)
    chosen: list[_Paper] = []
    for era, low, high in _ERAS:
        taken = 0
        for p in pool:
            if taken == args.per_era:
                break
            if not low <= (p['year'] or 0) <= high:
                continue
            manifest = _manifest(bucket, p['doc_id'])
            found = records.markdown(manifest)
            held = _pdf_revision(manifest)
            if found is None or held is None:
                continue
            converter = litcache_pb2.Converter.Name(found[1].rendering.converter).removeprefix('CONVERTER_').lower()
            if args.converter and converter != args.converter:
                continue
            source, revision = held
            pdf_bytes = bucket.blob(
                writer.source_revision_path(p['doc_id'], source.handle, revision.hash, source.media_type)
            ).download_as_bytes()
            pages = pdf.page_count(pdf_bytes).result()
            if not args.min_pages <= pages <= args.max_pages:
                continue
            chosen.append(_Paper(p['doc_id'], p['year'], era, converter, pages, counts[p['doc_id']]))
            taken += 1
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'sample.json').write_text(json.dumps([dataclasses.asdict(p) for p in chosen], indent=1))
    for p in chosen:
        print(p.era, p.year, p.converter, f'{p.pages} pages', f'{p.october_mentions} October mentions', p.doc_id)


async def _run_one(
    paper: dict, reading: str, out: pathlib.Path, references: variant_reference.References, bucket: storage.Bucket
) -> None:
    target = out / reading / paper['doc_id']
    if (target / 'stats.json').exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    markdown, pdf_bytes = await asyncio.to_thread(_inputs, bucket, paper['doc_id'])
    arm = _ARMS[reading]
    tokens = _Tokens(arm.model)
    started = time.monotonic()
    stats: dict[str, object] = {'doc_id': paper['doc_id'], 'reading': reading}
    try:
        async with anthropic.AsyncAnthropic(max_retries=4, timeout=900) as client:
            extraction = await variant_extract.extract(
                client=client,
                references=references,
                reading=arm.reading,
                markdown=markdown,
                pdf_bytes=pdf_bytes,
                tokens=tokens,
                model=arm.model,
                effort=arm.effort,
                scan=arm.scan,
            )
    except variant_extract.ExtractionError as error:
        # Kept, marked incomplete: what the agent recorded before it refused or ran out of turns.
        (target / 'partial.pb').write_bytes(error.partial.SerializeToString())
        _failed(
            target,
            stats
            | {
                'turns': error.turns,
                'partial_mentions': len(error.partial.mentions),
                'refusal': error.refusal,
                'tool_calls_before': dict(error.tool_calls),
            },
            error,
            tokens,
        )
        print(paper['doc_id'], reading, 'incomplete:', error, file=sys.stderr, flush=True)
        return
    except Exception as error:  # noqa: BLE001 — one paper's failure is recorded against it, and the run goes on
        _failed(target, stats, error, tokens)
        print(paper['doc_id'], reading, 'failed:', type(error).__name__, file=sys.stderr, flush=True)
        return
    # The mention set is kept before validation runs, so a validation failure does not lose what was paid for.
    (target / 'mentions.pb').write_bytes(extraction.mention_set.SerializeToString())
    try:
        text = markdown if arm.reading != 'pages' else await asyncio.wrap_future(pdf.text_layer(pdf_bytes))
        mention_set, verdicts, construction = await asyncio.to_thread(
            variant_validate.validate, extraction.mention_set, text=text, references=references
        )
    except Exception as error:  # noqa: BLE001 — as above; `revalidate` can make the verdicts once it is fixed
        _failed(target, stats | {'validation_failed': True}, error, tokens)
        print(paper['doc_id'], reading, 'validation failed:', type(error).__name__, file=sys.stderr, flush=True)
        return
    (target / 'mentions.pb').write_bytes(mention_set.SerializeToString())
    (target / 'verdicts.pb').write_bytes(verdicts.SerializeToString())
    stats |= {
        'harness': extraction.construction.harness_version,
        'dismissals': len(extraction.mention_set.dismissals),
        'model': extraction.construction.model,
        'turns': extraction.turns,
        'tool_calls': dict(extraction.tool_calls),
        'tokens': tokens.totals,
        'cost': tokens.cost(),
        'seconds': round(time.monotonic() - started),
        'weaver': construction.weaver_version,
    }
    (target / 'stats.json').write_text(json.dumps(stats))
    print(
        paper['doc_id'],
        reading,
        f'{extraction.turns} turns',
        f'${tokens.cost():.2f}',
        f'{stats["seconds"]}s',
        flush=True,
    )


def _failed(target: pathlib.Path, stats: dict[str, object], error: Exception, tokens: _Tokens) -> None:
    stats = stats | {
        'error': f'{type(error).__name__}: {error}',
        'traceback': traceback.format_exc(),
        'tokens': tokens.totals,
        'cost': tokens.cost(),
    }
    (target / 'stats.json').write_text(json.dumps(stats))


def _spent(out: pathlib.Path) -> float:
    """What every finished run under `out` cost, from its recorded usage."""
    return sum(json.loads(stats.read_text())['cost'] for stats in out.glob('*/*/stats.json'))


async def _run(args: argparse.Namespace) -> None:
    out = pathlib.Path(args.out).expanduser()
    papers = json.loads((out / 'sample.json').read_text())
    references = _references(args.references)
    bucket = storage.Client(project=_PROJECT).bucket(_BUCKET)
    limit = asyncio.Semaphore(args.concurrency)

    async def bounded(paper: dict, reading: str) -> None:
        async with limit:
            spent = _spent(out)
            if spent >= args.budget:
                print(paper['doc_id'], reading, f'skipped: ${spent:.2f} spent of ${args.budget:.2f}', file=sys.stderr)
                return
            await _run_one(paper, reading, out, references, bucket)

    await asyncio.gather(*(bounded(p, r) for p in papers for r in args.arm))


def _revalidate(args: argparse.Namespace) -> None:
    """Make the verdicts again from the stored mention sets: free, and what a weaver or validator change runs."""
    out = pathlib.Path(args.out).expanduser()
    papers = json.loads((out / 'sample.json').read_text())
    references = _references(args.references)
    bucket = storage.Client(project=_PROJECT).bucket(_BUCKET)
    for paper in papers:
        markdown, pdf_bytes = _inputs(bucket, paper['doc_id'])
        layer = None
        for reading in _ARMS:
            directory = out / reading / paper['doc_id']
            if not (directory / 'mentions.pb').exists():
                continue
            if reading == 'pages' and layer is None:
                layer = pdf.text_layer(pdf_bytes).result()
            text = layer if reading == 'pages' and layer is not None else markdown
            mentions = variants_pb2.MentionSet.FromString((directory / 'mentions.pb').read_bytes())
            mentions, verdicts, _ = variant_validate.validate(mentions, text=text, references=references)
            (directory / 'mentions.pb').write_bytes(mentions.SerializeToString())
            (directory / 'verdicts.pb').write_bytes(verdicts.SerializeToString())
            stats_path = directory / 'stats.json'
            stats = json.loads(stats_path.read_text())
            if stats.pop('validation_failed', False):
                stats.pop('error')
                stats.pop('traceback')
                stats_path.write_text(json.dumps(stats))
    print('revalidated')


def _load(directory: pathlib.Path) -> tuple[dict, variants_pb2.MentionSet | None, variants_pb2.Verdicts | None]:
    stats = json.loads((directory / 'stats.json').read_text())
    if 'error' in stats:
        return stats, None, None
    mentions = variants_pb2.MentionSet.FromString((directory / 'mentions.pb').read_bytes())
    verdicts = variants_pb2.Verdicts.FromString((directory / 'verdicts.pb').read_bytes())
    return stats, mentions, verdicts


def _verbatims(mentions: variants_pb2.MentionSet) -> set[str]:
    return {
        ''.join(m.verbatim.split()).lower() for m in mentions.mentions if m.known_species == variants_pb2.SPECIES_HUMAN
    }


def _alleles(
    mentions: variants_pb2.MentionSet, verdicts: variants_pb2.Verdicts, references: variant_reference.References
) -> set[str]:
    return {
        variant_validate.identity(m.candidates[v.chosen].hgvs, references).key()
        for m, v in zip(mentions.mentions, verdicts.mentions, strict=True)
        if v.choice == variants_pb2.CHOICE_CHOSEN
    }


def _reference_printed(candidate: variants_pb2.Candidate, mention: variants_pb2.Mention) -> bool | None:
    """Whether the base or residue a candidate states is printed in its mention; None when it states none.

    A candidate whose stated reference the mention does not print may have taken it from the sequence it is checked
    against, which would make its validation circular.
    """
    try:
        variant = weaver.parse(candidate.hgvs)
    except weaver.HGVSError:
        return None
    posedit = variant.to_dict().get('posedit')
    if not posedit or not posedit.get('pos'):
        return None  # an allele in cis, or no position: not measured here
    printed = mention.verbatim
    if variant.coordinate_type == 'p':
        residue = posedit['pos']['start'].get('aa')
        if not residue:
            return None
        return residue.lower() in printed.lower() or variant_validate.ONE_LETTER.get(residue, '?') in mention.verbatim
    reference = (posedit.get('edit') or {}).get('ref_')
    if not reference:
        return None
    return reference.upper() in ''.join(printed.upper().split())


def _jaccard(a: set[str], b: set[str]) -> float | None:
    return len(a & b) / len(a | b) if a | b else None


def _report(args: argparse.Namespace) -> None:
    out = pathlib.Path(args.out).expanduser()
    references = _references(args.references)
    papers = json.loads((out / 'sample.json').read_text())
    readings = [r for r in _ARMS if (out / r).is_dir()]
    totals: dict[str, collections.Counter[str]] = {r: collections.Counter() for r in readings}
    anchors: dict[str, collections.Counter[str]] = {r: collections.Counter() for r in readings}
    per_paper: dict[str, dict[str, tuple]] = collections.defaultdict(dict)
    for reading in readings:
        for paper in papers:
            directory = out / reading / paper['doc_id']
            if not (directory / 'stats.json').exists():
                continue
            stats, mentions, verdicts = _load(directory)
            t = totals[reading]
            t['papers'] += 1
            t['cost_cents'] += round(stats['cost'] * 100)
            if mentions is None or verdicts is None:
                t['failed'] += 1
                continue
            t['turns'] += stats.get('turns', 0)
            t['seconds'] += stats.get('seconds', 0)
            t['mentions'] += len(mentions.mentions)
            t['human'] += sum(m.known_species == variants_pb2.SPECIES_HUMAN for m in mentions.mentions)
            agents = variant_fit.without_derived(mentions)
            t['candidates'] += sum(len(m.candidates) for m in agents.mentions)
            for m, v in zip(mentions.mentions, verdicts.mentions, strict=True):
                t[
                    f'disposition {variants_pb2.Disposition.Name(m.disposition).removeprefix("DISPOSITION_").lower()}'
                ] += 1
                t[f'choice {variants_pb2.Choice.Name(v.choice).removeprefix("CHOICE_").lower()}'] += 1
                t['occurrences'] += len(m.occurrences)
                t['occurrences on a page'] += sum(o.HasField('page') for o in m.occurrences)
                t['occurrences in a figure box'] += sum(o.HasField('box') for o in m.occurrences)
                if v.choice == variants_pb2.CHOICE_CHOSEN:
                    anchor = v.candidates[v.chosen].anchor
                    anchors[reading][variants_pb2.Anchor.Name(anchor).removeprefix('ANCHOR_').lower()] += 1
                    chosen = m.candidates[v.chosen]
                    if any(r.kind in variant_fit.DERIVED_REWRITES for r in chosen.rewrites):
                        t['chosen derived'] += 1
                    else:
                        t['chosen after attempts'] += sum(
                            not any(r.kind in variant_fit.DERIVED_REWRITES for r in c.rewrites)
                            for c in m.candidates[: v.chosen]
                        )
                    printed = _reference_printed(chosen, m)
                    if printed is False:
                        t['chosen whose stated reference is not printed'] += 1
                    t['chosen with a rewrite beyond the accession'] += any(
                        r.kind != variants_pb2.REWRITE_KIND_REFERENCE and r.kind not in variant_fit.DERIVED_REWRITES
                        for r in chosen.rewrites
                    )
            per_paper[paper['doc_id']][reading] = (_verbatims(mentions), _alleles(mentions, verdicts, references))
    print('per reading:')
    for reading in readings:
        t = totals[reading]
        print(f'  {reading}: ' + ', '.join(f'{k}={v}' for k, v in sorted(t.items())))
        print(f'    chosen anchors: {dict(anchors[reading])}')
    print('agreement between readings (Jaccard, mean over papers both finished):')
    for i, a in enumerate(readings):
        for b in readings[i + 1 :]:
            for label, index in (('verbatims', 0), ('chosen alleles', 1)):
                scores = [
                    s
                    for runs in per_paper.values()
                    if a in runs and b in runs
                    for s in [_jaccard(runs[a][index], runs[b][index])]
                    if s is not None
                ]
                if scores:
                    print(f'  {a} vs {b}, {label}: {sum(scores) / len(scores):.2f} over {len(scores)} papers')
    print('per paper (era, converter, pages; per reading: human mentions, chosen alleles, cost):')
    for paper in papers:
        cells = []
        for reading in readings:
            directory = out / reading / paper['doc_id']
            if not (directory / 'stats.json').exists():
                cells.append(f'{reading}: -')
                continue
            stats, mentions, verdicts = _load(directory)
            if mentions is None or verdicts is None:
                cells.append(f'{reading}: failed ${stats["cost"]:.2f}')
                continue
            cells.append(
                f'{reading}: {len(_verbatims(mentions))} / {len(_alleles(mentions, verdicts, references))} '
                f'${stats["cost"]:.2f}'
            )
        print(f'  {paper["era"]} {paper["converter"]} {paper["pages"]}p: ' + '; '.join(cells))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    sample = commands.add_parser('sample', help='draw papers from the October extraction, stratified by era')
    sample.add_argument('--october', required=True, help='directory holding papers.parquet and mentions.parquet')
    sample.add_argument('--out', required=True)
    sample.add_argument('--per-era', type=int, required=True)
    sample.add_argument('--min-mentions', type=int, default=3)
    sample.add_argument('--max-mentions', type=int, default=60)
    sample.add_argument('--min-pages', type=int, default=4)
    sample.add_argument('--max-pages', type=int, default=20)
    sample.add_argument('--seed', type=int, default=20261030)
    sample.add_argument('--converter', choices=['litdown', 'docling', 'llm_ocr'], help='only papers whose text it made')
    sample.add_argument('--min-table-mentions', type=int, default=0, help='October mentions printed in a table')
    sample.add_argument(
        '--exclude',
        action='append',
        default=[],
        help='a sample.json whose papers are not drawn again: a held-out sample',
    )
    run = commands.add_parser('run', help='run the agent over the sample in the given readings')
    run.add_argument('--references', required=True, help=_REFERENCES_HELP)
    run.add_argument('--out', required=True)
    run.add_argument('--arm', action='append', choices=sorted(_ARMS), required=True)
    run.add_argument('--concurrency', type=int, default=3)
    run.add_argument(
        '--budget', type=float, required=True, help='stop starting papers once this many dollars are spent'
    )
    revalidate = commands.add_parser('revalidate', help='make the verdicts again from the stored mention sets')
    revalidate.add_argument('--references', required=True, help=_REFERENCES_HELP)
    revalidate.add_argument('--out', required=True)
    report = commands.add_parser('report', help='measurements across readings, never mention text')
    report.add_argument('--references', required=True, help=_REFERENCES_HELP)
    report.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if args.command == 'sample':
        _sample(args)
    elif args.command == 'run':
        asyncio.run(_run(args))
    elif args.command == 'revalidate':
        _revalidate(args)
    else:
        _report(args)


if __name__ == '__main__':
    main()
