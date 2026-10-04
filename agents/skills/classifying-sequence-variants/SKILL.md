---
name: classifying-sequence-variants
description: Classifies one germline sequence variant against one gene-disease entity under the draft ClinGen/ACMG SVCv4 framework, returning a pathogenicity class with a reproducible point tally, per-claim provenance, and a working document for a curator. Covers missense, nonsense, frameshift, splice, in-frame indel, start-lost, stop-lost, and exon deletion or duplication variants. Use whenever a request names a variant (transcript or protein HGVS, a gene symbol with a change, an rsID, a ClinVar or gnomAD identifier), with or without a condition or phenotype, or asks whether a variant is pathogenic, benign or a VUS. A request naming a proband or sample with a presentation and no specific variant is a case analysis, not a classification.
---

# Classifying a sequence variant

Classify **one** variant against **one** Monogenic Disease Entity (MDE: gene × phenotype × inheritance × mechanism)
under draft SVCv4, and write the working document a curator reviews. Where SVCv4 defers to "an experienced analyst",
that is you.

- **The evidence services retrieve**: `variant`, `clinvar`, `vep`, `gnomad`, `gene_disease`, `transcript`, `splice`,
  `mavedb`, `cspec`, `literature`. The sandbox has no credentials and no egress. Filter what they return yourself.
- **`web_search`** runs outside the sandbox and returns titles, links and excerpts, never a whole document; nothing here
  opens a link it returns. Use it to find what the services missed: a paper's PMID or DOI, which you then hand to the
  `literature` service to read the paper, or the name and version of a document no service holds, such as a registry
  entry or a society guideline, which goes on the deposit list with the question it would answer. Take any value a
  service serves from the service. A search excerpt grounds no scored code, and you mark it unverifiable wherever you
  use it.
- **`themis.svcv4` computes every point.** You supply the judgement inputs and read its trail back. Never compute a
  total, band or class by hand. The exception is the `CLN_*`/`LOC_*` case codes: you reduce them from the case facts and
  price them with `observations.total`, and the classification record carries the cells you priced.
- **You judge** the MDE, mechanism level, exon relevance, informative-variant eligibility, assay concordance, DAFT
  parameters, critical-residue and multiple-disorder calls, and the verdict. Give each its reasoning, an explicit
  uncertainty and its provenance. The verdict rests on your reasoning over the evidence, not on the total.

## Draft framework

Write **draft** whenever you state a framework value. Put this notice under the document's title, verbatim:

```
> **Draft framework — evaluation only, not for clinical use.** This document applies the ACMG/AMP/CAP/ClinGen
> SVCv4 framework as released in the **July 2026 pilot**. Its point values, thresholds and code names may change
> before publication. This is **not a validated implementation and not a clinical variant classification**; no
> clinical or diagnostic decision may rest on it. Every value must be re-verified against the final published
> standard and the ClinGen Pilot Calculator.
```

A non-coding variant is out of scope because SM17 is unreleased. Say so, and stop after the ClinVar and literature
evidence.

## The sandbox

- Write Python and run it with `shell`. Every call needs a short present-tense `intent`. A call is killed after about
  110 s and every rpc has its own deadline, so put independent retrievals in separate `shell` calls.
- `/workspace` is a git clone of this Analysis's repository, with `origin` as the store, and the only directory the file
  tools reach. Commit and push whatever must outlive the run, the working document above all. At teardown the worker
  pushes commits you left unpushed and nothing else, and the next run starts from what was pushed. History is
  append-only: when a push is refused, `git pull --rebase` and push again. Never force-push.
- `/workspace/scratch/` is ignored, never pushed, and gone after the run. Snippets, the response cache
  (`cache_dir='/workspace/scratch/cache'`) and captured full texts go there. Full texts are licensed: never copy them
  elsewhere or commit them. Only a validated `:quote` passage enters the document. Your sub-agent threads share this
  clone, so name snippets for your thread.
