# Design: evidence interfaces — the curated-database half of `evidence`

> **SVCv4 is a draft standard, and this is evaluation software.** The framework was released as a **pilot in July
> 2026**; its point values, thresholds, code names and data model **may change before publication** (target: *Genetics
> in Medicine*, ~Jan 2027), and Supplementary Material 17 (non-coding variants) is unreleased. These interfaces exist to
> **evaluate whether the framework can be run autonomously**. They are **not a validated implementation, not for
> clinical or diagnostic use, and their output is not a clinical variant classification** — the values they return are
> public-database retrievals, and any classification built on them must be re-verified against the final published
> standard and the [ClinGen Pilot Calculator](https://calculator.clinicalgenome.org/v4/pilot/ui/classification).
>
> **The design is expected to move.** How the surface is split into services and interfaces, the contracts themselves,
> and the implementations behind them will all change as the framework settles and the evaluation feeds back. What this
> doc records is a concrete base layer to iterate on, not a final shape.

**Related:** [`svcv4-interpretations.md`](svcv4-interpretations.md) (the clinical readings of the framework these
interfaces apply — which frequency figure, which ClinVar terms, which exon tier), [`services.md`](services.md) (the
service pattern, and how several interfaces share one deployment),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the sibling literature interface — the *other* evidence
source), [`proto.md`](proto.md) (schema and serialisation posture), [`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)
(how an rpc becomes agent-callable), [`security.md`](security.md) (the default-on chokepoint rule the tool surface
follows), [`sandbox-worker.md`](sandbox-worker.md) (the bubblewrap guest, its empty network namespace, and the gRPC
hatch), [`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md) (the sandbox architecture around that
guest), [`analysis-scenarios.md`](analysis-scenarios.md) (the reference set the eval scores against),
[`../PRODUCT.md`](../PRODUCT.md) (the product frame: §4 tool layer, §6 facts + judgement, §12 the Spike); terms in
[`../../GLOSSARY.md`](../../GLOSSARY.md).

## Overview

Nine gRPC interfaces on the shared `evidence` deployment — one per curated public source — that, given a variant, return
what that source holds about it: the non-literature evidence a single-variant ACMG **SVCv4** classification needs. They
are the structured-data counterpart to the `literature` interface beside them in the same image, which answers from
papers.

The decision recorded here is a division of labour into four roles, and where each one lands. **Retrieval** that needs
the open internet is a service-side rpc, because the agent's sandbox has no egress. The deterministic SVCv4
**arithmetic** is a library shipped into the sandbox, `themis.svcv4`, because it needs no egress and the tally has to be
reproducible. **Remapping** — the error-prone coordinate and vocabulary wrangling between the two — is service code, so
the offline library never sees an upstream's serialisation quirks. Every call the framework hands to "an experienced
analyst" stays with the model. SVCv4 dictates *what* evidence a classification needs; this doc decides *how gathering
each line becomes runnable*.

So the design body is organised the way the framework asks its questions: one section per evidence-collection goal,
naming the rpcs that serve it, what its upstream gives and what has to be remapped, a worked example, and the caveats
that come with the source. What each of those readings of the framework *means* clinically — which frequency figure
`POP_FRQ` compares against, which ClinVar terms are a pathogenic assertion, what licenses an exon-relevance tier — is a
clinical judgement rather than a service-design one, and lives in
[`svcv4-interpretations.md`](svcv4-interpretations.md).

The contracts are the nine `.proto` files. Each states its own identifier forms, its absence semantics and its
field-to-SVCv4-code mapping, and that file is the spec — this doc explains it and does not repeat it.

## Background

This section fixes the vocabulary, what the framework demands of a tool set, the boundary that decides where each piece
can run, and the scale that makes every upstream a live wrap rather than a mirror.

### Vocabulary

SVCv4 brings its own terms; each is used throughout and defined once here.

| Term                            | Meaning                                                                                                                                                                                                                                                                                                                                                    |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SVCv4**                       | ACMG/AMP/CAP/ClinGen Sequence Variant Classification v4 — the points-based successor to ACMG 2015 (`ACMG (V4)` in the glossary).                                                                                                                                                                                                                           |
| **SM*n***                       | SVCv4 Supplementary Material *n*: the per-topic normative supplements the framework is specified in. The ones leaned on here: SM3 frequency and the DAFT; SM4 clinical observations; SM5 segregation; SM6 in-silico prediction; SM18 mechanism and exon relevance; SM19 informative variants; SM20 functional assays; SM21 the MDE and multiple disorders. |
| **VBC**                         | The variant being classified.                                                                                                                                                                                                                                                                                                                              |
| **MDE**                         | Mono-disease entity: gene × phenotype × inheritance × mechanism. The unit SVCv4 classifies — not the gene.                                                                                                                                                                                                                                                 |
| **Evidence code**               | One scored line of evidence, named by family: `POP_FRQ` / `POP_HMZ` (frequency), `*_PRD` (in-silico prediction), `*_INF` (other classified variants at the same codon or exon), `*_FXN` (functional assays), `CLN_*` (clinical), `LOC_*` (phenotype and segregation).                                                                                      |
| **P/LP, B/LB**                  | Pathogenic or likely pathogenic; benign or likely benign — the classification pairs ClinVar records are grouped by.                                                                                                                                                                                                                                        |
| **NMD / NSD**                   | Nonsense-mediated and non-stop decay: the pathways that decide whether a premature stop codon, or a missing one, leaves any protein. A **PTC** is a premature termination codon.                                                                                                                                                                           |
| **MAVE**                        | Multiplexed assay of variant effect — a deep mutational scan, the assay class MaveDB deposits.                                                                                                                                                                                                                                                             |
| **The matrix**                  | A molecular-mechanism × exon-relevance multiplier (×1.0 / 0.5 / 0.25 / 0) applied to *positive* predictive points.                                                                                                                                                                                                                                         |
| **All / Most / Few**            | SM18's three exon-relevance tiers, the matrix's other axis: how many of the gene's transcripts carry the assessed exon, and how abundant those are. SM18's *admission filter* is the clause naming which transcripts count toward a tier at all, applied before the tier is read.                                                                          |
| **GDV gate**                    | The gene-disease-validity gate: the curated validity level of the MDE caps the final class, and also gates the mechanism multiplier.                                                                                                                                                                                                                       |
| **DAFT**                        | Disease allele frequency threshold: the frequency above which a variant is too common for the disease, and the comparator `POP_FRQ` is scored against.                                                                                                                                                                                                     |
| **OddsPath**                    | The likelihood ratio a calibrated functional assay yields, which `*_FXN` reads.                                                                                                                                                                                                                                                                            |
| **VCEP**                        | A ClinGen Variant Curation Expert Panel, which may publish its own gene-specific criteria specification.                                                                                                                                                                                                                                                   |
| **Gold stars**                  | ClinVar's 0–4 review-status scale for how a record's classification was reviewed.                                                                                                                                                                                                                                                                          |
| **MANE Select / Plus Clinical** | The RefSeq/Ensembl-agreed representative transcript of a gene, and the additional clinically-used ones.                                                                                                                                                                                                                                                    |
| **pext**                        | gnomAD's proportion-expressed-across-transcripts: how much of a gene's expression includes a given base.                                                                                                                                                                                                                                                   |
| **Splice colour**               | The trichotomy a splice prediction routes on — likely / indeterminate / unlikely disruptive — which each workflow prices per colour.                                                                                                                                                                                                                       |
| **SVCv4 corpus**                | Our private checkout of the pilot's documentation set: the supplements as received, the practice variants, and our transcriptions of both. The reference's citations and the reference set point into it. Distinct from the literature corpus the `literature` interface serves.                                                                           |
| **Reference set**               | The graded evaluation cases the classifier loop scores against ([`analysis-scenarios.md`](analysis-scenarios.md)), drawn from the SVCv4 corpus.                                                                                                                                                                                                            |

### SVCv4's shape drives the tool set

Points-based ACMG: per-code points sum to a class. Two structures make the sum more than addition. Positive predictive
points are scaled by the matrix, so a mechanism call and an exon call multiply into most positive paths. The GDV gate
then caps the resulting class, and gates the mechanism multiplier itself. Each variant type has its own workflow, but
the workflows are assembled from the same recurring evidence modules — frequency, in-silico prediction, other classified
variants, functional assays, clinical observations, phenotype and segregation — so a module is built once and routed to
by molecular consequence. These interfaces realise the public, non-literature, non-case subset of the modules' inputs.

### The sandbox boundary fixes where retrieval and arithmetic can live