- `/workspace/skills/` is the platform's.

```python
from themis.agent import services, retry, display
from themis.rpc import variant_pb2, clinvar_pb2, literature_pb2   # one module per service
from themis.evidence.models import evidence_pb2                   # shared types: Consequence, Provenance, GenomicSpan

variant = services.variant()                                      # one accessor per service; get it once
resp = retry.call(variant.Normalize, variant_pb2.NormalizeRequest(variant='NM_000518.5:c.20A>T', genome_build='GRCh38'),
                  cache_dir='/workspace/scratch/cache')
display.show(resp)
```

- Before a service's first call, read its contract: `cat /usr/local/share/themis/proto/themis/rpc/<name>.proto`, and
  `/usr/local/share/themis/proto/themis/evidence/models/evidence.proto` for the shared types. The proto is authoritative
  for fields, required inputs, status codes, and what an absence means for that source.
- A request message is named for the rpc, not the service (`Splice.PredictDeltas` takes
  `splice_pb2.PredictDeltasRequest`), and takes keyword arguments only.
- `retry.call` retries transient failures under one time budget and re-raises a settled answer (`NOT_FOUND`,
  `INVALID_ARGUMENT`).
- Read every response with `display.show(resp)`, never by printing the fields you thought of: the field you did not name
  is often the one explaining an empty result. A submessage shown as a marker renders whole with
  `display.show(resp.<field>)`. An oversized `Struct` shows only its top-level keys; read it from the message.
- `NOT_FOUND` arrives as a raised `grpc.RpcError` and is a finding. Record the negative as exactly what the proto says
  it means.

## The library's doors

Read a module's docstring before using it (`python3 -c 'import themis.svcv4.frequency as m; print(m.__doc__)'`), and
`help()` a function for its contract. What each door is for:

- `builders.classify_variant(ref, consequence=…, evidence=…, independent_codes=…, gate_level=…)` routes and scores.
  `consequence` is `Normalize`'s. `evidence` is the family for the variant's arm (`MissensePaths`, `NullEvidence`,
  `CodingEvidence`, `SpliceEvidence`, `DuplicationEvidence`), and the initial PRD tier inside it is your decision-tree
  call. A whole-gene deletion goes through `builders.build_exon_deletion(…, whole_gene=True)` instead.
  `ref = data.load_reference()`.
- `frequency`: the POP_FRQ FAF from a `Gnomad.DescribeVariant` response (`absent_faf()` where it was `NOT_FOUND`),
  POP_HMZ, and the DAFT from a ClinVar gene pool. The DAFT needs one `Gnomad.DescribeVariant` per pooled record: spread
  them across `shell` calls with `cache_dir`.
- `nmd`: NMD from a `GetStructure` exon table, or from a `PredictSkipOutcome` skip.
- `predictor_policy`: the gene's one MIS_PRD predictor, the `Vep.Annotate` request for it, and its bin.
- `placement`: a ClinVar pool placed against a codon or an exon, for the `*_INF` rules and the SM18 §17 waiver.
- `gate_level`: pass the resolved entity's `gate_level` from `DescribeGene`. `gene_disease_validity.gate_level` maps a
  classification you hold from elsewhere.
- `observations`: prices the `CLN_*`/`LOC_*` cells.
- `functional`, `grantham`, `splice_tree`: FXN, MIS_INF, and the splice colour.

A nonsense variant with NMD predicted, on the null arm:

```python
import decimal
from themis.svcv4 import builders, data, frequency, scoring

ref = data.load_reference()
result = builders.classify_variant(
    ref,
    consequence=normalized.consequence,                    # Variant.Normalize's routing key
    evidence=builders.NullEvidence(
        nul_prd=decimal.Decimal('6'),                      # your decision-tree tier: NMD removes the product
        mechanism=scoring.MechanismLevel.ESTABLISHED,      # your call against the GenCC rubric (below)
        exon=scoring.ExonRelevance.ALL,                    # your call from AssessExonRelevance's inventory and pext
        nul_inf=scoring.informative_points(('P', 'LP')),   # distinct, same-MDE informative variants you judged eligible
    ),
    independent_codes=[frequency.pop_frq(ref, frequency.faf_from_gnomad(gnomad_resp), daft)],
    gate_level=entity.gate_level,                          # the resolved entity's, from DescribeGene
)
# result.total, result.band, result.vus_subband, result.final_class, result.gate_capped, result.contributions
```

When the library raises, your judgement inputs disagree with each other or with the gate. Re-derive them rather than
trimming a value to fit.

## Judgements the library cannot make

- **The entity.** The kickoff normally names no condition. Call `DescribeGene` on `Normalize`'s `hgnc_id` for the
  gene's curated entities, choose the one the presentation fits, and re-issue with `mondo_id` and `inheritance` to
  resolve it. Map a named condition to a MONDO term yourself. Where several MDEs are candidates, name them, say why you
  chose (SM21), and say which calls differ; a presentation too thin to choose between them is a finding. Never aggregate
  across mutually exclusive mechanisms.
- **The mechanism level** is a point sum over the GenCC LoF rubric in `data/gencc-lof-mechanism-framework.md`, beside
  `themis.svcv4.__file__`. Read the criteria there, never from web text, and cite the version the file names. Show the
  criteria met and the sum. Uncertain (×0) is the floor when the evidence cannot support a call, not a default for a
  missing haploinsufficiency score. A gate below Moderate forces Uncertain.
- **Exon relevance** is membership over `AssessExonRelevance`'s `transcript_inventory`, with abundance deciding which
  memberships count: pext against the gene's other exons, and GTEx in the disease tissue. Name the denominator, the
  transcripts you admitted and why, and the tissue. An rpc you did not issue is a retrieval to finish, not an open axis.
- **An open input reports no class.** An input is open when more than one value survives the evidence and no framework
  disposition settles it. Report each surviving value with the class it yields. Where they agree, report that class and
  say the input is open; where they differ, the class is **not established**, with the classes in contention. Never pick
  a value to report from. Where the framework disposes of the input (×0 for an unassessed mechanism, SM3's high
  conservative DAFT, FXN 0.0 for an assay off the disease-relevant function), apply the disposition.
- **A VCEP classification is evidence, not an answer.** VCEP specifications are ACMG/AMP-2015 rule sets, and a
  classification transfers only within its own framework. Read the submission's `assertion_method` and run the full
  assessment. Use the VCEP's criteria, comment and cited papers as inputs to the codes they bear on, and name the code
  that carries any divergence. Adopt and stop only when the VCEP ran SVCv4 and nothing patient-specific rules it out.
  `Cspec.ListSpecifications` serves the panel's text; a 2015 frequency cutoff earns SM3's first rung only through its
  own derivation.
- **`LOC_PHE`'s diagnostic yield** is a published cohort statistic, reached through the literature. It is keyed on the
  phenotype of the most specifically phenotyped carrier of the variant, internal or published, not the testee's by
  default. Award it only where the cohort's testing method matches this case's and its phenotype definition fits that
  carrier; otherwise 0.0, or ND where no yield data exist. State whether you read it gene-keyed (the worked examples) or
  pan-genomically (SM5). A gene's share of a phenotype, a by-method partition, a multi-gene panel's yield, and a
  numerator counting VUS are not yields.
- **SM4's biallelic weights** read gnomAD v2's categorical co-occurrence table. `Gnomad.DescribeVariant` with
  `cooccurrence_with` answers a different question, about one pair. Say which your number came from.
- **One fact funds one code**: the code whose definition it measures. Name it, say why the other is not charged, and
  cite the paragraph for any exception you claim.

## Intake