Untrusted agent code runs bubblewrapped with an empty network namespace, fail-closed, and reaches trusted services only
through a method-allowlisted gRPC hatch ([`sandbox-worker.md`](sandbox-worker.md) §"The hatch is the capability
boundary"). Trusted services egress normally. Two consequences fix the architecture:

- retrieval that needs the open internet **must** be service-side, because the agent cannot egress at all; and
- the deterministic arithmetic **must** be shippable into the image, because it has to run offline inside the sandbox.

Neither consequence is a requirement to mirror anything.

### Scale: one variant at a time

The Spike is curator-in-the-loop, per-variant evaluation — not VCF-wide, not batch (PRODUCT §12). Getting one
classification right takes a couple of dozen public REST calls. Every upstream is therefore a thin **live** wrap of a
public endpoint, stamped with its retrieval time. Local mirrors are the at-scale form of the same design, staged behind
explicit triggers ([Alternatives considered](#alternatives-considered)), not a rejected option.

## Non-goals

What these interfaces will never do, as opposed to what is merely not built yet.

- **Literature-derived evidence.** Functional assays reported only in papers, clinical narrative, de-novo and
  segregation from case reports. The `literature` interface owns find → fetch → distil over papers; no interface here
  reads a paper.
- **Case and patient-data evidence, as a *service* concern.** The `CLN_*` and `LOC_*` codes come from patient and
  pedigree data no public database holds, so no rpc retrieves them. The library still scores them from the analyst's
  categorical reading (see
  [the library takes categorical judgements](#the-library-takes-categorical-judgements-and-returns-derivations)). Beyond
  the public Spike, and gated tighter.
- **The SVCv4 scoring and combining arithmetic.** A library, not an rpc — see
  [`themis.svcv4`](#the-in-sandbox-library-themissvcv4).
- **CNV / dosage classification.** A separate ACMG framework; a multi-gene event routes out.
- **Evidence SVCv4 does not ask for.** No 3D-structure or PDB retrieval: the framework never cites structure, and
  critical-residue evidence is UniProt domains plus the calibrated predictor. No general Grantham signal: the distance
  appears only as the missense same-codon comparator inside the informative-variant rules. And none of the criteria v4
  dropped — PP2/BP1 (subsumed into the calibrated predictor), PP5/BP6 (assertion-based, discontinued), standalone PM2
  rarity — re-enters through an interface.
- **Bulk annotation.** No VCF-wide runs; the standard pipeline produces baseline annotations upstream (PRODUCT §6).
- **Hosting a prediction model on this deployment.** Predictors are live-queried: a self-hosted splice model or
  `ensembl-vep` brings reference data and real compute, so it would be its own deployment
  ([below](#one-interface-per-source-all-on-the-one-evidence-deployment)).

## Design

The decision first — who does what and where each piece runs — then the deployment shape and the rpc surface, then one
section per evidence-collection goal, and finally what cuts across all of them: the contract rules written against
upstream failure modes, provenance, the library, how it is evaluated, and the proto layout.

### Four roles: retrieve, remap, compute, judge

**A frontier model is the analyst.** It ingests the evidence and writes the report; everywhere SVCv4 hands a decision to
"an experienced analyst", that seat is the model, curator-steered (PRODUCT §4, §6). The interfaces and the library feed
and support that judgement rather than standing in for it. Four roles follow:

- **Retrieve** — an interface returning the upstream payload largely as-is, plus provenance, plus proto documentation
  naming which SVCv4 codes each field feeds. The model parses raw JSON reliably, so nothing is re-modelled needlessly:
  the value added is the egress and the documentation.
- **Remap** — an interface returning typed values the service derived: cross-build and transcript projection, reducing
  each splice predictor's per-transcript deltas onto a shared orientation, resolving ClinVar's review-status phrase to
  gold stars, keying MaveDB on the variant's ClinGen allele. This is the wrangling the model should not hand-do.
- **Compute** — `themis.svcv4`. Deterministic and offline, not because the model cannot add, but because the tally must
  be reproducible and auditable and the score is *shown, not load-bearing* (PRODUCT §6).
- **Judge** — the model. Every analyst call SVCv4 defines, each made with shown reasoning and an explicit uncertainty
  the curator can threshold on, and each curator-overridable:
  - the MDE itself;
  - ambiguous consequence routing;
  - the mechanism level;
  - the All / Most / Few exon-relevance call;
  - which classified variants carry distinct, non-circular evidence;
  - whether a functional assay measures the disease-relevant function and agrees with the mechanism;
  - the splice product's consequence;
  - DAFT parameters where no curated value exists;
  - critical-residue determination and multiple-disorder aggregation;
  - and the final holistic verdict, reasoned over the claims rather than the rolled-up score.

Most rpcs mix the first two roles, and which typed field is a copy and which is a derivation is a property of the field
rather than of the rpc — an rpc chaining three upstreams can still return nothing but copies. Each proto documents what
its own fields carry.

Two decisions run the other way, for the same reason: a choice made after seeing the answer is multiple testing rather
than judgement. **Predictor selection is frozen policy**, exercised once in advance and per gene, and so are two
frequency choices. Both are readings of the standard rather than service decisions, and both are in
[`svcv4-interpretations.md`](svcv4-interpretations.md). Where a judgement input does stay open, the framework's own
disposition applies, and what remains open is reported as no class.

### One interface per source, all on the one `evidence` deployment

Nine gRPC services, one per curated source, attached to the one `evidence` server beside `literature` — a deployment and
a gRPC service are not one-to-one ([`services.md`](services.md#one-deployment-several-interfaces)). **The source is the
unit** because it is what a contract is written against: an interface's proto states the identifier forms, absence
semantics and rate limits a caller has to know to read the answer, and it evolves when its upstreams do. An interface
may reach a helper upstream — a crosswalk registry, a projection engine — behind that contract without changing whose
vocabulary it speaks; the four whose answer no single vendor owns (`variant`, `transcript`, `splice`, `gene_disease`)
are named for the question instead ([Alternatives](#alternatives-considered)). That contract is the whole of what an
interface owns — the deployment, the image, the session resolver and the shared upstream clients are one for all of
them.

Sharing a deployment has two consequences here, both of them the general ones ([`services.md`](services.md)) landing on
this surface.

- **The sandbox agent is the one caller that does not reach every rpc.** IAM is granted per deployment, so any invoker —
  the web application's backend included — can call all twelve rpcs. The agent is the exception, and by a different
  mechanism: the hatch's method allowlist ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)) exposes rpcs to it one
  at a time.
- **`gene_disease` is a startup dependency of every other interface.** Failure isolates per rpc, but startup is shared:
  an interface that cannot build its backend takes the process down. `GeneDisease.DescribeGene` loads four reference
  tables before it serves, so a bucket it cannot read keeps the whole image down — which is the intended behaviour, not
  a defect in the probe. A revision that came up serving an unreadable reference store would answer "absent" for every
  gene-disease question put to it, and "absent" is scored evidence.

A source earns its own **deployment** only on a real driver, never pre-emptively. Self-hosting a splice predictor, a
dbNSFP query service or `ensembl-vep` would each bring a model or reference data plus real compute: a different image
and a different scaling profile.

### The rpc surface

Twelve rpcs across the nine interfaces. This table is the index; each linked proto is the contract.

| RPC                              | Upstream(s)                                             | Feeds                                                                 |
| -------------------------------- | ------------------------------------------------------- | --------------------------------------------------------------------- |
| `Variant.Normalize`              | ClinGen Allele Registry + VariantValidator + VEP        | routing consequence; the canonical join key                           |
| `Vep.Annotate`                   | Ensembl VEP REST                                        | consequence; `MIS_PRD`; `SPL_PRD`; a ClinVar/gnomAD snapshot          |
| `Gnomad.DescribeVariant`         | gnomAD GraphQL                                          | `POP_FRQ` / `POP_HMZ` inputs; exon-relevance signals                  |
| `ClinVar.DescribeVariant`        | NCBI ClinVar                                            | the `*_INF` pathogenic arm; P/LP density; expert-panel consensus      |
| `ClinVar.SearchCodingSpan`       | VariantValidator + NCBI ClinVar                         | the `*_INF` candidate set at a codon or exon, every classification    |
| `GeneDisease.DescribeGene`       | ClinGen validity + dosage + GenCC + PanelApp AU + MONDO | the GDV gate and mechanism signals, per MDE                           |
| `Transcript.GetStructure`        | VariantValidator                                        | exon membership; splice-site distances; per-exon skip frame           |
| `Transcript.AssessExonRelevance` | GTEx + gnomAD + ClinVar + VariantValidator              | the exon axis of the matrix                                           |
| `Splice.PredictDeltas`           | Broad SpliceAI + Pangolin                               | the `SPL_PRD` trichotomy                                              |
| `Splice.PredictSkipOutcome`      | VariantValidator + NCBI Nucleotide                      | the aberrant transcript: frame, PTC and NMD — the null vs coding fork |
| `MaveDb.DescribeVariant`         | MaveDB (keyed via ClinGen Allele Registry)              | `*_FXN` from public MAVE assays                                       |
| `Cspec.ListSpecifications`       | ClinGen CSpec Registry                                  | a VCEP's own criterion wording and thresholds                         |

Protos: [`variant`](../../schema/proto/themis/rpc/variant.proto), [`vep`](../../schema/proto/themis/rpc/vep.proto),
[`gnomad`](../../schema/proto/themis/rpc/gnomad.proto), [`clinvar`](../../schema/proto/themis/rpc/clinvar.proto),
[`gene_disease`](../../schema/proto/themis/rpc/gene_disease.proto),
[`transcript`](../../schema/proto/themis/rpc/transcript.proto), [`splice`](../../schema/proto/themis/rpc/splice.proto),
[`mavedb`](../../schema/proto/themis/rpc/mavedb.proto), [`cspec`](../../schema/proto/themis/rpc/cspec.proto). Auth is
per the service pattern: the session token arrives as request metadata and never as a message field
([`services.md`](services.md)) — the default-on chokepoint rule ([`security.md`](security.md)) applied to this surface.

Three properties of the surface are not visible in the table:

- **VEP is a consolidator.** One call returns molecular consequence, per-transcript HGVS, MANE flags, six of the seven
  SVCv4-calibrated missense predictors, splice deltas and an inline ClinVar/gnomAD snapshot, so several evidence modules
  collapse onto one rpc and no predictor is self-hosted. The seventh calibrated predictor is one the public endpoint
  does not serve at all, which matters only if a gene-specific calibration ever commits to it.
- **`Variant.Normalize` is the spine, and it is RefSeq-only**, because VariantValidator serves RefSeq accessions and
  refuses an Ensembl one. That is also why the exon-table and splice-outcome rpcs take RefSeq coding accessions.
  Accepting the other identifier forms a caller may hold (VCF locus, gnomAD id, rsID, CAID, protein HGVS) is deferred
  rather than refused on principle: each needs a different Allele Registry endpoint, a VCF locus additionally needs a
  chromosome-accession table, and protein HGVS has no Allele Registry route at all. Closing that gap belongs in one
  auditable normalisation step with its own provenance, not in a per-rpc sniffer.
- **Which identifier form each rpc accepts tracks what its upstream is keyed on.** The frequency and splice-delta rpcs
  take a positional id; the normalisation, ClinVar and VEP rpcs take HGVS. Every rejected form has a stated reason in
  its contract, because a rejection a caller cannot explain reads as a defect.

### Goal: place the variant, and route it to a decision tree

Before any evidence is scored, the variant has to be one thing. SVCv4 picks a decision tree by molecular consequence,
and every source below is keyed differently: a positional id at gnomAD and the splice hosts, transcript HGVS at ClinVar
and VEP, an HGNC id at `gene_disease`, a ClinGen allele id at MaveDB. One normalisation step, with its own provenance,
is what keeps twelve retrievals talking about the same allele.

**RPCs.** `Variant.Normalize` is the spine ([the rpc surface](#the-rpc-surface)); `Vep.Annotate` supplies the
consequence it routes on; `Transcript.GetStructure` places the allele inside its transcript — exon membership,
splice-site distances, and each exon's skip frame.

**What the upstreams give, and what has to be remapped.** The ClinGen Allele Registry canonicalises the allele and
carries its crosswalks; VariantValidator validates the HGVS, projects it onto MANE Select and yields VCF loci on both
builds; VEP states the molecular consequence. None of the three yields the join key in the form a caller needs, so the
service derives it:

- the canonical allele id is split out of a registry URL;
- the positional ids are assembled from VariantValidator's VCF parts;
- the transcript projections from both sources are deduped on the unversioned accession, and stamped per element with
  who stated them;
- and the consequence term is re-encoded onto the routing enum the workflows select on.

The registry's **ClinVar crosswalk is surfaced typed** here rather than left in the payload — the variation id, its RCV
accessions, the ClinVar allele id and the preferred name — because it is the key `ClinVar.DescribeVariant` fetches a
record by, and its emptiness on a resolved allele is the novel-allele finding
([below](#goal-what-else-has-been-classified-at-this-codon-this-exon-and-across-the-gene)). A field a downstream rpc
takes as input, and a finding a run reports, are both load-bearing enough to type.

**Worked example.** "Take the MANE Select transcript" is not a safe instruction against the registry's answer: it lists
a MANE pair once per accession namespace and flags both, with the Ensembl form first. A caller selecting by flag and
position gets an `ENST`, which the exon-table, exon-relevance and splice-outcome rpcs all reject — so the response
carries the namespace per projection and selection is on both.

**Caveats.** The projection set is *this allele's*, not the gene's transcript set: a transcript the allele does not
touch is absent, so it is the wrong denominator for an exon elsewhere in the gene
([exon relevance](#goal-how-relevant-is-the-exon-the-variant-lands-in) owns that one). Where the variant arrives already
annotated by the standard pipeline (PRODUCT §6), a caller can skip straight to the fact rpcs; normalisation is the route
in, not a toll.

### Goal: is the variant too common for the disease?

`POP_FRQ` and `POP_HMZ` compare the variant's population frequency against a disease-specific threshold, so this goal
needs a frequency, the quality verdict behind it, and homozygote counts — and it needs an absence to be *usable*, since
absence from gnomAD is the rarity input rather than a failed lookup.

**RPCs.** `Gnomad.DescribeVariant`, taking the positional id `Variant.Normalize` produced.

**What the upstream gives, and what has to be remapped.** gnomAD's GraphQL API serves per-dataset and joint frequency
blocks, the filtering allele frequencies maximised over genetic-ancestry groups, homozygote counts, the variant-QC
verdict per dataset, gnomAD's own caveat flags, and coverage and constraint context. Almost none of it is remapped: the
library reads the payload, at the paths `frequency.faf_from_gnomad` and `homozygotes_from_gnomad` name — the constraint
and coverage context is `Transcript.AssessExonRelevance`'s, and nothing in the library reads it. What the service owes
instead is three guards — the id is held to the stricter of the spellings its consumers accept, the dataset is checked
against the releases this rpc serves, and every release the answer rests on is stamped. Which figure `POP_FRQ` is
actually scored against, and what disqualifies one, is a reading of SM3 and of ClinGen's gnomAD guidance
([`svcv4-interpretations.md`](svcv4-interpretations.md#frequency-which-figure-and-what-disqualifies-one)); the rpc
returns the joint figure *and* each dataset's QC verdict because that reading needs both.

**Worked example.** gnomAD answers an id it cannot parse with a **200 carrying an absence**, not an error. Unguarded,
`17-4932566-A-T` mistyped is scored as "absent from gnomAD" — benign evidence manufactured from a typo, and never
retried, because absence is a settled answer
([where absence is scored](#where-absence-is-scored-a-shape-precondition-is-required-not-optional)).

**Caveats.** A `NOT_FOUND` here carries no coverage distinction: "not observed at a well-covered position" and "position
not covered" arrive the same way, and rarity evidence rests on the difference ([Open questions](#open-questions)). The
rate limit is per IP, so concurrent sessions behind one egress address share it.

**Moving off the upstream.** A local gnomAD mirror is the at-scale form of this rpc, not a different design; the
triggers that flip it are in [Alternatives](#alternatives-considered).

### Goal: what does a calibrated predictor say about the substitution?

The `*_PRD` codes read one calibrated predictor's score for the substitution, binned against that predictor's published
thresholds. The engineering problem is not obtaining a score — it is obtaining *exactly one*, from the predictor a
policy named in advance, without the others becoming a menu.

**RPCs.** `Vep.Annotate`, the same call that supplies the routing consequence
([above](#goal-place-the-variant-and-route-it-to-a-decision-tree)) and SpliceAI's deltas.

**What the upstream gives, and what has to be remapped.** One VEP call carries per-transcript HGVS, exon numbers, MANE
flags, the calibrated missense predictors, splice deltas and a colocated ClinVar/gnomAD snapshot — but VEP emits none of
the per-transcript fields unless asked, so the rpc asks on every call. The predictors arrive by two different wire
forms: some are first-class VEP fields, the rest reach VEP through its dbNSFP plugin, whose values arrive as
per-transcript dotted strings and are resolved service-side to one value per transcript (a string naming several is an
error, not a pick). The rpc also pins GRCh38, because MANE and AlphaMissense are GRCh38-only, so a reference that names
no assembly is refused rather than read at the pinned build.

Which predictor a gene's score comes from is frozen per-gene policy, versioned data shipped in `themis.svcv4` and read
before the call
([`svcv4-interpretations.md`](svcv4-interpretations.md#in-silico-prediction-one-predictor-chosen-in-advance)).
`Vep.Annotate` will serve any predictor on its allowlist, so **the single-predictor guarantee is the policy plus the
library's enforcement, not the rpc**: the library's score-to-bin step scores the policy's predictor and no other, every
selection reports the entry that decided it, and a missing or malformed gene id raises rather than defaulting. The other
predictors' scores ride in the payload as what a policy *revision* would be argued from.

**Worked example.** Ensembl ignores a flag it does not recognise: asking for `BayesDel` by a misspelled name returns a
200 with the score absent, which no caller can distinguish from the variant having no score. So the predictor names are
a closed set and one outside it is refused — the alternative is a run that silently scores nothing.

**Caveats and moving off the upstream.** The public Ensembl REST endpoint is frozen — its final release has shipped —
which pins the Ensembl release and therefore the transcript set, the MANE version and the dbNSFP branch. Nothing breaks;
the answers drift from current annotation silently, which is why the release stamps on the wire matter *more* under a
freeze rather than less. The end state for this upstream is a self-hosted `ensembl-vep`, for reasons the announced
successor makes concrete ([Alternatives](#alternatives-considered)).

#### Predictor data licensing binds on one trigger

Ensembl's public REST serves the **commercial branch of dbNSFP** and passes the obligation straight through: users are
solely responsible for establishing the nature of and complying with any restrictions. dbNSFP is free for academic and
non-profit use and requires an annual licence for any use within a for-profit organisation, or incorporation into
products that are sold or monetised. BayesDel itself states no licence, only a citation request.

The assessment: use here is academic / non-profit research, which the grant covers, and nothing is redistributed — the
service queries an API per variant and vendors no dbNSFP data. **The trigger that invalidates it: if this becomes a sold
or monetised product, dbNSFP, REVEL and ClinPred all need revisiting.** Ensembl documents the latter two as
non-commercial-use only, and both come back from the same VEP call; REVEL is on the predictor allowlist, so what keeps a
non-commercial score out of a response today is the frozen policy, not the contract.

The known alternative if that trigger fires or the terms change: BayesDel's author distributes the no-allele-frequency
build directly, sidestepping the dbNSFP *compilation* licence entirely. That is two orders of magnitude larger than the
gene-disease tables but the same shape — a periodically-refreshed reference the service reads, not a per-variant mirror.

### Goal: does the variant disrupt splicing, and what does the product become?

Every variant type checks a splice effect, so this goal splits in two: *whether* a splice site is lost, which two
predictors answer with per-position deltas, and *what the transcript becomes* if one is — the fork between the null path
(a PTC, NMD) and the coding path (an in-frame deletion), which no predictor answers at all.

**RPCs.** `Splice.PredictDeltas` for the deltas, `Splice.PredictSkipOutcome` for the product. `Vep.Annotate`'s inline
SpliceAI deltas are a snapshot beside them, and `Transcript.GetStructure` supplies the exon table the product is built
from.

**What the upstreams give, and what has to be remapped.** The Broad's SpliceAI and Pangolin hosts each return
per-transcript, per-position deltas — and they do not share a sign convention: SpliceAI's four deltas are magnitudes,
Pangolin signs a loss negative. Taken raw, one predictor's maximum is not comparable with the other's and a predicted
loss is invisible beside a co-reported gain. So each predictor is reduced to one gain and one loss on a shared
orientation, maxed over the scored transcripts as well as over its own delta pair, and the pair is reported per
predictor so the concordance check compares gain against gain. The library bins the trichotomy; the rpc does not.

`Splice.PredictSkipOutcome` composes rather than retrieves: it excises the named exon's span from the RefSeq transcript
sequence and translates the result, so the PTC is *read off the sequence* rather than assumed from the frame shift. It
returns the frame shift, which product class the aberrant transcript falls in, where the PTC lands, and the 50-nt NMD
determination over the aberrant transcript's own exon structure. Its scope is stated so it is not over-read: it does not
predict whether the defect occurs, and a retained intron is not part of the transcript it splices.

**Worked example.** A large indel spells a long positional id, and the Broad services carry it in a GET request line —
whose hosts answer anything past ~8 kB with a bare 400. So length is refused *separately* from shape, with a message
that says which: a 1 kb deletion's 1019-character id is well-formed and gets scored, where a format complaint about an
id in exactly the right format would have closed splice prediction for every large indel.

**Caveats.** Both hosts answer every variant-level problem inside a 200 carrying an error string, so their 4xx is a
malformed request line rather than a verdict on the caller's variant — which is why they are exempt from the 4xx mapping
rule and why the id carries a shape precondition instead
([the failure-mode rules](#the-contracts-are-written-against-the-upstreams-failure-modes)). Their rate limits are per
IP, like gnomAD's.

**Moving off the upstream.** Self-hosting a splice model brings the model plus real compute, so it is a separate
deployment rather than an interface change — a [non-goal](#non-goals) until something forces it.

### Goal: what else has been classified at this codon, this exon and across the gene?

The `*_INF` codes score *other* classified variants at the same codon or exon; SM18's exon-relevance signal reads the
gene's pathogenic-variant density; and SM3's last DAFT method reads the gene's known pathogenic variants. ClinVar
answers all three, and a single query cannot.

**RPCs.** `ClinVar.DescribeVariant` returns the queried allele's own record plus the **gene's pathogenic pool**;
`ClinVar.SearchCodingSpan` returns **every** classification in a coding span. `Transcript.AssessExonRelevance` counts a
pool of its own at a fixed floor ([below](#goal-how-relevant-is-the-exon-the-variant-lands-in)).

**Why two rpcs.** The gene pool is pathogenic *by its search term*, which makes it the right shape for the `*_INF`
pathogenic arm, the P/LP density and the DAFT — and structurally unable to answer the other half of the `*_INF` rules,
every one of which also scores a benign, likely-benign or VUS informative variant. Reading the second off the first is
silently wrong wherever such a variant sits in the codon or exon: it went wrong on **four of seven evaluation cases**,
one of which reported a code as not-determined having checked only the half that could not resolve it. And **the span,
not the gene, is the unit** of the second rpc, because a codon or an exon is what the rules key on. A gene-wide
all-classification pool would answer a question no rule asks. On the genes where the benign and VUS arms matter it is
unfetchable anyway, so a bound would cut it to a prefix that settles no position.

**What the upstream gives, and what has to be remapped.** NCBI's E-utilities serve a text search (`esearch`), a bulk
summary (`esummary`) and a full record fetch (`efetch`). Four remaps sit on top:

- the review-status *phrase* is resolved to its gold-star count;
- each record's HGVS title is parsed into coding coordinates, so placing a pool against an exon table is arithmetic
  rather than a regex over display strings;
- the pool's search term is built as an Entrez *property* term;
- and the census bounding the pool is reported as typed state rather than left implicit.

The search term is where this rpc is most easily wrong. Written as an Entrez *field*, a pathogenic-significance query is
not refused — Entrez has no such field, silently reinterprets it, and matched 1452 records on one gene, of which a
sampled page was 60% *Uncertain significance*, where the correct property term matched 465. So the term is a property
term, it is returned verbatim with the census, and every record's own classification is re-checked term by term after
the search. Which terms count as a pathogenic assertion — and why the DAFT reads the same pool through a narrower gate —
is a reading of the vocabulary
([`svcv4-interpretations.md`](svcv4-interpretations.md#which-clinvar-classifications-count-as-a-pathogenic-assertion)).

**A bounded pool is typed, not caveated.** A well-studied gene carries thousands of P/LP records, so the pool a call can
carry is a prefix and its size is a lower bound. The census is therefore typed state on the response — the search term
issued verbatim, the total it matched, how much of it was fetched, and whether what came back is a prefix — rather than
a note in prose. The term is there because it is what says what the pool is a pool *of*: the membership gate is a
six-token disjunction and the star floor rides in the same term, so nothing else distinguishes a complete census from a
plausible-looking one. Truncation is there because a caller has to branch on it. The summaries themselves are not echoed
back — an `esummary` runs ~2.3 kB per record — so every field the pool is read for is typed on the record message
instead.

**The records the coordinates cannot place.** Copy-number and uncertain-boundary titles carry no coding span, and
neither does **short-form repeat notation** (`c.571ATG[1]`): the extent of the change is not derivable from that string
without the reference sequence, so it is a declined form rather than a guessed span. Every such record is *named* in the
response's unparsed list rather than dropped, because dropping one silently understates the exon it belongs to, and
whether it belongs there is not a question the coordinates can settle.

**Where the interface fails rather than answering.** An empty span census and a symbol ClinVar does not index are the
same empty answer, and one of them is a *scored* negative — so where the annotation sources disagree about the gene's
symbol, the rpc fails. That disagreement is this interface's own inconsistency to reconcile, not a finding a caller
could score.

**The queried allele's own record is asked for by identity, not by string.** ClinVar indexes an allele only under the
renderings its submitters and its curated expression list happen to carry, and those need not include the transcript
version a caller holds: `HOXA13` `NM_000522.5:c.396_398del` is a one-star record two laboratories classified, and a
search for that exact string matches nothing — ClinVar indexes the allele under the `.4` spelling and under a
repeat-notation title. No string bridges version × notation × shift, and a miss read as "novel allele" is a scored claim
about an allele two laboratories have classified.

So the lookup is keyed on the allele's identity. `Variant.Normalize` surfaces the ClinGen Allele Registry's ClinVar
crosswalk as a typed field — the variation id, its RCV accessions, the ClinVar allele id and the preferred name — and
`ClinVar.DescribeVariant` takes the VCV accession from it and fetches that record directly. Two things follow. **An
empty crosswalk on a resolved allele is the novel-allele finding**: the registry knows the allele and crosswalks it to
no ClinVar variation, which is the only evidence that supports reporting one as novel. And **a VCV accession the
registry named that ClinVar will not serve is a loud inconsistency** — two upstreams disagreeing about whether a record
exists — so it fails the rpc rather than arriving as an absence a caller could score.

> **The novelty finding is bounded by the crosswalk's release, and that is accepted.** The registry's crosswalk trails
> ClinVar's own releases, so a variant classified since the last crosswalk ingest reads as novel. Measured on a
> 30-record `MYH7` P/LP sample, about one of 26 precise alleles sat inside that window: `VCV004884318` (`MYH7`
> `c.1543A>G`, single submitter, evaluated 2026-08-04) had a registered allele carrying no ClinVar link two weeks later.
> A novel-allele finding therefore means "no ClinVar variation as of the crosswalk release named in the provenance", and
> the window closes under the dump-backed index below, where the crosswalk is ours and refreshed on our own cadence.

Where the record does come back, its review-status *phrase* is returned beside the star count, because an expert-panel
record is adopted on which panel reviewed it rather than on a count
([`svcv4-interpretations.md`](svcv4-interpretations.md#which-clinvar-classifications-count-as-a-pathogenic-assertion)).

**Moving off the upstream: a dump-backed index is where this goes.** One root cause is behind most of the machinery
above — a bulk dataset is being queried through a rate-limited text-search API. It is what makes the pool truncatable,
what makes the search term a correctness risk, what leaves the pool without per-submission detail (a bulk `esummary`
carries none), what makes a repeat-notation title unplaceable, and what puts the whole surface behind an E-utilities
rate limit. ClinVar publishes a **weekly VCV release**. Ingested and keyed on canonical SPDI, it dissolves all of those
at once: the pool becomes a query over a local index, the membership gate a predicate rather than a search string, and
the submission detail available for every record rather than only the queried one. A repeat-notation title becomes
placeable too, since the reference sequence is in hand at ingest time. It also subsumes the crosswalk's freshness
problem, because the mapping from allele to variation becomes ours, refreshed on our cadence.

The cost is freshness — a local index is as old as its last ingest, which Provenance's release list states — and the
ingest itself. Living with the query API is what is implemented; the triggers that pull the dump forward: the per-IP
rate limits binding at multi-session scale, an `*_INF` completeness requirement the span search cannot meet under a
bound, a need for pool-wide submission detail, or novelty findings going stale often enough on recently classified
variants to matter.

### Goal: is the gene-disease relationship valid, and by what mechanism?

Two of SVCv4's structural multipliers come from the gene rather than the variant: the curated **validity** level of the
MDE caps the class and gates the mechanism multiplier, and the **mechanism** level scales positive predictive points.
Both are stated per MDE — gene × phenotype × inheritance × mechanism — and never per gene, which is what shapes the
response.

**RPCs.** `GeneDisease.DescribeGene`.

**What the upstreams give, and what has to be remapped.** This is the one goal with no per-variant API to wrap. Its four
sources are gene-level datasets — ClinGen gene-validity, ClinGen dosage, GenCC submissions, and a PanelApp Australia
dump — so the interface loads all four at startup from a GCS bucket and answers every lookup as an in-memory join.
**Every table is keyed by HGNC id**, which is stable across symbol churn: a symbol can be retired and reassigned, so the
request names the gene by that id alone and carries no symbol field for a lookup to drift from. (ClinVar stays
symbol-keyed, because its upstream API searches by symbol.)

**Sourcing from a bucket rather than downloading at startup is what keeps a scale-to-zero service viable.** GenCC's
download endpoint caps near twenty requests a day, which per-startup downloads would exhaust under cold-start churn. So
a weekly refresh Cloud Run Job is the only thing that touches those upstreams, and the interface reads what it left
behind.

Three tables are stored **verbatim**, fetched with a conditional GET so an unchanged file is not rewritten. The PanelApp
dump is **transformed**: the Mendeliome and Incidentalome panels — the two carrying the diagnostic long tail — are
aggregated per HGNC id into the maximum confidence across the two, the mode of inheritance of the highest-confidence
entry, the gain-of-function veto flag, and the de-duplicated per-gene evaluation comments. PanelApp publishes no entity
tag, so it is rebuilt each run.

The rpc's one live dependency is the MONDO subclass closure at EBI OLS4, reached only when a request names an entity the
gene is not curated for under that exact term. Curators routinely curate a numbered subtype of the term a presentation
names, so identifier equality alone under-matches, and the ontology's subclass relation is what settles "is a kind of"
without a label comparison.

**The response shape follows from "per MDE".** Four decisions:

- **Nothing is reduced across entities and none is selected.** Which entity a presentation belongs to is a judgement
  over the full clinical picture (SM21), and a gene-wide maximum answers it by taking the strongest, discarding the cap
  the chosen entity imposes.
- **Within one entity, code does reduce**, because several submitters file assertions about the same entity and an
  aggregation under a published vocabulary is not a judgement.
- **A named entity the curations do not settle raises, rather than resolving to the nearest.** The inheritance mode is
  half the key it resolves on, and the mode also feeds SM3's DAFT — so collapsing it would answer the gate right and the
  frequency arithmetic wrong.
- **The classification-to-gate-level translation is the library's, not the caller's.** Curators publish
  *classifications*, the gate is keyed by *levels*, and several published classifications name no level at all, so one
  map holds vocabulary, strength rank and gate level together. The level is a `GateLevel` wherever it appears — what the
  rpc states per entity, and what `themis.svcv4` takes. The framework partitions validity once; a second spelling inside
  the library would be one more thing to keep in step with it.

PanelApp asserts panel membership rather than validity, so it contributes no entity to any of this.

**The mechanism signals sit in a gene-scoped block**, apart from the per-entity fields, because they are curated per
gene: the dosage-sensitivity haploinsufficiency score and PanelApp's flags say something about the gene, not about the
entity a caller chose. On a gene carrying both a loss-of-function and a gain-of-function entity (SM21), a gene-level
score read as the chosen entity's is the error that would scale the multiplier on every LoF path. What those signals
*mean* — that the mechanism level is a rubric an analyst scores, that Uncertain is a floor and never a default — is
[`svcv4-interpretations.md`](svcv4-interpretations.md#mechanism-a-rubric-an-analyst-scores-not-a-field-to-retrieve).

**Worked example.** In the July 2026 ClinGen snapshot, 492 of 3,017 genes carry more than one curated entity, 315 of
them at more than one classification; GenCC's submitters disagree on 5,319 of 15,464 entities; and 1,185 (gene, term)
pairs are curated under more than one inheritance mode. A gene-wide maximum would have answered every one of those with
the strongest classification on file.

**Moving off the upstreams.** The live MONDO closure could be replaced by a precomputed one shipped with the tables, at
the cost of a fifth object the interface cannot start without ([Alternatives](#alternatives-considered)).

### Goal: how relevant is the exon the variant lands in?

The matrix's other axis is SM18's All / Most / Few exon-relevance tier. The design question is not how to fetch the
inputs but what a retrieval may *conclude*: the tier scales every positive predictive path, and SM18 calibrates neither
half of its own definition.

**RPCs.** `Transcript.AssessExonRelevance`, over the exon table `Transcript.GetStructure` returns.

**What the upstreams give, and what has to be remapped.** Four sources compose here: VariantValidator's per-transcript
exon spans, GTEx isoform expression, gnomAD constraint and per-base pext, and ClinVar's pathogenic-variant density. The
remapping is where they meet. pext is gnomAD's per-region values weighted over VariantValidator's exon spans, and the
transcript inventory is an interval test run **natively in each annotation namespace**, so no accession crosswalk is
needed to answer a structural question. gnomAD's MANE Select pair is the one RefSeq/Ensembl pairing any upstream here
publishes, and it can differ from the release the exon table flags — which is why the pair is returned rather than
assumed.

**What it returns, and what it withholds.** MANE membership, a gene-wide transcript inventory against the assessed exon
— per transcript, in both annotation sets, with a four-outcome verdict rather than a carries/omits boolean — GTEx
expression, constraint, a per-exon **pext profile** rather than a scalar, ClinVar density, and **no All / Most / Few
tier**. The membership test is a fact the rpc owes; the tier is the judgement it must not make
([Alternatives](#exon-relevance) weighs the field it declines to carry). Why the tier is uncalibrated, why pext cannot
establish *All*, and what an empty omitting set does and does not license are readings of SM18
([`svcv4-interpretations.md`](svcv4-interpretations.md#exon-relevance-what-the-tiers-admit)).

**A record the interface cannot read is named, not raised on and not dropped.** One unreadable annotation record would
otherwise take pext, constraint, density and expression down with the inventory, while dropping it silently would let a
record the interface could not read pass for one that lacks the exon.

**Worked example.** The two gene-wide transcript payloads run to 1.26 MB on the largest measured gene, against a 60 s
per-request upstream timeout, and are issued concurrently with the assessed transcript's own — so the inventory costs
one extra round trip rather than three, which is what makes returning the whole census affordable.

**Caveats.** The density here is counted at a fixed review-status floor, a coarse burden count feeding no scored
comparison — unlike the ClinVar pool's floor, which is the caller's
([above](#goal-what-else-has-been-classified-at-this-codon-this-exon-and-across-the-gene)). GTEx samples no retina, no
megakaryocyte or platelet and no trabecular meshwork, so for some entities SM18's abundance limb is unanswerable from
these sources at all; the transcript-level data that exists ships as bulk files behind no query interface, so closing
that gap is an ingest rather than a wrapper.

### Goal: has a calibrated functional assay measured this variant?

`*_FXN` reads a calibrated functional assay's OddsPath. MaveDB is the public deposit of MAVE score sets, and the whole
design question is *which allele is asked*, because that decides what comes back.

**RPCs.** `MaveDb.DescribeVariant`, keyed on the variant's **ClinGen allele, never on HGVS text**.

**What the upstream gives, and what has to be remapped.** MaveDB stamps every mapped variant with a ClinGen allele id
and answers a list of them in one request, so identity is the natural key. HGVS is not available even as a fallback: its
only HGVS-bearing endpoint is a score set's whole dump, and MaveDB renders each variant's protein change against its own
target sequence, so no expression a caller holds equals the stored string. Two remaps follow. **Both the canonical and
the protein allele are registered and asked**, because one variant can be deposited at the nucleotide level in one score
set and at the protein level in another and MaveDB's own bridge between the two is incomplete. And **the candidate order
is derived** — match directness, then publication date descending, then identifier, and along it the first *calibrated*
deposit, else the first *scored* one — rather than taken from the upstream's serialisation. Deriving it matters because
every published score set of a gene typically sits at the same version, so an identifier tiebreak alone resolves to
registration order and consults the newest scan last. Why that is the right evidential ordering, and why a depositor's
primary calibration marker is honoured, is
[`svcv4-interpretations.md`](svcv4-interpretations.md#functional-assays-which-deposit-speaks-and-about-what).

**Worked example.** For one LDLR synonymous variant the canonical allele's protein-equivalence list is empty while its
protein allele returns two deposits. A canonical-only question answers "no assay covers this variant" for a variant two
deposits score — which is why both allele kinds are registered and asked, even though on most alleles sampled the
canonical id alone would have sufficed.

**Where it raises.** A deposit marking several calibrations primary has no ground for a choice, so it raises rather than
guessing. A deviation from the documented response shape raises too, rather than yielding no candidates: the two would
otherwise arrive as the same `NOT_FOUND`, and one renamed upstream field would report "no assay covers this variant" for
every variant asked.

**Caveats.** MaveDB holding no score set for the allele is a real answer: no MAVE assay exists, which removes a code
rather than setting a value.

### Goal: does a VCEP publish its own criteria specification?

SM3 ranks a curated VCEP or community threshold first among its DAFT methods, and several other codes defer to a panel's
gene-specific wording. A run therefore has to be able to *read* a panel's specification and cite it; an ad-hoc page
fetch yields text nothing downstream can re-check.

**RPCs.** `Cspec.ListSpecifications`, over the ClinGen CSpec Registry.

**What the upstream gives, and what is deliberately not remapped.** The registry serves ACMG/AMP-2015 profiles, and
there is no SVCv4 profile in it. So this rpc emits no SVCv4 code, point or DAFT: a panel's own points come back as a
*string*, its criteria-combining rules stay untyped, and a threshold enters SM3's ordering at the rung its own
derivation earns rather than at method 1 by virtue of who published it
([`svcv4-interpretations.md`](svcv4-interpretations.md#criteria-specifications-a-vcep-profile-is-acmg-2015)). Typing a
panel's ACMG-2015 vocabulary into SVCv4's would be a translation nobody sanctioned.

**Nothing is snapshotted server-side.** A paper's rendering stored in the literature corpus is the anchor a quote
directive resolves against and a linter can re-check; Zenodo already holds each document version under its DOI, so a
snapshot here would duplicate a permanent third-party artefact. The consequence, stated in the contract: a quote from a
specification is verifiable against the DOI and not against a store a linter can re-read.

**Worked example.** Three cases forced the rpc. One gene reaches a specification marking the null-variant criterion not
applicable, with the panel's reason beside it; another reaches a panel's phenotype tiers and frequency cut-offs; and a
third reaches a panel's population-frequency threshold with **no derivation anywhere in the registry** — which is itself
the finding the rpc has to let a run report.

### The contracts are written against the upstreams' failure modes

The hard part of wrapping a public database is not the happy path. Five cross-cutting rules follow, each fixing a way an
upstream's answer is silently wrong. Per-rpc instances of each are in the protos.

#### An upstream's "no record" is a finding, and gets its own status

Absence from gnomAD *is* the `POP_FRQ` rarity input; MaveDB holding no score set means no MAVE assay exists; a splice
predictor returning no score means the position is unscorable. These are settled answers, so they map to gRPC
`NOT_FOUND`, leaving `UNKNOWN` to mean an uncharacterised fault. Collapsing the two would make a caller retry a question
already answered, and would make "absent from gnomAD" unusable as evidence.

The status is also what a caller's retry helper keys on, so the taxonomy has to hold end to end. `NOT_FOUND` and
`INVALID_ARGUMENT` are answers and are never retried. `RESOURCE_EXHAUSTED` joins them, because no rpc here rate-limits
its caller — it means the response outgrew the transport, which re-fetching reproduces. A helper matching on message
text instead would re-acquire the coupling the taxonomy exists to remove.

#### Where absence is scored, a shape precondition is required, not optional

`Gnomad.DescribeVariant` and `Splice.PredictDeltas` are the two rpcs where an absence is *scored* — MaveDB's absence
removes a code rather than setting a value — and both upstreams answer an id they cannot parse with a **200 carrying an
absence** rather than an error.

> **Hazard.** Without a check, a caller's typo is scored as "absent from gnomAD": evidence manufactured from a mistyped
> field, and never retried, because absence is settled.

Two mechanisms, and both are needed. Each rpc holds its positional id to the *stricter* of the two upstreams' spellings,
since a check calibrated to the more permissive one would still pass the other an id it cannot score. But no syntactic
check reaches the semantic cases — a nonexistent contig, a position past the end of a real one — so each adapter also
reads its upstream's own message, the only reliable discriminator between "cannot parse" and "not held". The dataset is
checked for the same reason but as policy rather than as a limit of the upstream: gnomAD resolves releases this rpc does
not serve, and a third dataset would silently change the allele-frequency denominator under a caller reading the result
as a v4 filtering allele frequency.

#### An upstream 4xx is placed on that taxonomy, not passed through

Where a 4xx is *about the request*, a non-429 4xx is the source judging the request as issued, so it becomes
`INVALID_ARGUMENT` — or `NOT_FOUND` where the source spells "no record" that way, as NCBI's nucleotide fetch does for an
accession it holds no sequence under. **429 and 5xx stay retryable**, and 429 is the reason the rule is not simply
"4xx". Passing every status through unmapped makes a deterministic 400 cost four calls and a backoff before failing
identically.

Which reading applies is per **endpoint**, not per source: one endpoint of a source can spell "no such entity" as a 400
while another answers 200-and-empty for the same question. ClinVar's archive fetch does both, which is what makes it the
case where "no record" is not `NOT_FOUND` at all.

The qualifier is load-bearing, and **three transports are exempt** under it — mapping their 4xx would be worse than the
defect the rule fixes.

- At **gnomAD** the caller's fields ride in GraphQL variables and a bad one comes back inside a 200, so no 4xx there is
  a verdict on one: measured, its 429 is the rate limiter and its 400 is a malformed query document, which is ours to
  fix.
- The **Broad splice hosts** answer every variant-level problem with a 200 carrying an error string, so their 4xx is a
  malformed request line or a moved endpoint — reporting one against the caller's variant would name an input that was
  not the problem.
- **EBI OLS4** is exempt on a different reading: the value reaching its URL is a curated term read off a reference
  table, never a caller's field, so a 4xx there is a stale table or a retired MONDO term. It becomes an uncharacterised
  fault rather than `INVALID_ARGUMENT`, which would blame a well-formed request for the reference data behind it.

The reference-table refresh job is outside the taxonomy for a fourth reason: it is not on the request path at all, so a
4xx there names no request field and fails the run.

> **The test is not "does a caller-chosen value reach this URL".** It does at two of the three. The test is "would a 4xx
> here be the source's verdict on that value". Where the answer is no, the exemption has to be paid for at the boundary
> instead — which is why the two rpcs whose absence is scored carry shape preconditions.

#### Two sources disagreeing is neither an absence nor a bad request

`NOT_FOUND` is the vocabulary absence is *scored* in, so it can only carry what a source actually said about a subject
the caller chose. Where a lookup is keyed on an id that came from *another* source, "no record" says something else
entirely. ClinVar's archive fetch is keyed on the accession the registry's crosswalk named for the allele, so ClinVar
answering that it holds nothing under it — as a 400 stating the id resolved to nothing, or as a 200 carrying an empty
result set — is one source contradicting the other about a record's existence. Reported as an absence it becomes the
novelty finding, off an answer the crosswalk denies; reported as a bad request it sends an analyst hunting for a typo in
a field the service filled in itself. The same shape reaches `SearchCodingSpan` through the gene symbol: the exon table
names it, and where ClinVar indexes nothing under it every span is empty at any coordinates — which is precisely the
finding "no informative variant at this codon" the rules ask for.

So the sources disagreeing is `FAILED_PRECONDITION` — the status a well-formed request already gets where the sources
cannot settle what it names, as `GeneDisease` gives it for a disease entity the caller has to restate. Both sources
answered, and reconciling them is this service's own job rather than anything a reissue or a corrected field can change.
The message names both sources and what each said, because that is where reconciling them starts.

#### Every handler is bounded, and the bound is a status

An unbounded handler does not produce a slow answer, it produces no answer: the caller waits on a call that cannot fail,
so nothing downstream can retry it, route around it, or report it. Inside a sandbox the wait ends with the harness
killing the whole snippet, taking the results of every call before it in the same script.

So there are three nested budgets, each strictly inside the one above: the harness's limit on one shell tool call, the
guest's limit on a whole retried call, and the service's per-handler deadline. Expiry maps to `DEADLINE_EXCEEDED` naming
the rpc, which also cancels the upstream work rather than leaving it holding the instance. The guest's budget exceeds
the service's ceiling rather than matching it, deliberately: a service that can answer gets to, and the snippet keeps
the rest of the tool budget to report what it did get. `DEADLINE_EXCEEDED` is therefore **not** retryable, unlike the
other never-reached-an-answer statuses — the deadline is a budget the caller itself set, and reissuing spends it again
on the same slow path.

> **Trade-off.** The handler deadline is a *cut*, not a ceiling above the worst case. The chained rpcs still sum higher
> than it, because each awaits several per-upstream timeouts in turn. Bringing those into line is what would make the
> handler bound generous rather than merely bounded.

### Provenance: every fact carries the releases it rests on

Every retrieval carries a provenance record — source, every release the fact rests on, the exact query, and the
retrieval time (PRODUCT §6 verifiable provenance). A live value stamped as-of its query time is reproducible without a
mirror, which is what makes the no-mirror stance viable at all.

The release list is **neither optional nor partial**. Two rpcs whose answers get joined are the reason: a
transcript-annotation release and an expression release name different transcript sets, so where neither is stated a
version disagreement between them reads as a disagreement about the gene — a curator question manufactured out of an
unstamped join. An upstream that publishes no version of its own is asked for one separately, and a release that cannot
be established is a fault rather than a bare assembly name. A partial list is worse than none, because it reads as a
complete one.

Provenance is a repeated field on **every** response, including single-source ones. A caller that iterates one shape
everywhere cannot get it wrong, and a response that later composes does not change contract.

### The in-sandbox library: `themis.svcv4`

The deterministic framework logic ships into the sandbox image as an at-rest domain library plus a skill doc, imported
and run by the agent in code mode. **Not a service**: it needs no egress and no secret, and it iterates fast because the
combining rules are what we tune, so an rpc would add a network hop per re-score and couple that iteration to the egress
deploy for nothing. A library is also reusable by the eval harness and by any server-side Python.

The model calls it with the evidence plus its own judgement inputs — the mechanism factor, the exon factor, the eligible
informative variants, the DAFT parameters — and gets back the transparent point tally and class band, then authors the
verdict reasoning over the claims. **The evidence reaches it as the response messages themselves**, since those are what
the model is holding: a **door** per source reads each one at the paths its own contract documents and returns the code
it feeds, so which path a figure is read at, which vocabulary a term resolves to and which tier a score bins to are the
library's steps rather than ones the model writes out. The judgement arrives beside them, typed. The call goes through a
**typed builder per variant type**, one for each of the framework's ten workflows: the caller supplies the judgement
inputs and the builder holds that workflow's caps and path structure, and the routing consequence selects the builder so
that naming one is not a step either.

It holds:

- the scoring and combining engine — points to class, the matrix multiplier on positive predictive points,
  informative-variant points added *after* the matrix, per-code caps and the concept and category caps above them, the
  missense/splice max-path, the GDV-gate cap;
- the splice-tier cells per flow and per colour, including the two-layer combine cap the reference states only as a
  union across the three workflows that reach a splice tier;
- DAFT computation with `POP_FRQ` binning, and `POP_HMZ`;
- Grantham distance (the missense same-codon comparator) and the four summable missense informative-variant sub-rules;
- the NMD/NSD determination from transcript structure;
- OddsPath and animal-model calibration tables;
- and the score-to-bin thresholds with the single-predictor-per-gene enforcement.

Its data is the SVCv4 reference — typed value modules in the package — and the predictor policy beside them; the ten
workflow-diagram transcriptions its per-variant-type structure is read from stay in the corpus, at the revision the
reference's citation pin names. Where a transcription flags a diagram-versus-text numeric conflict, the ClinGen Pilot
Calculator is the tie-breaking **oracle** (see [Eval](#eval-the-contract-the-graded-cases-then-the-calculator-oracle)).

**Two of its numbers are explicit caller inputs rather than derivations**: the stop-lost NSD branch, routed through the
null path, and the exon-duplication not-tandem cap. Neither has a worked case anywhere in the standard to check a
derivation against; requiring the number keeps it the caller's and visible, where deriving it would fold an invented
figure into the tally.

### The library takes categorical judgements and returns derivations

All seven `CLN_*` and `LOC_*` codes are **deterministic downstream of a categorical judgement**. What is irreducibly the
analyst's is the reading that indexes the table — the phenotype tier, whether each of SM4's conjuncts is affirmed,
parentage, each relative's genotype and affected status, the penetrance band, the in-trans class, which yield figure
applies
([`svcv4-interpretations.md`](svcv4-interpretations.md#clinical-and-locus-observations-the-categorical-readings)). The
library takes those categorical answers and returns the points, the cell it read, and the per-observation derivation,
applying aggregation and caps. The judgement stays where the framework puts it; the arithmetic stops being a step script
— the Python the agent writes and runs for one analysis step.

**Every helper returns its derivation with its total, or the move is net negative.** A sum written out in a step script
is a number a reviewer checks against the printed table; a helper returning a bare total makes them reconstruct which
cell it was. The worst case is confirmed-versus-assumed in-trans phase, where points turn on one word in a referral — so
the derivation carries the phase *basis*, not the enum name.

Three signatures follow from readings that a single value would bury: the SM4 affected-proband column takes **three
booleans**, not one judgement; phenotype specificity takes the **numerator and denominator**, never a percentage; and
the exon inventory is tabulated with the **admitted set passed in explicitly**, which keeps SM18's admission filter
shown rather than buried. The splice colour splits for a different reason — everything but "does a score above the
threshold have a certain consequence" is a threshold table plus a truth table. Two framework invariants fold in with the
lookups: SM4's affected-proband code is unavailable outside two `POP_FRQ` values, and the library refuses a conditioned
code awarding points outside that gate, with no `POP_FRQ` in the tally at all, or under one it did not determine.

**Where a reading is unsettled, the library requires the value rather than deriving it.** A helper that resolved an open
conflict would hide it: a boolean for SM18's waiver would silently permit a double count of the same pathogenic
variants, and a `bool` for specific-versus-consistent phenotype reads the *absence* of an examination as False. The four
readings held open this way are in
[`svcv4-interpretations.md`](svcv4-interpretations.md#clinical-and-locus-observations-the-categorical-readings); the
mechanism level is a fifth, and Uncertain must never arrive as its default. Transcribing SM5's per-co-segregation row
table is the one external precondition on any of this; the rest is local.

The same argument reaches past the clinical and locus codes. Each of these is deterministic downstream of stated inputs,
and belongs in the library on that ground:

- the NMD/NSD inputs — the exon structure from a transcript, the PTC position from a protein HGVS;
- SM20's control-count-to-points tables;
- splice-colour routing;
- the exon-inventory tabulation with its pext comparison;
- the coding-prediction protein-fraction tier;
- and the missense informative-variant positional predicates (a list, never a boolean).

### The shipped package carries the rules, not the evidence for them

`themis.svcv4` ships into the sandbox whole, so its docstrings *and its reference data* are text a classification run
reads while classifying. They must carry the rule a caller needs to apply the framework correctly and not the evidence
for it: a worked case naming a gene, its parameters and its resulting points hands a run being scored on that gene the
answer, and the reference set is drawn from the same SVCv4 corpus these arguments were worked against. No image copies
`docs/`, so the evidence lives in these docs — reachable by every human who needs it, and by no run.

Two authoring rules follow, and **neither can be recorded in the shipped files themselves**: a note there saying an
example was withheld tells a run it is being evaluated, and turns every gene still named into a signal that that gene is
not a case.

1. **The shipped scoring reference illustrates no rule with a gene.** Each scenario carries its whole policy in its own
   scenario/action pair, so there is no asymmetry to reason from and no per-gene example to keep screening.
1. **Elsewhere in the package, a worked example names a gene the reference set does not**, and states the inputs it is
   worked from rather than a bare result.

Nothing automatic stands behind either rule, because what leaks is an answer and not a string: a gene the reference set
does not carry can still restate a case's result, so a check matching symbols would let through the text worth catching.
The screen is a reading, made when the text is written.

The tests are the exception, and not one the screen can close: a test worth running names its subject and states the
answer it expects. So the requirement falls on packaging rather than on authoring — an image carrying `themis.svcv4`
leaves the test suites out.

**Screen a supplement against the reference set's subjects before transcribing it.** The standard's own worked examples
name specific genes and exon ranges, and thereby state an answer for whichever of them a case is graded on. Substituting
a gene the set does not carry, or dropping the example, is a precondition on transcribing a supplement, not an
afterthought. What stays, stays for a reason: SM3's DAFT grids and SM20's control-count grids are carried, because an
X-linked MDE reaches no DAFT without the first and a small functional experiment reaches no points without the second,
and neither names a gene or states a case's answer.

> **The reference is deliberately quieter than the corpus it is transcribed from, and that silence is load-bearing.** It
> omits several cells and rules that each keep a live case question open, and carries no marker saying so — the correct
> state, by the rule above. The corpus holds complete transcriptions of the same tables, so re-reading one of them into
> the reference is the likeliest way an omission gets helpfully closed. Guard it whenever a value is re-read.

### Eval: the contract, the graded cases, then the calculator oracle

Three layers, each answering a different question.

- **Unit tests** per rpc against recorded upstream fixtures, so CI needs no live network, and per library function and
  builder against worked micro-cases. Each door is driven from the service's own response message and `classify_variant`
  from a variant's consequence, so the path from a wire message to a scored tally is exercised whole rather than only in
  pieces.

- **Graded cases against curated ground truth.** Whether a class is *right* for a real variant is not something the
  library's own tests can settle: an expected class derived from the framework and applied to transcribed inputs is the
  arithmetic under test, run twice. It has to come from a curation instead — which is the classifier-evaluation loop
  scoring against the reference set ([`analysis-scenarios.md`](analysis-scenarios.md)), not a test suite.

- **The calculator oracle — the gold-standard diff.** The ClinGen Pilot Calculator computes classifications client-side,
  so the oracle fetches the bundle at runtime and evaluates it in an isolated JavaScript context with no ambient
  globals, diffing the cap tables, the banding across every class edge, the missense-versus-splice max path, and
  clamping.

  **It is a manual gate, not a CI job.** It needs a JavaScript runtime and network access, so a divergence surfaces only
  when someone runs it, and then as a diff a reader can attribute to the wrong side. Each pinned divergence therefore
  also carries a CI test asserting the pin's *our* side against the loaded reference: a library change that silently
  converges on the calculator fails there rather than waiting for the next manual run. What the oracle cannot answer at
  all: per-code point values, which the bundle does not carry — so scoring a case end to end, evidence in and class out,
  is beyond its reach — and the GDV gate, which the calculator does not implement. Over what it does reach the combining
  engine matches exactly, and the pinned divergences are readings of the standard rather than defects on either side
  ([`svcv4-interpretations.md`](svcv4-interpretations.md#where-the-standard-contradicts-itself)).

  > **The bundle (© Baylor College of Medicine) is read, not copied.** The oracle fetches it at runtime and evaluates it
  > in memory; nothing from it is vendored, quoted or copied into a tracked file, apart from the calculator-side ranges
  > the pinned divergences state.

**Predictor-attributable divergence is recorded, not scored wrong.** Where the reference set derives `MIS_PRD` from a
different predictor than the policy names, both choices are valid — each was pre-selected — so the divergence is
recorded with both predictors and both scores, the treatment the reference set gives any divergent answer. Diverging on
the policy's *own* predictor is an error.

### Proto layout

The contract is **one `.proto` per interface** under [`schema/proto/themis/rpc/`](../../schema/proto/themis/rpc), each
in package `themis.rpc.<source>`, plus [`evidence.proto`](../../schema/proto/themis/evidence/models/evidence.proto) for
the value types they exchange.

**A type sits in the shared file only where several interfaces exchange it.** Provenance, the routing consequence and a
genomic span qualify — a ClinVar record and a transcript exon are placed by the same coordinates — so housing them in
any one source's file would make the other eight import that source's contract to name a coordinate. The
coding-coordinate types and the transcript-namespace enum have one exchanger each, so they live with it: the c.
coordinate types in `clinvar.proto` and the namespace enum in `transcript.proto`. The shared file declares no service
and imports no rpc file, so the dependency runs one way.

**Every response layers the same three things**: the typed load-bearing fields, then the upstream material, then the
provenance list. Typing only the load-bearing fields and passing the rest through is the "external data" posture
[`proto.md`](proto.md) already sets; the typing that *is* added is documentation, not translation. Within the upstream
material, three cases are distinguished by what a caller does with it.

- **State a caller branches on is typed**, however raw its origin. The ClinVar pool census is the case that forced the
  distinction: the search term issued, the total it matched and how much was fetched decide whether a pool is a census
  or a prefix, and the term itself is decision-bearing, since the membership gate is a six-token disjunction.
- **A payload nothing branches on stays a `Struct`, and where a response carries two upstreams' payloads they are named
  apart** — a ClinVar span search returns the VariantValidator projection it ran on under its own field, not pooled into
  one `raw`. A reader of a replay payload is a human or a re-run, and a field named for whose payload it is is one they
  can attribute.
- **A payload with a published schema is generated, not hand-modelled.** The queried allele's whole VCV record is the
  richest thing on this surface — per-submission observations, assertion methods, citations — and NCBI publishes an XSD
  for it, so the message is generated from that XSD rather than transcribed. The generating happens in its own repo,
  which publishes the record type as a pinned wheel; the rules that come with consuming one are in
  [`proto.md`](proto.md#generated-upstream-schemas). A `Struct` there would make every consumer re-derive a schema the
  source already publishes.

An rpc becomes agent-callable by a file option on its proto, from which the hatch allowlist and forwarders are generated
([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)). That is why adding an rpc is not free: it costs a forwarder, an
allowlist regeneration, a fixture table and a servicer method.

## Alternatives considered

Each option weighed, and the reason it was rejected, grouped by what it is an alternative to.

### Shape of the service surface

- **One gRPC service carrying every source's rpcs.** Rejected on two counts, neither of them deployment shape — the
  deployment is one either way. The **contract** is the first: one service puts twelve unrelated rpcs and their whole
  message set in one file, so every consumer of a gnomAD id compiles ClinVar's submission model, breaking-change
  detection reports one baseline for twelve independently evolving upstreams, and the file grows past what a reader — or
  a model holding it as context — can hold. The **name** is the second: an rpc is named for what it does, and the
  qualifier saying which source answered it is the service. Three interfaces each serve a `DescribeVariant` about their
  own source, where one service would force the source names into the rpc names and they would read as sources rather
  than operations. Per source, the file boundary and the naming boundary coincide with the boundary the upstream itself
  moves along.
- **One service per evidence tool, each its own deployment.** Rejected: under the bubblewrap boundary there is no
  isolation to gain, since there are no per-service egress allowlists, while each deployment adds a cold start, an image
  and a hatch-wiring step. Sharing one deployment costs nothing on failure isolation, which is already per rpc.
- **The scoring engine as an rpc.** Rejected: it needs no egress and iterates fastest, so an rpc adds a network hop per
  re-score and couples that iteration to the egress deploy.
- **A separate transcript-inventory rpc.** Rejected for now: `Transcript.AssessExonRelevance` already takes the exact
  key the inventory is scoped by and already joins its sources on genomic interval, while a new rpc costs the full
  per-rpc price for a second consumer that does not exist. Revisit if something wants the inventory without an exon.
- **Fully typing every upstream payload.** Rejected as needless massaging: the model parses raw JSON reliably, so only
  the ACMG-load-bearing fields and the remaps are typed. The exception is a payload whose owner publishes a schema,
  where the message is generated rather than authored and the massaging cost is a version pin
  ([Proto layout](#proto-layout)).
- **Letting the model do the point arithmetic too.** Rejected: the tally must be reproducible and auditable, and the
  score is shown-not-load-bearing. Deterministic code yields the same points every run plus a trace the curator can
  check; a model re-deriving arithmetic per run gives neither. The model's contribution is the judgement *inputs* and
  the verdict, not the addition.
- **Letting the model pick the predictor per variant** — "judgement everywhere", taken literally. Rejected: SM6 bans it
  as multiple-testing over correlated metapredictors, so the expert judgement moves to policy time — a place where
  "everywhere" would break the standard.

### Identifiers and keys

- **Routing a positional variant id to Ensembl's region endpoint** rather than refusing it in `Vep.Annotate`. *Feasible*
  — that endpoint carries the predictor scores the interface depends on — and rejected anyway, for two reasons. The
  normalisation rpc hands the agent a GRCh37 positional id beside the GRCh38 one and nothing in either says which it is,
  so the same silent-wrong-variant failure returns by another route. And a gnomAD-style indel id is VCF-style with a
  padding base, making region derivation a second normaliser whose off-by-one is as silent.
- **Accepting an Ensembl accession in the normalisation or exon-table rpcs** so the expression rows join directly.
  Rejected: the RefSeq-only spine is load-bearing, since canonicalisation and the coding-coordinate arithmetic both
  depend on it and VariantValidator has no Ensembl record. The problem it was meant to solve dissolves on coordinates
  instead — the inventory reads each namespace natively and runs the interval test in it.
- **`MaveDb.DescribeVariant` taking a ClinGen allele id directly**, which a caller already holds. Rejected for now: a
  bare canonical id reaches only nucleotide-level deposits, and the protein-level ones — where a missense or synonymous
  variant is usually assayed — are keyed on an id the caller has no typed field for. Accepting an id would save a round
  trip but still needs the id expanded through the registry, so taking the expression keeps one path. Surfacing the
  protein allele id on the normalisation response would remove the hop altogether, at the cost of a proto field and a
  per-transcript choice the caller then has to make correctly.
- **`MaveDb.DescribeVariant` accepting a genomic HGVS.** It would work — the registry resolves it to the same canonical
  allele. Left out because nothing upstream produces one, and each accepted shape is a form whose rejection has to stay
  legible.
- **Ensembl's transcript-lookup endpoint for the exon table.** Rejected on three counts. It is keyed on an Ensembl
  accession, so a RefSeq-first service must cross namespaces first. It returns genomic exon bounds only, leaving the
  caller to sum CDS lengths to reach coding coordinates — the arithmetic the rpc exists to remove. And the Ensembl
  alignment can differ from the RefSeq one, so the table would not describe the transcript the rest of these interfaces
  resolve onto.
- **Taking the PTC position as a caller input** — exon table only, no sequence. Rejected: the point of the rpc is to
  remove the hand-derivation, and the caller cannot derive a PTC without the bases either. Fetching the transcript
  sequence costs one cached upstream request and makes the product classification a read rather than an assumption.
- **A sequence-length bound on the positional ids, as a shape rule.** Kept, but as a **transport** bound refused with
  its own message. A per-allele character bound is neither: the normalisation rpc hands out a longer id for a kilobase
  deletion, and both rpcs refused it with a *format* complaint about an id in exactly the right format, closing
  frequency and splice prediction for every large indel. Measured against the live upstreams, gnomAD carries the id in a
  POST body and parses it whatever its length, while the splice hosts carry it in a request line and fail past a size
  limit. There is no second route for an allele past the bound: the splice predictors cannot be handed a sequence, and
  an allele that size is in gnomAD's structural-variant release, which these rpcs do not serve.

### ClinVar and gene-disease scoping

- **A gene-wide all-classification ClinVar pool** instead of a span search. Rejected: wrong unit and unfetchable. The
  informative-variant rules key on a codon or an exon, and on the genes where the benign and VUS arms matter the
  gene-wide set cannot be fetched. Widening the existing pool with a classification filter has the same defect — still
  gene-scoped, still truncated exactly where a codon census has to be complete.
- **Sweeping a string search across transcript versions** to find an allele ClinVar indexes under another rendering.
  Rejected as the primary route: the versions ClinVar indexes are not knowable in advance, so each guess is another
  rate-limited round trip — and the motivating case is not a version problem at all, since the current version is
  indexed under a repeat notation no version sweep would have produced.
- **Keeping a string search as a recency check on the empty-crosswalk path**, so a variant classified since the last
  crosswalk ingest is not reported as novel. Rejected for what it costs to buy a closing window: it reinstates the exact
  lookup whose miss motivated identity keying, gives every absence two routes and therefore an outcome enum to say which
  one answered, and re-exposes the NCBI tokenisation hazard — an unquoted HGVS string is tokenised and answered with a
  *different* allele of the same codon. The staleness it addresses is bounded, stated in the provenance, and dissolved
  by the dump.
- **Staying on ClinVar's query API as the end state.** Rejected, for the root cause the goal section names: a
  dump-backed index keyed on canonical SPDI dissolves five separate defects at once
  ([above](#goal-what-else-has-been-classified-at-this-codon-this-exon-and-across-the-gene)). What it costs is
  freshness, which the release list states, and the ingest itself; what it does not cost is any change to the rpc
  surface, which is why living with the query API in the meantime is not a dead end.
- **Scoping `GeneDisease.DescribeGene` by a free-text disease string** (substring containment, survivors reduced by
  max). Rejected: it fails three ways, each silently. A caller's natural phrasing that is not a substring of the
  curator's label drops the ClinGen curation, and lets the classification fall through to whichever submitter spelled it
  the caller's way. A shorter phrasing matches several distinct entities and the max returns the strongest, removing the
  cap SM21's multiple-disorder rule exists to impose. And proto3 cannot tell an unset string from an empty one, so the
  default behaviour answers for the whole gene with nothing saying so. Scoping by MONDO id *alone* does not fix it
  either, since curators curate a numbered subtype of the term a presentation names. What separates the cases is that
  "which entity is this presentation's" is a judgement and "is this curated term a kind of the one I named" is a
  question the ontology answers: the first goes to the model with the full entity list, the second to code with the
  closure.
- **Removing the entity parameter outright**, returning the list and nothing else. Rejected: it makes the three failures
  impossible rather than correct, and it deletes the only code-side check on the entity choice — a missed lookup yields
  an unset gate level the library raises on, where a wrongly-chosen entity off a returned list yields a *valid* gate key
  and nothing raises.
- **A precomputed MONDO closure shipped with the reference tables.** It would remove the interface's one live
  dependency, at the cost of a fifth reference object the interface cannot start without. Deferred until the latency or
  the OLS4 dependency proves to matter.
- **Live PanelApp on the request path.** Rejected: a per-request call makes the rpc's availability track PanelApp's and,
  with scale-to-zero, re-hits the API on every cold start; a dump also lifts the cross-panel maximum off the hot path,
  keys on the stable HGNC id, and spends no cold-start rate budget. The price is scope — the dump covers two panels, not
  all Australian ones.
- **A FUSE volume mount for the reference bucket** rather than the storage client library. Chose the library: it is the
  repo convention, places no cap on reads, and adds no net-new infra mechanism, where a mount is a deploy-time volume
  plus a lifecycle to reason about. The tables are small and read once at startup, so the in-process fetch costs nothing
  a mount would save.

### Exon relevance

- **An All / Most / Few tier on the response.** Rejected: two of SM18's three admission filters carry no test and no
  threshold in the standard, and its one quoted pext figure is for an exon the MANE rule already disposes of — so the
  All-versus-Most span is uncalibrated. A tier field would encode a threshold the standard declines to state, and would
  be quoted back as the standard's answer.
- **A "no transcript omits the exon" boolean** instead of the inventory. Rejected: it reads as the verdict, and the
  condition it names licenses *All* only together with an abundance limb the rpc cannot supply. An empty spans-but-skips
  grouping carries the same information and cannot be requoted as "the rpc said All".
- **Rebuilding the inventory from the splice predictor's payload.** Rejected as a denominator: it does carry
  per-transcript exon tables and a namespace pairing, but it is that predictor's own annotation snapshot and holds fewer
  transcripts than the curated set — a good crosswalk source and a wrong census. It also arrives only when a splice call
  was made, and a value that settles a matrix axis needs an advertised home.
- **Renaming the eval module's measurement buckets to match the proto enum.** Deferred, not rejected: that module
  partitions the same four outcomes under different names, and its keys are a corpus-schema version the reference
  measurements are stored against. The two agree on semantics, so the split is a naming cost, and moving it belongs with
  the next schema bump rather than inside a service change.

### Scale and framework encoding

- **Local mirrors of gnomAD and the other per-variant sources.** Staged, not rejected — the at-scale form of this
  design. Volume decides it: mirroring hundreds of gigabytes to answer a couple of dozen per-variant calls is the wrong
  trade, a live value stamped with its retrieval time is reproducible without a mirror, and no egress-posture argument
  applies, because a trusted service egresses freely. The triggers that flip it: a throughput at which per-variant
  public calls become the bottleneck — where the per-IP rate limits also bind — or a hard offline, latency or
  availability requirement. ClinVar is the one source with its own decided end state, above; the gene-disease reference
  tables sit outside the question, having no per-variant live API.
- **Staying on the frozen public Ensembl REST indefinitely.** A separate trigger on one upstream. Its final release has
  shipped and it will receive no further updates; what the freeze pins is the Ensembl release, and therefore the
  transcript set, the MANE version and the dbNSFP branch. Nothing breaks, but the answers drift from current annotation
  silently, which is why the release stamps on the wire matter *more* under a freeze rather than less. The announced
  successor is a GraphQL *lookup of pre-computed output for catalogued variants*, so a novel variant — the case a
  classification exists to handle — is not in it; it is also scoped to gene and transcript consequence alone, and it
  reports errors inside a 200 where REST returned a 404, so error handling does not port either. The end state for this
  upstream is therefore a self-hosted `ensembl-vep`, which takes HGVS directly. Nothing forces the move while the frozen
  endpoint serves.
- **Leaving the clinical and locus codes out of the library** and requiring the classification document to show each
  per-cell derivation instead, with a linter failing a code that shows none. Rejected: it named the symptom — a
  hand-summed total nobody can check — and picked the wrong remedy. The check it produced is satisfiable by a tautology,
  and tightening it does not survive contact with the tables. Only three of the seven codes have a source table that is
  a grid; one is a computation; one ships as a bare value list with no keys; and the capped families admit no legal
  derivation at all, since "terms name cells" and "terms sum to points" cannot both hold under a cap. Making the line
  library output retires the check rather than patching it.
- **Adopting an upstream encoding of the framework instead of transcribing it.** There is none to adopt.
  [`clingen-data-model/svcv4-model`](https://github.com/clingen-data-model/svcv4-model) (MIT) draws the line itself: it
  holds the *Classification Model* — what a classification is — while the *Method Model*, the rules that evaluate
  evidence and produce workflow-specific scores, is stated to live in CSpec, outside that repo and outside GA4GH
  VA-Spec. The data bears it out: its scores are bare floats with one numeric constraint in the whole model, its
  classification enum is documented in-code as a placeholder with no VUS subclasses, its one worked example contradicts
  its own threshold and invents a code that does not exist, and only the clinical workflows are modelled. VA-Spec ships
  three community profiles and no SVCv4 one; CSpec's specifications are all ACMG-2015 vintage. The realistic upside is
  different in kind: a VA-Spec SVCv4 profile would give a standard *output* format and a shared evidence-code
  vocabulary, worth adopting at the seam where results are emitted and never inside the scoring engine. CSpec gaining
  SVCv4 support is the one thing that could displace the transcription.
- **Naming an interface for the evidence it yields** rather than for its source — `frequency` instead of `gnomad`,
  `criteria` instead of `cspec`. Rejected: the source is what a caller has to know to read the answer — its identifier
  forms, its absence semantics, the release its answers rest on — and it is what stays true when the framework renames a
  code or moves a rule between supplements. Four interfaces carry a name that is not a vendor's, all for the same
  reason: `variant`, `transcript`, `splice` and `gene_disease` each compose several upstreams, so any vendor name would
  claim the whole answer for one of them. The deployment is `evidence` and covers papers too, which is why the
  curated-database half is named per source rather than by one word for all of it.

## Open questions

Unresolved points in this design, each needing a decision or an input. The framework's own contradictions, and the
readings applied to them, are [`svcv4-interpretations.md`](svcv4-interpretations.md)'s.

- **Absence carries no coverage distinction.** A caller reading `NOT_FOUND` cannot tell "not observed at a well-covered
  position" from "position not covered", which is the distinction rarity evidence rests on. Whether absence becomes a
  typed field on the responses whose absence is scored is undecided.
- **Which ClinVar need pulls the dump forward first.** Whether the E-utilities serve both the classified-variant pool
  behind informative variants and exon-level pathogenic density at usable query cost, or whether the density need — a
  count that has to be complete to mean anything — is what fires the dump trigger ahead of the rate limits.
- **Rate limits at multi-session scale.** gnomAD's and the splice hosts' limits are per-IP, so sessions sharing an
  egress IP share the budget. How many concurrent curators that tolerates before a mirror trigger fires is unmeasured.