The kickoff states the transcript, the coding change, and the clinical context in the referring clinician's words, and
nothing else. Read the case facts the `CLN_*`/`LOC_*` codes need from that text, each with its basis: the phenotype and
how specific it is, the proband count, phase, segregation, the assay modality and scope, the genes analysed and their
coverage, other candidate variants, and the non-genetic work-up. Where the text is silent, state the absence ("the
testing modality is not stated") and leave the code open or ND. Never default: an unstated modality is not exome, and
silence about other variants is not "none found". Decide non-genetic etiology yourself, per MDE, or say it cannot be
decided.

## The workflow

1. **Judgements.** On a turn that finds an SVCv4 classification committed, first run
   `widgets.judgements('assets/svcv4.binpb')` (`from themis.agent import widgets`). It prints each tick and note a
   curator has made. A note is the curator's answer on the code, the routing or the record it sits on: treat it as
   §"Questions for the curator" treats an answer.
1. **Normalize.** `Variant.Normalize` gives the canonical ids, `gene_symbol`, `hgnc_id`, `consequence`, the transcript
   projections (use the MANE Select RefSeq `NM_`), and the ClinVar crosswalk `clinvar_variations[]`. Establish the MDE.
   On `FAILED_PRECONDITION` from `DescribeGene`, restate the entity rather than broadening the term.
1. **ClinVar.** `ClinVar.DescribeVariant` once per crosswalk entry, with its `vcv`, or with `vcv` unset where the crosswalk is empty. An empty crosswalk supports novelty only up to the last
   ClinVar ingest. Read `review_status`, `submissions[]` (`assertion_method`, `comment`, `observations[]`,
   `pubmed_ids[]`) and the pool census. Whether two agreeing labs are independent depends on what each applied and
   observed. Re-issue with a neighbouring variant's own accession to weigh it.
1. **Position.** `Transcript.GetStructure` at the c. position. `Splice.PredictDeltas` for every variant type, and `Splice.PredictSkipOutcome` for the product where a
   splice effect is plausible.
1. **The rest**, in separate `shell` calls: `Vep.Annotate` (through `annotate_request`), `Gnomad.DescribeVariant`,
   `Transcript.AssessExonRelevance`, `MaveDb.DescribeVariant`, `Cspec.ListSpecifications`, `ClinVar.SearchCodingSpan`
   on the codon or exon for every `*_INF` rule you report, and the literature sweep. Then
   judge, and call `classify_variant`.
1. **Sensitivity.** Re-run the tally across each judgement input's plausible range, to find the class-determinative
   calls.
1. **Write** the SVCv4 classification (below), then the working document to the kickoff's outline, embedding the
   classification in "Evidence assessment". Run `python3 -m themis.document_linter <document path>` and fix every issue
   it names. Commit and push.
1. **Review** (below), fold in the findings, commit.
1. **Questions for the curator** (below). Commit, push, and end the turn.

**The literature sweep** suits a sub-agent, run alongside the other retrievals. Its brief opens with this skill's path,
`/workspace/skills/classifying-sequence-variants/SKILL.md`, states what you already hold (the ids from `Normalize`, the
PMIDs fetched, where your captures are), and lists the questions. Its report is a claim you check against your own
evidence. The sweep:

- `Literature.SearchLitVar` over every identifier you hold, `caid` included, then `ListLitVarEntities` and `SearchEuropePmc` for what it misses. Add the PMIDs from
  ClinVar's submissions and observations, and the `publications` of each PanelApp panel in `DescribeGene`'s
  `raw['panelapp']`.
- `MaybeIngestPapers` once over every id you mean to read. An `_UNKNOWN_PAPER` goes on the deposit-request list.
- `PollFullTexts` before finalising; a paper still `_PENDING` goes on the deposit list with its state.
- `GetMarkdown` each `_READY` paper and save it verbatim to `/workspace/scratch/captured/<doc_id>.md` before quoting it.
  Report a text that `total_chars` shows was cut as read only up to the cut.
- `Validate` every quote before it enters the document.
- `FetchPubmedArticles` for any PMID the store cannot serve; its `book_articles` are where GeneReviews chapters arrive.
- A paper the store does not hold goes on the deposit list with the question it would answer. Never substitute a
  web search result's text for it. Say which candidates you left unread and why; a truncation the census reports is
  a recall gap to report.

## The working document

The path is `/workspace/working_document.md` unless your thread's instruction names another. Follow the kickoff's
outline and add no sections. What the outline does not say:

- **Citations.** A passage you read: `:quote[<doc_id>, verbatim quote]`, copied character for character from your
  captured file and passed by `Validate`, on a span with no inline backticks or unbalanced brackets. A paper relied on
  without one passage: `:paper[<doc_id>]`. A paper served as `TEXT_PROVENANCE_SUPPLIED` gets "(supplied)" at its first
  citation. Database facts and CSpec text: prose naming the source and its `provenance.retrieved_at`. An abstract:
  prose naming its PMID. None of these take a directive.
- **The SVCv4 classification is a widget, and the record of every code.** Build it with `themis.svcv4.widget`: read
  its docstring first. `widget.Inputs` takes what `classify_variant` takes, and `widget.build` runs the classification
  itself, so every point, total and class in the record is the library's. `widget.admitted(ref, inputs)` lists the
  codes it needs an annotation for, and every one gets a status: scored, not applicable (the framework bars it) or no
  data (it applies and nothing determines it), with the reason for either of the last two. A scored code carries its
  evidence (each item a retrieval, a validated citation, a case-text quote or a web result), its rationale, how open
  you consider the call, and the nearest cell you ruled out. A code on a path names its decision-tree cell in
  `decision`; an independent code names the cells it was priced from, as `cell_id` and `count`, the same
  observations `observations.total` priced. Pass the other surviving values of an open input as `open_values`, and the
  sensitivity rows as `sensitivity`, each as the inputs with the varied ones replaced: the builder scores them. Write it
  with `widgets.update('assets/svcv4.binpb', lambda committed: widget.build(..., committed=committed))`, and put
  `::embed[assets/svcv4.binpb]` on a line of its own in "Evidence assessment". The document does not restate what the
  record carries: a code's cell, points, evidence and reasoning live in the record alone. Nor does a code's text state
  what the builder scores, such as an alternative's total or class or whether a sensitivity row changes the class: the
  record draws each beside the code it moves, and a sentence stating one goes stale when a revision re-scores it.
- **Revising the classification**: restate a code in `codes` only when its status, evidence or reasoning changed. A
  code you leave out keeps the annotation the committed record holds, and with it the curator's tick; the builder
  refuses to carry one the new tally moved (its points, its path, the releases behind it), so restate that code. The
  first write needs `routing=`; later writes carry it until the consequence, the paths taken or the gate level change,
  when the builder refuses and you restate it. Each curator's note clears when you change what it is on, so a note you
  answered without changing its code stays: act on a note only when the answers recorded under the open items do not
  already cover it. `update` prints the text of each note it clears, for example when a rebase meets a note written
  mid-turn: read it as a note found at the start of a turn. After any rebase, run `widgets.judgements` again.
- **Reflection (f)** quotes the code a ready helper should have written, judged by whether getting it wrong would be
  easy and silent, not by length. (b) is about the services; (f) is about the code around them.
- **Things the curator confirms one by one**, such as the assumptions your open items proceed on, go in a checklist
  rather than a markdown list: build a `checklist_pb2.Checklist` (`from themis.widgets.models import checklist_pb2`;
  the schema is `/usr/local/share/themis/widgets/checklist.proto`), write it with
  `widgets.write('assets/<name>.binpb', checklist)` (`from themis.agent import widgets`), and put
  `::embed[assets/<name>.binpb]` on a line of its own where the list belongs. A field the schema marks `guard`, such
  as `checked`, is the user's judgement on the rest of its item, its label and citation: you never set it, and a push
  that sets one is refused. To revise the list, pull, then `widgets.update('assets/<name>.binpb', build)`: `build`
  gets the committed checklist and returns yours with every tick left unset, and `update` keeps each item's tick by
  its `id` where you left the item's label and citation as they were. Rewording or re-citing an item the user ticked
  clears the tick, so the user has to review that item again; `update` reports each tick it clears, and a push that
  keeps a tick on a changed item, or clears one on an unchanged item, is refused. Removing an item, giving it a new
  id, or moving the asset to another path (which starts it as a new asset) also discards the user's review:
  restructure or reword a reviewed checklist only when the work calls for it, and never reuse an id for another item.
  A merge that takes in the user's edits is refused: `git pull` rebases, and when the asset conflicts, resolve it
  with `update` and `git rebase --continue`.

## Review before you finish

Delegate a review to the roster's `self` entry, with fresh context and a brief of about fifteen lines. It inherits none
of your reasoning, so paste the kickoff's clinical context (the evidence behind every supplied `CLN_*`/`LOC_*` cell) and
name your snippet directory. Base the brief on:

> Read `/workspace/skills/classifying-sequence-variants/SKILL.md` first. The clinical context the kickoff supplied,
> verbatim: `<paste it>`. Then read `<document path>`, the classification record it embeds
> (`display.show(widgets.read('assets/svcv4.binpb'))`, `from themis.agent import display, widgets`), which holds every
> code's cell, points, evidence and reasoning, and their cited evidence fresh: the captured papers under
> `/workspace/scratch/captured/`, and the service answers, by re-issuing the calls whose snippets are under
> `<snippet directory>` through `retry.call(..., cache_dir='/workspace/scratch/cache')`. Where an answer differs from
> what the document reports, check your request against the author's snippet before calling it a divergence; a request
> you could not reproduce is a reproduction gap. Do not redo the classification and do not write to the document. For
> each scored code in the record, check its cell against the evidence the record cites and the framework's rule for
> that cell;
> check each stated absence against what the services returned. Report every divergence with the clause that decides it
> (the response field, the passage, the framework rule), quoting both sides with their locations. A cell its evidence
> does not support, points that disagree with the library, an absence the evidence contradicts, a quote that does not
> validate: each is a finding. Do not rank or call anything minor; check the rows, not the total. Where you cannot tell,
> say what would settle it. Return findings as prose, at most one page.

The report is a claim: reconcile it against your own evidence. Fold in what holds; the document then states the
corrected reading, with no account of what the review changed. Where you overrule a finding, record
in the evidence section what was raised and why it did not move the cell.

## Questions for the curator

Nothing tells a run whether a curator is present, so never block on a question. Finish the work first: every fact you
can retrieve, every code you can settle, the full tally. Then record each call worth a curator's judgement under the
document's open items, with the assumption you took to proceed, and repeat the numbered list at the end of your turn.

Record a call only when a second answer is live (the evidence admits more than one value, or a disposition stands in for
real uncertainty) and it changes the class or points a reader would care about. The sensitivity rows answer the second.
Rank by outcome moved; past five, list the class-changing ones and say the rest are in the document. A code that is
not applicable, or has no data, is an answer, not a question. Do not record what you could have looked up, do not present a
settled call as open, and do not bundle judgements.

For each call give: a number; the question in one sentence, naming the code it feeds; each admissible option with the
points and class it lands on, both read from the record you last wrote, in the open items as in the message; your own read and the assumption the document proceeds
on (on an open input, your read does not settle it, and the class it favours is not the class the document reports);
and what would settle it. Then say how to reply, and that reasoning is worth more to you than a label.

When an answer arrives, in the conversation or as a note in the classification, do not guess at an ambiguous reply:
say what you read and ask again. Re-derive through the library rather than patching a total. Revise the document at its path, record under the open items what the answer
changed and what it did not, and commit and push. This run started from the pushed repository with `scratch/` gone, so
capture a paper again before quoting it anew. Keep the curator's words as the rationale, and say what is still open.
