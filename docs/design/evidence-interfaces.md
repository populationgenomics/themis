# Design: evidence interfaces — the curated-database half of `evidence`

> **SVCv4 is a draft standard, and this is evaluation software.** The framework was released as a **pilot in July
> 2026**; its point values, thresholds, code names, and data model **may change before publication** (target: *Genetics
> in Medicine*, ~Jan 2027), and Supplementary Material 17 (non-coding variants) is unreleased. These interfaces exist to
> **evaluate whether the framework can be run autonomously**. They are **not a validated implementation, not for
> clinical or diagnostic use, and their output is not a clinical variant classification** — the values they return are
> public-database retrievals, and any classification built on them must be re-verified against the final published
> standard and the [ClinGen Pilot Calculator](https://calculator.clinicalgenome.org/v4/pilot/ui/classification).
>
> **The design is expected to move.** How the surface is split into services and interfaces, the contracts themselves,
> and the implementations behind them will all change as the framework settles and the evaluation feeds back. What this
> doc records is a concrete base layer to iterate on.

**Related:** [`svcv4-interpretations.md`](svcv4-interpretations.md) (the clinical readings of the framework these
interfaces apply — which frequency figure, which ClinVar terms, which exon tier), [`services.md`](services.md) (the
service pattern, and how several interfaces share one deployment),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the sibling literature interface — the *other* evidence
source), [`proto.md`](proto.md) (schema and serialisation posture), [`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)
(how an rpc becomes agent-callable), [`security.md`](security.md) (the default-on chokepoint rule the tool surface
follows), [`sandbox-worker.md`](sandbox-worker.md) (the bubblewrap guest, its empty network namespace, and the gRPC
hatch), [`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md) (the sandbox architecture around that
guest), [`run-review-loop.md`](run-review-loop.md) (the loop that reads runs against the curation worksheet),
[`../PRODUCT.md`](../PRODUCT.md) (the product frame: §4 tool layer, §6 facts + judgement, §12 the Spike); terms in
[`../../GLOSSARY.md`](../../GLOSSARY.md).

## Overview

Nine gRPC interfaces on the shared `evidence` deployment — one per curated public source — take a variant and return
what that source holds about it: the non-literature evidence a single-variant ACMG **SVCv4** classification needs. They
are the structured-data counterpart to the `literature` interface beside them in the same image, which answers from
papers.

This design divides the work of variant classification into four roles and fixes where each role runs:

- **Retrieval** that needs the open internet runs as a service-side RPC, because the agent's sandbox has no network
  egress.
- **Arithmetic** over SVCv4's deterministic rules ships into the sandbox as a Python library, `themis.svcv4`, because it
  needs no egress and the point tally must be reproducible.
- **Remapping** — the error-prone coordinate and vocabulary wrangling between upstream APIs and the rule library — runs
  in service code, so the offline library never sees an upstream's serialisation quirks.
- **Judgement** — every decision the framework hands to "an experienced analyst" — stays with the model and the human
  curator.

SVCv4 dictates *what* evidence a classification needs; this document decides *how gathering each line becomes runnable*.

```
sandbox (agent + themis.svcv4)              gRPC hatch            evidence deployment
──────────────────────────────              ──────────            ───────────────────
agent calls RPC ──────────────────────────► allowlist ──────────► Variant / Vep / Gnomad / ClinVar /
                                                                  GeneDisease / Transcript / Splice /
                                                                  MaveDb / Cspec  (+ Literature)
                                                                        │
                                                                  retrieves upstream data, remaps
                                                                  coordinates/vocabularies, stamps
                                                                  Provenance
                                                                        │
agent reads facts + makes judgements ◄─────────────────────────── typed response message
        │
        ▼
calls themis.svcv4 (in-process, offline)
        │
        ▼
derivation lines + points + class
```

The design body is organised in the order the framework asks its questions: one section per evidence-collection goal,
naming the RPCs that serve it, what its upstream gives and what has to be remapped, a worked example, and the caveats
that come with the source. What each of those readings of the framework *means* clinically — which frequency figure
`POP_FRQ` compares against, which ClinVar terms count as a pathogenic assertion, what licenses an exon-relevance tier —
lives in [`svcv4-interpretations.md`](svcv4-interpretations.md).

The contracts are the nine `.proto` files under [`schema/proto/themis/rpc/`](../../schema/proto/themis/rpc). Each states
its own identifier forms, its absence semantics, and its field-to-SVCv4-code mapping; this document explains the design
choices behind those contracts without restating their field lists.

## Background

### Vocabulary

SVCv4 brings its own terms; each is used throughout and defined once here.

| Term                            | Meaning                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **SVCv4**                       | ACMG/AMP/CAP/ClinGen Sequence Variant Classification v4 — the points-based successor to ACMG 2015 (`ACMG (V4)` in the glossary).                                                                                                                                                                                                                                                     |
| **SM*n***                       | SVCv4 Supplementary Material *n*: the per-topic normative supplements specifying the framework. The supplements referenced here are SM3 (frequency and the DAFT), SM4 (clinical observations), SM5 (segregation), SM6 (in-silico prediction), SM18 (mechanism and exon relevance), SM19 (informative variants), SM20 (functional assays), and SM21 (the MDE and multiple disorders). |
| **VBC**                         | The variant being classified.                                                                                                                                                                                                                                                                                                                                                        |
| **MDE**                         | Monogenic disease entity: gene × disease × mode of inheritance; a different mechanism comes as a different disease. The unit SVCv4 classifies is the MDE rather than the gene.                                                                                                                                                                                                       |
| **Evidence code**               | One scored line of evidence, named by family: `POP_FRQ` / `POP_HMZ` (frequency), `*_PRD` (in-silico prediction), `*_INF` (other classified variants at the same codon or exon), `*_FXN` (functional assays), `CLN_*` (clinical), `LOC_*` (phenotype and segregation).                                                                                                                |
| **P/LP, B/LB**                  | Pathogenic or likely pathogenic; benign or likely benign — the classification pairs ClinVar records are grouped by.                                                                                                                                                                                                                                                                  |
| **NMD / NSD**                   | Nonsense-mediated and non-stop decay: the pathways that decide whether a premature stop codon, or a missing one, leaves any protein. A **PTC** is a premature termination codon.                                                                                                                                                                                                     |
| **MAVE**                        | Multiplexed assay of variant effect — a deep mutational scan, the assay class MaveDB deposits.                                                                                                                                                                                                                                                                                       |
| **The matrix**                  | A molecular-mechanism × exon-relevance multiplier (`×1.0` / `0.5` / `0.25` / `0`) applied to *positive* predictive points.                                                                                                                                                                                                                                                           |
| **All / Most / Few**            | SM18's three exon-relevance tiers on the matrix's exon axis: how many of the gene's transcripts carry the assessed exon, and how abundant those transcripts are. SM18's *admission filter* names which transcripts count toward a tier before the tier is read.                                                                                                                      |
| **GDV gate**                    | The gene-disease-validity gate: the curated validity level of the MDE caps the final class and gates the mechanism multiplier.                                                                                                                                                                                                                                                       |
| **DAFT**                        | Disease allele frequency threshold: the frequency above which a variant is too common for the disease, and the comparator `POP_FRQ` is scored against.                                                                                                                                                                                                                               |
| **OddsPath**                    | The likelihood ratio a calibrated functional assay yields, which `*_FXN` reads.                                                                                                                                                                                                                                                                                                      |
| **VCEP**                        | A ClinGen Variant Curation Expert Panel, which may publish its own gene-specific criteria specification.                                                                                                                                                                                                                                                                             |
| **Gold stars**                  | ClinVar's 0–4 review-status scale for how a record's classification was reviewed.                                                                                                                                                                                                                                                                                                    |
| **MANE Select / Plus Clinical** | The RefSeq/Ensembl-agreed representative transcript of a gene, and the additional clinically used transcripts.                                                                                                                                                                                                                                                                       |
| **pext**                        | gnomAD's proportion-expressed-across-transcripts: how much of a gene's expression includes a given base.                                                                                                                                                                                                                                                                             |
| **Splice colour**               | The trichotomy a splice prediction routes on — likely, indeterminate, or unlikely disruptive — which each workflow prices per colour.                                                                                                                                                                                                                                                |
| **SVCv4 corpus**                | Our private checkout of the pilot's documentation set: the supplements as received, the practice variants, and our transcriptions of both. The reference's citations and the reference set point into it. Distinct from the literature corpus that the `literature` interface serves.                                                                                                |
| **Reference set**               | The variants curators have classified on the curation worksheet, which the run-review loop reads runs against ([`run-review-loop.md`](run-review-loop.md)).                                                                                                                                                                                                                          |

### SVCv4's shape drives the tool set

Under points-based ACMG, per-code points sum to a pathogenicity class, modified by two structural controls. First,
positive predictive points are scaled by the matrix, so a mechanism call and an exon-relevance call multiply into most
positive paths. Second, the GDV gate caps the resulting class and gates the mechanism multiplier itself. Each variant
type has its own workflow, but the workflows are assembled from the same recurring evidence modules — frequency,
in-silico prediction, other classified variants, functional assays, clinical observations, and phenotype and segregation
— so each module is built once and routed to by molecular consequence. These interfaces supply the public,
non-literature, non-case inputs to those modules.

### The sandbox boundary fixes where retrieval and arithmetic can live

Untrusted agent code runs inside bubblewrap with an empty network namespace, fail-closed, and reaches trusted services
only through a method-allowlisted gRPC hatch
([`sandbox-worker.md`](sandbox-worker.md#the-hatch-is-the-capability-boundary)). Trusted services egress to the internet
normally. Because the agent cannot egress at all, any retrieval that needs the open internet must run service-side;
conversely, deterministic arithmetic must ship into the sandbox image so it can run offline inside the guest. Neither
constraint requires mirroring external databases locally.

### Scale: one variant at a time

The Spike targets curator-in-the-loop, single-variant evaluation rather than VCF-wide batch processing
([`../PRODUCT.md`](../PRODUCT.md) §12). Classifying one variant takes a couple of dozen public REST calls. Every
per-variant upstream is therefore wrapped live and stamped with its retrieval time. Local mirrors are the at-scale form
of the same design, staged behind explicit volume and completeness triggers
([Alternatives considered](#alternatives-considered)).

## Non-goals

- **Literature-derived evidence.** Functional assays reported only in papers, clinical narrative, and de-novo and
  segregation observations from case reports belong to the `literature` interface, which owns find → fetch → distil over
  papers ([`literature-evidence-layer.md`](literature-evidence-layer.md)).
- **Case and patient-data evidence as a service concern.** The `CLN_*` and `LOC_*` codes come from patient and pedigree
  data that no public database holds, so no RPC retrieves them. The in-sandbox library still scores them from the
  analyst's categorical reading ([below](#the-library-takes-categorical-judgements-and-returns-derivations)). Beyond the
  public Spike, and gated tighter.
- **The SVCv4 scoring and combining arithmetic as an RPC.** Deterministic scoring lives in the in-sandbox library
  [`themis.svcv4`](#the-in-sandbox-library-themissvcv4).
- **CNV and dosage classification.** Copy-number variants follow a separate ACMG framework; multi-gene events route out.
- **Evidence SVCv4 does not ask for.** No 3D-structure or PDB retrieval is included because the framework never cites
  protein structure, and critical-residue evidence comes from UniProt domains plus the calibrated predictor. No
  standalone Grantham signal is exposed because Grantham distance appears only as the missense same-codon comparator
  inside the informative-variant rules. Criteria dropped in v4 — `PP2`/`BP1` (subsumed into the calibrated predictor),
  `PP5`/`BP6` (assertion-based, discontinued), and standalone `PM2` rarity — do not re-enter through an interface.
- **Bulk annotation.** No VCF-wide batch runs are supported here; the standard pipeline produces baseline annotations
  upstream ([`../PRODUCT.md`](../PRODUCT.md) §6).
- **Hosting a prediction model on this deployment.** Predictors are queried live; a self-hosted splice model or
  `ensembl-vep` brings reference data and compute that belong on a separate deployment
  ([below](#one-interface-per-source-all-on-the-one-evidence-deployment)).

## Design

### Four roles: retrieve, remap, compute, judge

A frontier model serves as the analyst: everywhere SVCv4 hands a decision to "an experienced analyst", the model makes
that call under curator steering ([`../PRODUCT.md`](../PRODUCT.md) §4, §6). The interfaces and the library feed and
support that judgement across four distinct roles:

- **Retrieve** — an interface returns the upstream payload largely as-is, attaches provenance, and documents in the
  proto which SVCv4 codes each field feeds. Because the model parses raw JSON reliably, the service avoids remodelling
  fields that nothing downstream branches on.
- **Remap** — an interface returns typed values derived by the service across upstream conventions: cross-build and
  transcript projection, reducing each splice predictor's per-transcript deltas onto a shared sign orientation,
  resolving ClinVar's review-status phrase to gold stars, and keying MaveDB on the variant's ClinGen allele. Moving this
  wrangling into service code keeps the model from doing coordinate or vocabulary conversions by hand.
- **Compute** — `themis.svcv4` runs deterministic rule arithmetic offline inside the sandbox so the transparent point
  tally, derivation lines, and class band are reproducible and auditable — shown to the curator, not load-bearing for
  the clinical conclusion ([`../PRODUCT.md`](../PRODUCT.md) §6).
- **Judge** — the model makes every analyst call SVCv4 defines, with explicit reasoning and uncertainty that the curator
  can inspect and override:
  - choosing the MDE when the request names none, and matching which curated source record describes that MDE;
  - resolving ambiguous consequence routing;
  - scoring the mechanism level;
  - assigning the `All / Most / Few` exon-relevance tier;
  - deciding which classified variants carry distinct, non-circular evidence;
  - evaluating whether a functional assay measures the disease-relevant function and agrees with the mechanism;
  - judging the splice product's consequence;
  - choosing DAFT parameters when no curated value exists;
  - making critical-residue determinations and multiple-disorder aggregations; and
  - authoring the final holistic verdict reasoned over the claims rather than treating the rolled-up point total as the
    argument.

Most RPCs combine retrieval and remapping, and whether a field is a verbatim copy or a derived value is documented per
field in the proto.

Where choosing a parameter *after* seeing the variant's data would constitute multiple testing, the choice is fixed in
advance as policy rather than left as a per-variant judgement. In particular, predictor selection is frozen per-gene
policy, and so are two frequency choices; both are documented in [`svcv4-interpretations.md`](svcv4-interpretations.md).
Where a required judgement input remains unsettled, the framework's own disposition applies and the run reports no
class.

### One interface per source, all on the one `evidence` deployment

Nine gRPC services — one per curated source — attach to the single `evidence` server beside `literature`
([`services.md`](services.md#one-deployment-several-interfaces)). Each interface is scoped to a source because the
source is what a contract is written against: an interface's `.proto` states the identifier forms, absence semantics,
and rate limits a caller needs to read the answer, and evolves when its upstream evolves. An interface may consult a
helper upstream (such as a crosswalk registry or projection engine) behind that contract without changing whose
vocabulary it speaks. The four interfaces whose answer no single vendor owns (`variant`, `transcript`, `splice`,
`gene_disease`) are named for the domain question instead ([Alternatives considered](#alternatives-considered)). All
nine interfaces share one deployment, container image, session resolver, and outbound HTTP client pool.

Sharing a deployment has two operational consequences on this surface ([`services.md`](services.md)):

- **The sandbox agent reaches these RPCs through a narrower gate than other callers.** Cloud Run IAM is granted per
  deployment, so an authenticated backend service can call all twelve RPCs. The sandbox agent instead reaches only the
  methods listed on the hatch's allowlist, controlled per RPC by the `agent_exposed` option on its proto declaration
  ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)).
- **`gene_disease` is a startup dependency of the entire process.** Request failures isolate per RPC, but startup is
  shared. `GeneDisease.DescribeGene` loads four reference tables from GCS before serving; if that bucket cannot be read,
  the container refuses to start — which is the intended behaviour, not a defect in the startup probe. Starting with an
  unreadable reference store would answer "absent" for every gene-disease query, and an absence there is scored
  evidence.

A source moves to its own deployment only when it brings dedicated model weights, large reference data, or heavy compute
— such as self-hosting a splice predictor, a local dbNSFP query service, or `ensembl-vep`.

### The rpc surface

The nine interfaces expose twelve RPCs ([`themis/services/evidence/`](../../themis/services/evidence/)):

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
[`mavedb`](../../schema/proto/themis/rpc/mavedb.proto), [`cspec`](../../schema/proto/themis/rpc/cspec.proto).
Authentication follows the standard service pattern: the session token arrives as gRPC request metadata and never as a
message field ([`services.md`](services.md), [`security.md`](security.md)).

Three cross-surface properties shape how callers use these twelve RPCs:

- **VEP acts as a consolidator.** One `Vep.Annotate` call returns molecular consequence, per-transcript HGVS, MANE
  flags, six of the seven SVCv4-calibrated missense predictors, SpliceAI deltas, and an inline ClinVar/gnomAD snapshot,
  so several evidence modules read from one RPC and no predictor is self-hosted. The seventh calibrated predictor is not
  served by the public Ensembl endpoint, which matters only if a future gene-specific policy commits to it.
- **`Variant.Normalize` is the spine, and it accepts RefSeq accessions only.** VariantValidator serves RefSeq accessions
  and rejects Ensembl accessions, which is also why the exon-table and splice-outcome RPCs take RefSeq coding
  accessions. Accepting other identifier forms a caller may hold (VCF locus, gnomAD ID, rsID, CAID, protein HGVS) is
  deferred: each requires a different Allele Registry endpoint, a VCF locus also requires a chromosome-accession table,
  and protein HGVS has no Allele Registry route. Adding those input forms belongs in `Variant.Normalize` with its own
  provenance rather than in per-RPC format sniffers.
- **Each RPC's accepted identifier form matches its upstream's native key.** The frequency and splice-delta RPCs take a
  positional ID (`14-23424860-G-A`); the normalisation, ClinVar, and VEP RPCs take HGVS. Every rejected form has an
  explicit reason in its contract so the caller gets an actionable error message.

### Goal: place the variant, and route it to a decision tree

Before any evidence is scored, all twelve retrievals must refer to the exact same allele. SVCv4 selects a workflow by
molecular consequence, while each downstream source keys variants differently: a positional ID at gnomAD and the splice
hosts, transcript HGVS at ClinVar and VEP, an HGNC ID at `gene_disease`, and a ClinGen allele ID at MaveDB.

**RPCs.** `Variant.Normalize` is the spine ([the rpc surface](#the-rpc-surface)); `Vep.Annotate` supplies the routing
consequence; `Transcript.GetStructure` places the allele inside its transcript (exon membership, splice-site distances,
and each exon's skip frame).

**What the upstreams give, and what has to be remapped.** The ClinGen Allele Registry canonicalises the allele and holds
its crosswalks; VariantValidator validates the HGVS, projects it onto MANE Select, and yields VCF loci on GRCh37 and
GRCh38; VEP states the molecular consequence. Because none of the three returns the join keys in the exact form
downstream RPCs require, `Variant.Normalize` derives them:

- the canonical allele ID (`CA*`) is parsed out of the Allele Registry URL;
- the positional IDs are assembled from VariantValidator's VCF components;
- transcript projections from both sources are deduplicated on the unversioned accession and stamped per element with
  which source stated them; and
- the consequence term is mapped onto the routing enum that selects the SVCv4 workflow.

`Variant.Normalize` also surfaces the Allele Registry's **ClinVar crosswalk as a typed message** (variation ID, RCV
accessions, ClinVar allele ID, and preferred name) because `ClinVar.DescribeVariant` fetches the variant's record by
that VCV accession, and an empty crosswalk on a resolved allele is how a run establishes that the allele is novel
([below](#goal-what-else-has-been-classified-at-this-codon-this-exon-and-across-the-gene)).

**Worked example.** Instructing an agent to "take the first MANE Select transcript" from the Allele Registry response
fails: the registry lists a MANE pair once per accession namespace and flags both, with the Ensembl `ENST` entry first.
Selecting by flag and position yields an `ENST` accession, which the exon-table, exon-relevance, and splice-outcome RPCs
all reject. Stamping the namespace on each projection lets the caller filter by both MANE flag and `RefSeq` namespace.

**Caveats.** The projection list on `Variant.Normalize` covers only transcripts that *this allele* overlaps; transcripts
the allele does not touch are absent, making it the wrong denominator for exon inclusion across the gene
([`Transcript.AssessExonRelevance`](#goal-how-relevant-is-the-exon-the-variant-lands-in) owns the gene-wide transcript
inventory). When a variant arrives pre-annotated by the standard pipeline ([`../PRODUCT.md`](../PRODUCT.md) §6), a
caller can pass those keys straight to the fact RPCs without calling `Variant.Normalize` first.

### Goal: is the variant too common for the disease?

`POP_FRQ` and `POP_HMZ` compare the variant's population frequency against a disease-specific threshold (the DAFT), so
this goal requires an allele frequency, the quality verdict behind it, and homozygote counts — and it requires database
absence to be a usable finding, since absence from gnomAD is the rarity input.

**RPCs.** `Gnomad.DescribeVariant`, taking the positional ID produced by `Variant.Normalize`.

**What the upstream gives, and what has to be remapped.** gnomAD's GraphQL API serves per-dataset (`exome`, `genome`)
and `joint` frequency blocks, filtering allele frequencies (`faf95`) maximised over genetic-ancestry groups, homozygote
counts, per-dataset variant-QC verdicts, caveat flags, and coverage and constraint context. The service passes the
frequency payload through largely intact for `themis.svcv4` to read at `frequency.faf_from_gnomad` and
`homozygotes_from_gnomad` (while `Transcript.AssessExonRelevance` consumes the constraint and coverage tracks). What the
service adds is three boundary checks: holding the variant ID to the stricter of the spellings its consumers accept,
checking the requested dataset against the releases the RPC serves, and stamping every release the answer rests on.
Which frequency figure `POP_FRQ` is scored against, and what disqualifies one, is defined in
[`svcv4-interpretations.md`](svcv4-interpretations.md#frequency-which-figure-and-what-disqualifies-one); the RPC returns
both the `joint` figure and each dataset's QC verdict so `themis.svcv4` has both inputs.

**Worked example.** gnomAD answers a variant ID it cannot parse with **HTTP 200 carrying a null variant**, identical to
a genuine absence. Without an input shape precondition, a mistyped ID such as `17-4932566-A-T` is scored as "absent from
gnomAD" — manufacturing benign/pathogenic rarity evidence from a typo and never retrying because absence is a settled
answer ([see failure-mode rules](#where-absence-is-scored-a-shape-precondition-is-required-not-optional)).

**Caveats.** A `NOT_FOUND` response from `Gnomad.DescribeVariant` carries no coverage distinction: "not observed at a
well-covered position" and "position not covered" return the same status, even though rarity evidence depends on the
difference ([Open questions](#open-questions)). In addition, gnomAD's rate limit is enforced per IP address, so
concurrent sessions behind one egress IP share the budget.

**Moving off the upstream.** A local gnomAD mirror is the at-scale form of this RPC; the triggers that flip it are in
[Alternatives considered](#scale-and-framework-encoding).

### Goal: what does a calibrated predictor say about the substitution?

The `*_PRD` codes score one calibrated predictor's output for the substitution against that predictor's published
thresholds in SM6. The engineering problem is obtaining *exactly one* score from the predictor that policy selected in
advance for the gene, without exposing the remaining predictors as a menu to cherry-pick from.

**RPCs.** `Vep.Annotate`, the same call that supplies the routing consequence
([above](#goal-place-the-variant-and-route-it-to-a-decision-tree)) and inline SpliceAI deltas.

**What the upstream gives, and what has to be remapped.** One VEP call carries per-transcript HGVS, exon numbers, MANE
flags, calibrated missense predictors, splice deltas, and a colocated ClinVar/gnomAD snapshot — provided the request
explicitly enables those flags on every call. Predictor scores arrive in two different wire formats: some are
first-class VEP fields, while others come through VEP's dbNSFP plugin as per-transcript dot-separated strings that the
service resolves to a single float per transcript (raising an error if a string names multiple conflicting values for
the same transcript). The RPC also pins GRCh38, because MANE and AlphaMissense are GRCh38-only, and rejects references
that omit an assembly rather than assuming the build.

Which predictor governs a given gene is frozen per-gene policy shipped as versioned data inside `themis.svcv4` and read
before the call
([`svcv4-interpretations.md`](svcv4-interpretations.md#in-silico-prediction-one-predictor-chosen-in-advance)).
`Vep.Annotate` serves any predictor on its allowlist, while **`themis.svcv4` enforces the single-predictor guarantee**:
its score-to-bin function scores only the predictor named by the gene's policy, reports the policy entry that selected
it, and raises on a missing or malformed gene ID. If a caller requests a second predictor on `Vep.Annotate`, its score
is returned alongside the first only as data for evaluating a future policy revision.

**Worked example.** Ensembl VEP silently ignores any request flag it does not recognise: asking for `BayesDel` under a
misspelled flag returns HTTP 200 with the score omitted, indistinguishable from the variant having no score. The RPC
therefore validates requested predictor names against a closed allowlist and rejects unknown names with
`INVALID_ARGUMENT`.

The same silent-omission hazard motivates returning each score as a typed `PredictorScore` message rather than passing
through VEP's raw JSON keys. VEP's request flag and response key spellings differ across plugins: requesting the dbNSFP
column `BayesDel_noAF_score` returns a field named `bayesdel_noaf_score`, whereas AlphaMissense returns a nested object
at `alphamissense.am_pathogenicity`. A caller looking up the request's spelling on a real response would find nothing
and report `MIS_PRD` as undetermined, while unit tests against a hand-written mock sharing the wrong key would pass. To
lock the mapping to reality, `Vep.Annotate` normalises every score onto the request's canonical predictor name and tests
that parser against a live-recorded VEP fixture checked in at
[`vep.json`](../../themis/services/evidence/upstreams/tests/fixtures/vep.json).

Attaching scores to a typed element per annotated transcript also separates two distinct absences: a transcript entry
with no `scores` element for a requested predictor means VEP had no score for it on that transcript, whereas a
transcript missing from `transcripts` altogether means VEP did not annotate that transcript. For
`NM_001042492.3:c.3496G>C` with `BayesDel` and `AlphaMissense` requested, VEP returns the MANE Select transcript as:

```json
{
  "transcript_id": "ENST00000358273.9",
  "mane_select": "NM_001042492.3",
  "bayesdel_noaf_score": 0.0531436,
  "alphamissense": {"am_class": "likely_pathogenic", "am_pathogenicity": 0.9467}
}
```

and `Vep.Annotate` remaps it to:

```textproto
transcripts {
  transcript_id: "ENST00000358273.9"
  mane_select: "NM_001042492.3"
  scores { predictor: "BayesDel" score: 0.0531436 }
  scores { predictor: "AlphaMissense" score: 0.9467 }
}
```

Because VEP annotates Ensembl transcripts only, each element carries its `mane_select` RefSeq partner so a caller
holding the MANE Select `NM_*` accession can locate its transcript. `Vep.Annotate` requests versioned transcript IDs
from VEP, and `themis.svcv4` matches an unversioned accession by base while requiring an exact version match when the
caller supplies a versioned accession, because a different version is a different transcript model.

**Caveats and moving off the upstream.** EMBL-EBI has shipped the final release of the public Ensembl REST endpoint,
freezing its Ensembl release, transcript set, MANE version, and dbNSFP branch. Responses remain reproducible and carry
their exact release stamps in `Provenance`, but annotations gradually drift from newer releases. When MANE updates a
gene to a newer RefSeq transcript version than the frozen VEP endpoint knows, `themis.svcv4`'s exact-version check
refuses to read the score off the older transcript model and reports the version VEP holds so the analyst can decide
whether to score against the older version explicitly. The long-term replacement for the frozen endpoint is a
self-hosted `ensembl-vep` deployment ([Alternatives considered](#scale-and-framework-encoding)).

#### Predictor data licensing binds on one trigger

Ensembl's public REST API serves the **commercial branch of dbNSFP** and passes licensing responsibility through to the
caller: dbNSFP is free for academic and non-profit research use and requires an annual commercial licence for use within
a for-profit organisation or in a monetised product. BayesDel itself states no licence beyond a citation request.

Our current evaluation use is academic / non-profit research, which the grant covers, and the service queries the public
API per variant without vendoring or redistributing dbNSFP data. **The trigger that requires licensing action is
commercialising or monetising the product**, at which point dbNSFP, REVEL, and ClinPred must be cleared or replaced
(Ensembl documents REVEL and ClinPred as non-commercial-use only; both return from the same VEP call, and only the
frozen per-gene policy currently keeps non-commercial predictors out of scored responses). If that trigger fires,
BayesDel's author distributes the no-allele-frequency build directly, avoiding the dbNSFP compilation licence at the
cost of hosting a periodic reference table two orders of magnitude larger than the gene-disease tables.

### Goal: does the variant disrupt splicing, and what does the product become?

Every variant workflow checks for a splice effect, splitting this goal in two: *whether* a splice site is gained or lost
(answered by two splice predictors with per-position deltas), and *what the transcript becomes* if an exon is skipped —
the fork between the null path (a premature termination codon and NMD) and the coding path (an in-frame deletion), which
splice predictors do not compute.

**RPCs.** `Splice.PredictDeltas` for the splice deltas and `Splice.PredictSkipOutcome` for the skipped-exon product
(`Vep.Annotate` also returns an inline SpliceAI snapshot, and `Transcript.GetStructure` supplies the exon table).

**What the upstreams give, and what has to be remapped.** Broad's SpliceAI and Pangolin hosts each return
per-transcript, per-position delta scores, but they use incompatible sign conventions: SpliceAI reports all four deltas
(`DS_AG`, `DS_AL`, `DS_DG`, `DS_DL`) as positive magnitudes, whereas Pangolin signs splice losses negative. Taking a raw
maximum across Pangolin's deltas hides a predicted loss behind any co-reported gain and makes the two predictors
incomparable. `Splice.PredictDeltas` therefore reduces each predictor to one maximum gain and one maximum loss on a
shared positive orientation across the scored transcripts, returning the `(gain, loss)` pair per predictor so
`themis.svcv4` can check concordance and bin the `SPL_PRD` trichotomy.

`Splice.PredictSkipOutcome` composes VariantValidator's exon structure with the RefSeq transcript sequence from NCBI
Nucleotide (`efetch`): it excises the named exon's coding span from the transcript sequence and translates the spliced
product so any premature stop codon (PTC) is read directly off the sequence rather than assumed from the frame shift. It
returns the frame shift, the aberrant product class, the PTC coordinate, and the 50-nt NMD determination computed over
the aberrant transcript's own exon junctions. Its contract states its scope explicitly: it computes the consequence *if*
the exon is skipped, not whether skipping occurs, and does not model intron retention.

**Worked example.** A 1 kb deletion produces a 1,019-character positional ID, and the Broad splice hosts take the ID in
a `GET` request line that fails with a bare HTTP 400 past ~8 kB. `Splice.PredictDeltas` therefore checks sequence length
separately from coordinate syntax and returns a distinct error message for oversize alleles, so a well-formed 1 kb
deletion is scored rather than rejected as a syntax error.

**Caveats.** Both Broad hosts report variant-level problems inside HTTP 200 payloads carrying an `"error"` string, so an
HTTP 4xx from them indicates a malformed request line or moved endpoint rather than an invalid caller variant. That
makes them exempt from the general 4xx-to-`INVALID_ARGUMENT` rule and requires the input shape precondition on
`PredictDeltas` ([see failure-mode rules](#the-contracts-are-written-against-the-upstreams-failure-modes)). Like gnomAD,
their rate limits are enforced per client IP.

**Moving off the upstream.** Self-hosting SpliceAI and Pangolin requires model weights and GPU/CPU compute, making it a
separate deployment ([Non-goals](#non-goals)) until rate limits or latency force the move.

### Goal: what else has been classified at this codon, this exon and across the gene?

ClinVar classifications feed three distinct SVCv4 steps: the `*_INF` codes score other classified variants at the same
codon or exon (SM19), SM18's exon-relevance signal reads the gene's pathogenic-variant density, and SM3's fourth DAFT
rung reads the highest-frequency known pathogenic variant in the gene. A single ClinVar query cannot serve all three.

**RPCs.** `ClinVar.DescribeVariant` returns the queried allele's own VCV record plus the **gene's pathogenic pool**;
`ClinVar.SearchCodingSpan` returns **every** classification (including Benign, Likely Benign, and VUS) within a coding
base span. (`Transcript.AssessExonRelevance` counts its own density pool at a fixed review-status floor; see
[below](#goal-how-relevant-is-the-exon-the-variant-lands-in).)

**Why two RPCs are required.** The gene-wide pool on `DescribeVariant` is restricted to pathogenic and likely pathogenic
records by its search term, which fits the pathogenic arm of `*_INF`, SM18's P/LP density, and the DAFT. However, every
`*_INF` rule in SM19 also checks for benign, likely benign, or VUS variants at the same codon or exon. Reading that
second check off a P/LP-only gene pool silently misses benign and VUS neighbours: in our evaluation corpus, doing so
went wrong on **four of seven cases**, including one run that reported an `*_INF` code as not-determined after checking
only the pathogenic pool. Conversely, fetching all classifications across an entire gene is both unnecessary (no SVCv4
rule asks for gene-wide benign/VUS records) and unfetchable on large genes where truncation would cut off the pool
before reaching the target codon. `ClinVar.SearchCodingSpan` therefore scopes its all-classification query to the
specific codon or exon coding span that SM19 keys on.

**What the upstream gives, and what has to be remapped.** NCBI's E-utilities expose text search (`esearch`), bulk
summaries (`esummary`), and full XML record fetch (`efetch`). Four remapping steps sit on top of those endpoints:

- resolving ClinVar's review-status *phrase* to its 0–4 gold-star count;
- parsing each record's HGVS title into coding base coordinates (`c.`), so placing records against an exon table is
  coordinate arithmetic rather than regex matching over display strings;
- constructing the pool's query as an Entrez *property* term rather than a field term; and
- returning the census bounding the pool as typed state on the response.

Constructing the search term as an Entrez property term is critical because Entrez does not reject unknown field tags: a
pathogenic-significance filter written as an Entrez field tag is silently reinterpreted as a free-text query, matching
1,452 records on one test gene (of which a sampled page was 60% *Uncertain significance*) where the valid property term
matched 465 records. The service therefore issues the verified property term, echoes it verbatim in the response census,
and re-checks every returned record's classification term by term after retrieval. Which ClinVar classification strings
count as a pathogenic assertion — and why the DAFT filters the same pool through a narrower gate — is documented in
[`svcv4-interpretations.md`](svcv4-interpretations.md#which-clinvar-classifications-count-as-a-pathogenic-assertion).

**A bounded pool is typed, not caveated in prose.** Because a well-studied gene can carry thousands of P/LP records, the
pool returned by a single call may be a prefix and its count a lower bound. The response therefore carries a typed
census (`search_term`, `total_count`, `fetched_count`, and whether the list is truncated) so the caller can branch on
truncation and verify the exact six-token membership disjunction and star floor that defined the pool. Raw `esummary`
payloads (~2.3 kB per record) are omitted in favour of typed fields on each record message.

**Records whose coordinates cannot be placed are listed in `unparsed`, never dropped.** Copy-number records,
uncertain-breakpoint titles, and **short-form repeat notation** (`c.571ATG[1]`, whose base extent cannot be derived from
the title string without the reference sequence) carry no parseable coding span. Every such record is returned in the
response's `unparsed` list rather than dropped, because silently dropping an unplaced record understates the pathogenic
count of the exon it belongs to.

**Disagreement on the gene symbol fails the RPC.** An empty span census and a gene symbol that ClinVar does not index
produce the exact same empty `esearch` result, and an empty span census is a scored negative finding for `*_INF`. When
the annotation sources disagree on the gene symbol, `ClinVar.SearchCodingSpan` fails with `FAILED_PRECONDITION` so an
internal symbol mismatch is never scored as absence of informative variants.

**The queried allele's own record is fetched by identity, not by HGVS string.** ClinVar indexes an allele only under the
text expressions its submitters and curated expression list happen to carry, which often omit the caller's transcript
version: for example, `HOXA13` `NM_000522.5:c.396_398del` is a one-star record classified by two laboratories, yet a
ClinVar search for that exact `.5` string matches nothing because ClinVar indexes the allele under `.4` and under a
repeat-notation title. A string search miss read as "novel allele" would make a false scored claim about an allele two
laboratories have already classified.

Instead, `Variant.Normalize` returns the ClinGen Allele Registry's ClinVar crosswalk (variation ID, RCV accessions,
ClinVar allele ID, and preferred name), and `ClinVar.DescribeVariant` takes the `VCV*` accession from that crosswalk and
fetches the record directly:

- **An empty crosswalk on a resolved allele is the novel-allele finding**: the Allele Registry resolved the canonical
  allele and links it to no ClinVar variation.
- **A VCV accession named by the registry that ClinVar will not serve fails the RPC**: two upstreams disagree about
  whether a record exists, so `ClinVar.DescribeVariant` raises `FAILED_PRECONDITION` rather than returning an absence.

> **The novelty finding is bounded by the crosswalk's release.** Because the Allele Registry's crosswalk trails
> ClinVar's own releases slightly, a variant classified after the last crosswalk ingest reads as novel until the next
> sync. On a 30-record `MYH7` P/LP sample, 1 of 26 precise alleles sat inside that window (`VCV004884318`, `MYH7`
> `c.1543A>G`, single submitter, evaluated 2026-08-04, had a registered allele with no ClinVar link two weeks later). A
> novel-allele finding therefore means "no ClinVar variation as of the crosswalk release named in `Provenance`," a
> window that closes once the dump-backed index below replaces the live query API.

When the VCV record is returned, `ClinVar.DescribeVariant` includes the review-status *phrase* alongside the gold-star
count, because an expert-panel classification is adopted based on which panel reviewed it rather than on the star count
alone
([`svcv4-interpretations.md`](svcv4-interpretations.md#which-clinvar-classifications-count-as-a-pathogenic-assertion)).

**Moving off the upstream to a dump-backed index.** Querying a bulk dataset through a rate-limited text-search API is
the single root cause behind pool truncation, search-term fragility, missing per-submission detail on pool records (bulk
`esummary` carries none), unplaceable repeat-notation titles, and E-utilities rate limits. Ingesting ClinVar's **weekly
VCV release** and keying it on canonical SPDI resolves all five issues at once: the pool becomes a local index query,
the membership gate becomes a predicate rather than a search string, per-submission detail is available for every record
in the pool, repeat-notation titles are normalized against the reference sequence at ingest time, and the allele-to-VCV
mapping is refreshed on our own schedule. The trade-off is operating the weekly ingest and bounding freshness by the
last ingest timestamp in `Provenance`. Living with the query API is what is implemented now; the triggers that pull the
dump ingest forward are per-IP E-utilities rate limits binding at multi-session scale, an `*_INF` completeness
requirement that a bounded span search cannot meet, a need for pool-wide submission detail, or novelty findings going
stale often enough on recently classified variants to matter.

### Goal: is the gene-disease relationship valid, and by what mechanism?

Two of SVCv4's structural controls come from the gene and disease rather than the variant: the curated **validity**
level of the MDE caps the pathogenicity class and gates the mechanism multiplier (SM21), and the **mechanism** level
scales positive predictive points (SM18). Both are defined per MDE — gene × disease × mode of inheritance — and never
per gene, which shapes `GeneDisease.DescribeGene`.

**RPCs.** `GeneDisease.DescribeGene`.

**What the upstreams give, and what has to be remapped.** Unlike the variant-level goals, gene-disease validity has no
per-variant API to wrap. Its four sources are gene-level datasets — ClinGen gene-validity, ClinGen dosage sensitivity,
GenCC submissions, and a PanelApp Australia dump — so the interface loads all four at startup from a GCS bucket and
answers every lookup as an in-memory join. **Every table is keyed by HGNC ID**, which remains stable across gene symbol
retirements and reassignments; the request takes the HGNC ID alone and carries no symbol field that could drift from it.
(ClinVar remains symbol-keyed because its upstream API searches by symbol.)

**Loading from a GCS bucket rather than downloading from upstreams at startup keeps a scale-to-zero service viable.**
GenCC's download endpoint caps near twenty requests a day, which cold-start churn would quickly exhaust. A weekly
refresh Cloud Run Job is therefore the only process that touches those upstream hosts, and the `evidence` service reads
the objects it writes to GCS.

Three tables are stored in GCS **verbatim**, fetched with conditional `GET` requests so unchanged files are not
rewritten. The PanelApp Australia dump is **transformed**: the Mendeliome and Incidentalome panels (the two carrying the
diagnostic long tail) are aggregated per HGNC ID into the maximum confidence across the two panels, the mode of
inheritance of the highest-confidence entry, the gain-of-function veto flag, and the deduplicated per-gene evaluation
comments. Because PanelApp publishes no entity tag, its summary object is rebuilt on each weekly run.

The RPC's one live network dependency is the MONDO subclass closure at EBI OLS4, reached only when a request names a
disease term that the gene is not directly curated for. Curators routinely curate a numbered subtype of the broader
disease term named in a referral, so exact identifier equality under-matches, and the ontology's subclass relation
settles whether one term is a subtype of another without fragile label comparisons.

**The response shape follows from "per MDE":**

- **Records are never reduced across entities, and no entity is pre-selected.** Deciding which MDE a clinical
  presentation belongs to is an analytical judgement over the full clinical picture (SM21); a gene-wide maximum would
  always take the strongest curation on the gene and discard the class cap imposed by the chosen entity.
- **Within one source's record for one entity, the service does reduce.** Several submitters file assertions in GenCC
  about the same entity, and aggregating those assertions under a published vocabulary is deterministic bookkeeping
  rather than clinical judgement.
- **Each source's mode of inheritance is returned verbatim in the source's own words.** See below for why the service
  does not harmonise inheritance modes across sources.
- **A named disease term that the curations do not settle raises an error rather than resolving to the nearest match.**
  The RPC returns every curated record on the requested MONDO term across all modes and sources, or — when the gene is
  not curated directly on that term — every record on the *single* curated term that is a MONDO subclass of it. Where
  several distinct curated subtype terms sit under the requested term, or none does, choosing among them belongs to the
  caller, and the RPC fails with `FAILED_PRECONDITION` rather than picking.
- **The classification-to-gate-level translation lives in the library and response contract, not in caller prose.**
  Curators publish *classifications* (`Definitive`, `Strong`, `Moderate`, `Limited`, `Disputed`, `Refuted`,
  `Supportive`, `No Known Disease Relationship`), whereas the GDV gate is keyed by *levels*, and several published
  classifications name no level directly. One canonical map holds the vocabulary, strength rank, and `GateLevel`
  together across both `GeneDisease.DescribeGene` and `themis.svcv4`.

**Why the sources' inheritance modes stay verbatim.** An obvious design maps each source's mode term onto one shared
enum so a caller can ask for "the autosomal dominant entity" and let service code select the matching record. That
mapping breaks because the two sources that curate entities do not share a vocabulary or granularity: ClinGen publishes
a two-letter code and GenCC publishes an HPO term. GenCC distinguishes X-linked dominant from X-linked recessive (which
SVCv4 routes to different rules), whereas ClinGen curates a single undifferentiated X-linked mode (`XL`), so a shared
enum must either drop GenCC's distinction or invent one for ClinGen. Moreover, any term outside the enum has nowhere to
go: GenCC files a digenic assertion (`HP:0010984`) against `CLCNKB`, and a strict mapping that rejects unknown terms
would fail every lookup of that gene.

Downstream consumers do not need a shared mode enum on the RPC response. The GDV gate reads a `GateLevel` from the
record the analyst matched to the MDE, while `themis.svcv4`'s frequency and clinical observation functions route on the
framework's own five modes stated in the library's vocabulary
([`curation-surface.md`](curation-surface.md#one-vocabulary-per-concept)). Each curated record therefore carries its
source's mode term verbatim, keyed by `(source, disease_term, source_mode_term)`, and a request filters by disease term
alone. For a disease curated by ClinGen as autosomal dominant and by GenCC as autosomal recessive, a request naming its
MONDO term receives both `(ClinGen, MONDO:…, "AD")` and `(GenCC, MONDO:…, "HP:0000007")`, and the model matches the
appropriate record against the MDE's mode (SM21).

The cost of leaving mode unharmonised is that service code no longer verifies that the record chosen by the analyst
matches the MDE's mode of inheritance: a run classifying an autosomal recessive entity off the gate level of the same
disease's autosomal dominant record receives a valid `GateLevel`. To keep that choice auditable, the record's verbatim
mode term sits beside its `GateLevel` in the response and the run states the mode it classified under, so a reviewer can
check the two against each other. (PanelApp asserts panel membership rather than MDE validity, so it contributes no
entity record.)

**Mechanism signals sit in a gene-scoped block**, separate from the per-entity records, because they are curated per
gene: ClinGen's dosage-sensitivity haploinsufficiency score and PanelApp's flags describe the gene as a whole rather
than a specific MDE. On a gene carrying both a loss-of-function and a gain-of-function entity (SM21), attaching a
gene-level haploinsufficiency score to the chosen entity would falsely scale the multiplier on the gain-of-function
path. How an analyst scores the mechanism rubric from those signals — and why `Uncertain` is a floor and never a default
— is documented in
[`svcv4-interpretations.md`](svcv4-interpretations.md#mechanism-a-rubric-an-analyst-scores-not-a-field-to-retrieve).

**Worked example.** In the July 2026 ClinGen snapshot, 492 of 3,017 genes carry more than one curated entity (315 of
them at more than one validity classification), GenCC's submitters disagree on 5,319 of 15,464 entities, and 1,185
`(gene, disease_term)` pairs are curated under more than one inheritance mode. A gene-wide maximum would collapse every
one of those to the strongest classification on file, whereas a lookup by disease term returns each pair's records under
every mode so the analyst can select the record matching the MDE.

**Moving off the upstreams.** The live MONDO closure call to EBI OLS4 could be replaced by a precomputed closure shipped
with the GCS reference tables, at the cost of a fifth required startup object
([Alternatives considered](#clinvar-and-gene-disease-scoping)).

### Goal: how relevant is the exon the variant lands in?

The matrix's second axis is SM18's `All / Most / Few` exon-relevance tier. Here the design question is not how to fetch
the inputs, but what a retrieval RPC may *conclude*: the tier scales every positive predictive path, and SM18 calibrates
neither half of its own definition.

**RPCs.** `Transcript.AssessExonRelevance`, built over the exon table returned by `Transcript.GetStructure`.

**What the upstreams give, and what has to be remapped.** Four sources compose in this RPC: VariantValidator's
per-transcript exon spans, GTEx isoform expression, gnomAD constraint and per-base pext, and ClinVar's
pathogenic-variant density. Remapping happens where they meet: pext is computed by weighting gnomAD's per-region values
over VariantValidator's exon spans, and the transcript inclusion inventory is an interval test run **natively in each
annotation namespace** (RefSeq and Ensembl), so no accession crosswalk is needed to answer the structural inclusion
question. gnomAD's MANE Select pair is the one RefSeq/Ensembl pairing published by an upstream here, and because it can
differ from the release flagged by VariantValidator's exon table, the RPC returns the pair explicitly rather than
assuming they match.

**What the RPC returns, and what it withholds.** `AssessExonRelevance` returns MANE membership, a gene-wide transcript
inventory against the assessed exon (per transcript, in both RefSeq and Ensembl annotation sets, using a four-outcome
inclusion verdict rather than a binary carries/omits boolean), GTEx expression, constraint, a per-exon **pext profile**
rather than a single scalar, ClinVar P/LP density, and **no `All / Most / Few` tier**. Computing transcript inclusion is
a structural fact the RPC owes; choosing the tier is the uncalibrated clinical judgement it must withhold
([Alternatives considered](#exon-relevance)). Why SM18's tier boundary is uncalibrated, why pext alone cannot establish
`All`, and what an empty omitting-transcript set does and does not license are detailed in
[`svcv4-interpretations.md`](svcv4-interpretations.md#exon-relevance-what-the-tiers-admit).

**Unreadable annotation records are named, neither raised on nor dropped.** Failing the whole RPC on one unreadable
transcript annotation record would take pext, constraint, density, and expression down with the inventory, while
silently dropping the record would let an unparsed transcript pass for one that lacks the exon.

**Worked example.** On the largest measured gene, the two gene-wide transcript payloads run to 1.26 MB against a 60 s
per-request upstream timeout; issuing them concurrently with the assessed transcript's own fetch keeps the inventory
cost to one extra round trip rather than three, making it practical to return the full transcript census.

**Caveats.** The ClinVar pathogenic density returned here is counted at a fixed review-status floor as a coarse burden
signal feeding no scored comparison, unlike `ClinVar.DescribeVariant`'s pool whose floor is chosen by the caller
([above](#goal-what-else-has-been-classified-at-this-codon-this-exon-and-across-the-gene)). In addition, GTEx samples no
retina, no megakaryocyte or platelet, and no trabecular meshwork, so for diseases of those tissues SM18's abundance limb
cannot be answered from GTEx at all; the tissue-specific transcript datasets that do exist ship as bulk files without a
query API, so adding them requires an offline ingest.

### Goal: has a calibrated functional assay measured this variant?

`*_FXN` (SM20) reads a calibrated functional assay's OddsPath. MaveDB is the public repository of MAVE score sets, and
the central design question for `MaveDb.DescribeVariant` is *which allele identity is queried*, because that choice
determines whether deposits are found.

**RPCs.** `MaveDb.DescribeVariant`, keyed on the variant's **ClinGen allele IDs and never on HGVS text**.

**What the upstream gives, and what has to be remapped.** MaveDB stamps every mapped variant with a ClinGen allele ID
and accepts a list of IDs in a single request, making allele identity the natural lookup key. HGVS text cannot serve
even as a fallback: MaveDB's only HGVS-bearing endpoint is a full score-set dump, and MaveDB renders each variant's
protein change against the assay's own target sequence rather than a standard transcript, so no caller HGVS string is
guaranteed to match the stored string.

Two remapping rules follow:

- **Both the canonical nucleotide allele (`CA*`) and the protein allele (`PA*`) are registered and queried**, because
  one MAVE score set may deposit variants at the nucleotide level while another deposits them at the protein level, and
  MaveDB's own bridge between the two is incomplete.
- **Candidate ordering is derived by the service** — sorted by match directness, then publication date descending, then
  identifier, selecting the first *calibrated* deposit along that order or else the first *scored* deposit — rather than
  trusting MaveDB's response order. Deriving the order is necessary because published score sets for a gene typically
  sit at version 1, so an identifier tiebreak alone falls back to registration order and consults the newest scan last.
  Why this ordering reflects evidential strength, and why a depositor's primary calibration marker is honoured, is
  covered in
  [`svcv4-interpretations.md`](svcv4-interpretations.md#functional-assays-which-deposit-speaks-and-about-what).

**Worked example.** For an `LDLR` synonymous variant in our evaluation set, the canonical nucleotide allele's
protein-equivalence list in the registry is empty, while its protein allele returns two MaveDB deposits. Querying only
the canonical nucleotide ID would report "no assay covers this variant" for a variant that two deposits actually score.

**Where the RPC raises.** A deposit that marks multiple calibrations as primary provides no basis for choosing one, so
the RPC fails rather than guessing. Any deviation from MaveDB's documented response schema also raises an error
(`UNKNOWN`) rather than returning zero candidates: otherwise, a renamed upstream JSON field would return `NOT_FOUND` and
report "no assay covers this variant" for every lookup.

**Caveats.** When MaveDB genuinely holds no score set for the allele, `NOT_FOUND` means no MAVE assay exists in MaveDB,
which leaves `*_FXN` unscored from MaveDB rather than awarding benign or pathogenic points.

### Goal: does a VCEP publish its own criteria specification?

SM3 ranks a curated VCEP or community frequency threshold first among its four DAFT methods, and several other codes
defer to a panel's gene-specific wording. A curation run therefore needs a structured way to fetch and cite a panel's
specification rather than scraping ad-hoc web pages that nothing downstream can verify.

**RPCs.** `Cspec.ListSpecifications`, over the ClinGen CSpec Registry.

**What the upstream gives, and what is deliberately not remapped.** Every specification currently in the CSpec Registry
is written against the ACMG/AMP-2015 guidelines; no SVCv4 specification exists in the registry yet. Accordingly,
`Cspec.ListSpecifications` emits no SVCv4 code, point value, or pre-approved DAFT: a panel's strength tiers come back as
verbatim strings, its criteria-combining rules stay untyped, and a VCEP frequency threshold enters SM3's four-rung
ordering at the rung its own mathematical derivation earns rather than at rung 1 merely by virtue of who published it
([`svcv4-interpretations.md`](svcv4-interpretations.md#criteria-specifications-a-vcep-profile-is-acmg-2015)).
Translating a panel's ACMG-2015 rules into SVCv4 points inside the service would invent an unsanctioned mapping.

**Specifications are not snapshotted server-side.** Whereas the `literature` corpus stores paper renderings so quote
directives can be verified by a linter, Zenodo already archives each CSpec document version permanently under its DOI.
The contract states the resulting consequence explicitly: a quote from a VCEP specification is verifiable by a human
against its Zenodo DOI, not against a local corpus store that an automated linter re-reads.

**Worked example.** Three evaluation cases motivated this RPC: one gene with a VCEP specification marking the
null-variant criterion not applicable along with the panel's rationale; a second gene where the panel defines
gene-specific phenotype tiers and frequency cutoffs; and a third gene where the panel states a population-frequency
threshold with **no derivation anywhere in the registry** — an omission that the run must be able to inspect and report.

### The contracts are written against the upstreams' failure modes

Public genomic APIs frequently return failure modes that look like scientific findings if passed through uninspected.
Six cross-cutting contract rules govern how the nine interfaces map upstream responses onto gRPC status codes; per-RPC
instances are documented in each `.proto` file.

#### An upstream's "no record" is a finding, and gets its own status

Absence from gnomAD is the `POP_FRQ` rarity input; MaveDB holding no score set means no MAVE assay covers the variant; a
splice predictor returning no score means the position is unscorable. Because these are settled negative answers from
the upstream database, they map to gRPC `NOT_FOUND`, reserving `UNKNOWN` for uncharacterised faults. Collapsing absence
into `UNKNOWN` would cause callers to retry settled questions and make database absence unusable as evidence.

The gRPC status taxonomy also drives the sandbox retry helper end to end: `NOT_FOUND` and `INVALID_ARGUMENT` are settled
answers and are never retried. `RESOURCE_EXHAUSTED` is never retried either, because no RPC on this surface rate-limits
its caller; it means the upstream response exceeded the transport size bound, which retrying will only reproduce. Keying
retries on gRPC status codes avoids fragile substring matching on error message text.

#### Where absence is scored, a shape precondition is required, not optional

`Gnomad.DescribeVariant` and `Splice.PredictDeltas` are the two RPCs whose absence is *scored* as evidence (whereas
MaveDB's absence simply leaves a code unapplied), and both upstreams answer a variant ID they cannot parse with **HTTP
200 carrying an absence** rather than an HTTP error.

> **Hazard.** Without a precondition check, a caller's typo is scored as "absent from gnomAD" — manufacturing rarity
> evidence from a mistyped field, and never retrying because `NOT_FOUND` is settled.

Both RPCs therefore enforce two guards:

1. **Syntactic precondition before the call.** Each RPC validates its positional ID against the *stricter* of the
   upstreams' accepted spellings before issuing any HTTP request, rejecting malformed IDs with `INVALID_ARGUMENT`.
1. **Semantic discriminator on the response.** Because no regex can catch semantic errors such as a nonexistent contig
   or a coordinate past the end of a chromosome, each adapter also inspects the upstream's warning/error fields to
   distinguish "cannot parse coordinate" (`INVALID_ARGUMENT`) from "variant not held in dataset" (`NOT_FOUND`).
   `Gnomad.DescribeVariant` validates the requested dataset name for the same reason: gnomAD resolves older releases
   that this RPC does not serve, which would otherwise silently change the allele-frequency denominator under a caller
   expecting v4 filtering allele frequencies.

#### An upstream 4xx is placed on that taxonomy, not passed through

When an upstream endpoint's HTTP 4xx is a judgement *about the caller's request*, a non-429 4xx maps to
`INVALID_ARGUMENT` — or to `NOT_FOUND` when that specific endpoint uses 4xx to spell "no record" (as NCBI Nucleotide
`efetch` does for an accession with no sequence). **HTTP 429 and 5xx remain retryable (`UNAVAILABLE`)**; mapping every
4xx the same way or passing statuses through unmapped would either retry deterministic 400 errors four times with
backoff or fail fatally on a transient 429 rate limit.

This mapping is defined per **endpoint** rather than per source, because one endpoint of a source can spell "no such
entity" as HTTP 400 while another endpoint of the same source returns HTTP 200 with an empty set (ClinVar's archive
fetch does both, which is why "no record" there is handled under `FAILED_PRECONDITION` below rather than `NOT_FOUND`).

Three upstream transports are **exempt** from mapping non-429 4xx responses to `INVALID_ARGUMENT`, because a 4xx from
them is never a verdict on the caller's input field:

- **gnomAD GraphQL**: the caller's variant ID rides in GraphQL variables and an invalid ID comes back inside HTTP 200,
  so an HTTP 400 from gnomAD means a malformed GraphQL query document (a bug in our service, mapped to `UNKNOWN`) and an
  HTTP 429 is the rate limiter (`UNAVAILABLE`).
- **Broad splice hosts**: both hosts report every variant-level problem inside HTTP 200 with an `"error"` string, so an
  HTTP 4xx indicates a malformed request line or moved endpoint rather than a bad variant input (except when the request
  line exceeds the length bound checked at the boundary).
- **EBI OLS4**: the MONDO term reaching OLS4's URL is read from a curated reference table rather than supplied by the
  caller, so an HTTP 4xx there reflects a stale reference table or retired MONDO term (`UNKNOWN`) rather than an
  `INVALID_ARGUMENT` from the caller.

(The weekly reference-table refresh job sits outside the gRPC status taxonomy altogether because it runs offline, where
any 4xx fails the job run.)

> **Rule of thumb.** The test for mapping an upstream 4xx to `INVALID_ARGUMENT` is not "does a caller-chosen value reach
> this URL?" (it does at two of the three exempt transports), but "is an HTTP 4xx from this endpoint the upstream's
> verdict on that caller value?" Where the answer is no, the service must enforce its own input shape precondition at
> the boundary instead.

#### An upstream that fails says which upstream it was

An HTTP 429 or 5xx, a connect/read/write timeout, or a dropped connection reaches the caller as `UNAVAILABLE` with a
message naming the RPC, the upstream, and what happened: for example,
`PredictDeltas: SpliceAI returned 503 for …; a 5xx is usually transient`. The message states the observation rather than
promising that a retry will succeed, because an upstream 500 can be deterministic (for instance, gnomAD returns HTTP 500
for a dataset name outside its enum, which is why `DescribeVariant` validates the dataset name before calling). Naming
the failing upstream on `UNAVAILABLE` lets the agent distinguish an external database outage from a bug in our service.

`UNKNOWN` is reserved for faults on our side of the boundary, including unexpected upstream response schemas and two
transport failures that look like upstream outages but are not: an outbound connection-pool timeout (meaning our own
service's concurrency pool was saturated before any upstream was contacted) and a TLS verification failure (which a
retry will not clear).

To keep error trailers bounded and safe for the sandbox guest to read, error messages follow four formatting rules:

1. **Name the upstream by label or public hostname, never by full URL.** Full URLs echo query parameters added by the
   service (such as VEP's long dbNSFP column list) and can run to several kilobytes for large indels.
1. **Summarise HTML error bodies as `"an HTML error page"` rather than quoting raw markup.**
1. **Scrub credentials and mask IP addresses in quoted error bodies.** When NCBI's E-utilities rejects a keyless request
   over its rate limit, its JSON error body echoes the caller's egress IP address inside the `"api-key"` field (and
   would echo the secret key itself if one were configured):
   ```json
   {"error":"API rate limit exceeded","api-key":"203.0.113.7","count":"4","limit":"3"}
   ```
   The error formatter replaces any `"api-key"` value with `"<withheld>"` and masks any other IPv4 or IPv6 literal in
   quoted bodies (`{"error":"API rate limit exceeded","api-key":"<withheld>",...}`).
1. **Mark servicer-authored status messages explicitly for the hatch.** The sandbox gRPC hatch forwards a status's text
   to the guest only when the servicer marked it as safe to expose
   ([`sandbox-worker.md`](sandbox-worker.md#the-hatch-is-the-capability-boundary)).

#### Two sources disagreeing is neither an absence nor a bad request

Because `NOT_FOUND` is scored as evidence of absence, it must only represent what an upstream said about an identifier
chosen by the caller. When a lookup is keyed on an identifier supplied by *another upstream*, "no record" means two
sources disagree:

- `ClinVar.DescribeVariant` fetches its archive record using the `VCV*` accession named by the ClinGen Allele Registry's
  crosswalk. If ClinVar answers that it holds nothing under that accession (via HTTP 400 or an empty 200 result set),
  one source contradicts the other about whether the record exists. Reporting `NOT_FOUND` would turn that contradiction
  into a novel-allele finding; reporting `INVALID_ARGUMENT` would blame the caller for a field the service populated.
- `ClinVar.SearchCodingSpan` queries ClinVar using the gene symbol named by the exon table. If ClinVar indexes no
  records under that symbol, every coding span returns empty — which would falsely score as "no informative variant at
  this codon."

Both cross-source disagreements therefore return `FAILED_PRECONDITION`, naming both upstreams and what each returned so
the inconsistency is visible and never mis-scored.

#### Every handler is bounded, and the bound is a status

An unbounded handler inside a sandboxed tool call does not merely respond slowly; if it hangs until the outer shell
timeout kills the agent's script, it destroys the results of every earlier call in the same script.

The system therefore enforces three nested timeout budgets, each strictly inside the one above it:

1. the sandbox harness's limit on a single shell tool invocation;
1. the guest retry helper's limit on a whole retried RPC call; and
1. the `evidence` service's per-handler deadline.

When the per-handler deadline expires, the service cancels the in-flight upstream requests and returns
`DEADLINE_EXCEEDED` naming the RPC. Because the guest's tool budget is larger than the handler ceiling, the agent's
script survives to handle the error and keep prior results. Unlike `UNAVAILABLE`, `DEADLINE_EXCEEDED` is **not** retried
automatically, because reissuing the same slow composite call would immediately consume the remainder of the caller's
budget.

> **Trade-off.** For chained composite RPCs that await several upstream calls in sequence, the sum of per-upstream
> timeouts can exceed the overall handler deadline, making the handler deadline a hard cut rather than a generous
> ceiling above the worst case. Bringing those per-upstream timeouts into line is what would make the handler bound
> generous rather than merely bounded.

### Provenance: every fact carries the releases it rests on

Every RPC response carries a repeated `Provenance` record — naming the source, every dataset release the answer rests
on, the exact query issued, and the retrieval timestamp ([`../PRODUCT.md`](../PRODUCT.md) §6). Stamping live retrievals
with their query timestamp and upstream versions makes every answer reproducible without operating local database
mirrors.

The release list on `Provenance` is required and must be complete on every call. When a run joins answers from two RPCs
(for example, a transcript-annotation release from VariantValidator and an expression release from GTEx), omitting the
release stamps makes a version skew between the two sources look like a biological discrepancy on the gene. If an
upstream does not include its version in the primary response payload, the adapter queries the upstream's version/info
endpoint separately; if a release stamp cannot be established, the call fails rather than returning a bare assembly name
or a partial release list.

`Provenance` is a `repeated` field on **every** response message, including single-source RPCs, so callers iterate a
single uniform field shape everywhere and a single-source RPC that later composes a second upstream does not break its
wire contract.

### The in-sandbox library: `themis.svcv4`

The deterministic SVCv4 framework logic ships into the sandbox image as an offline Python library (`themis.svcv4` under
[`themis/svcv4/`](../../themis/svcv4/)) accompanied by a skill document, imported and run by the agent in code mode.
Keeping `themis.svcv4` as an in-sandbox library rather than a gRPC service avoids a network round-trip on every
re-score, decouples rule iteration from `evidence` service deployments, and lets both the evaluation harness and
server-side Python import the exact same package.

The agent calls `themis.svcv4` with the retrieved evidence messages plus its own typed judgement inputs (the mechanism
level, the exon-relevance tier, the eligible informative variants, and the DAFT parameters), receives the transparent
point tally, derivation lines, and class band (which are shown, not load-bearing), and then authors the verdict
reasoning over the claims rather than the rolled-up score. **The library accepts the protobuf response messages
directly**: a per-source **door** function reads each response message at the documented proto paths, resolves its
vocabulary, and bins its scores into the corresponding SVCv4 code. That call flows into a **typed builder per variant
type** — one for each of SVCv4's ten consequence workflows — selected automatically from the routing consequence so the
caller supplies only the judgement inputs while the builder enforces that workflow's path structure and caps.

`themis.svcv4` implements:

- the scoring and combining engine: mapping points to pathogenicity classes, applying the 3×3 matrix multiplier to
  positive predictive points, adding informative-variant points *after* the matrix, enforcing per-code, concept, and
  category caps, taking the missense-vs-splice maximum path, and applying the GDV gate class cap;
- the splice-tier cells per workflow and per splice colour, including the two-layer combination cap across the three
  workflows that reach a splice tier;
- DAFT computation across SM3's four rungs with `POP_FRQ` binning and `POP_HMZ`;
- Grantham distance (as the missense same-codon comparator) and the four summable missense informative-variant
  sub-rules;
- NMD and NSD determination from transcript structure;
- OddsPath and animal-model calibration tables; and
- score-to-bin predictor thresholds with single-predictor-per-gene policy enforcement.

The package's data modules hold the transcribed SVCv4 reference tables and the frozen per-gene predictor policy, while
the ten workflow-diagram transcriptions remain in the private SVCv4 corpus at the revision named by the reference's
citation pin. Whenever a supplement diagram and main-text table disagree numerically, the ClinGen Pilot Calculator
serves as the tie-breaking **oracle**
([see Eval below](#eval-the-contract-runs-read-against-curations-then-the-calculator-oracle)).

**Two unscored edge-case numbers are required as explicit caller inputs rather than derived inside the library**: the
stop-lost NSD branch (routed through the null path) and the exon-duplication not-tandem cap. Because the standard
provides no worked example for either branch to verify a formula against, requiring the caller to pass the number keeps
that choice visible in the call site rather than hiding an invented rule inside the tally.

### The library takes categorical judgements and returns derivations

All seven clinical and locus codes (`CLN_*` and `LOC_*` in SM4 and SM5) are **deterministic downstream of a categorical
judgement**. The analyst's irreducible job is reading the case evidence to index the table: choosing the phenotype tier,
confirming whether each of SM4's conjuncts holds, checking parentage, recording each relative's genotype and affected
status, selecting the penetrance band, classifying in-trans phase, and picking which diagnostic yield figure applies
([`svcv4-interpretations.md`](svcv4-interpretations.md#clinical-and-locus-observations-the-categorical-readings)). The
library accepts those categorical inputs, applies the table lookup, aggregation, and caps, and returns both the awarded
points and the per-observation derivation string.

**Every scoring helper returns its human-readable derivation alongside its point total.** If a helper returned only a
bare float, a reviewer could not tell which table cell or cap produced the number — especially for confirmed versus
assumed in-trans phase (`CLN_CIS`), where the awarded points turn on a single word in a clinical referral. Each
derivation therefore states the phase *basis* and cell coordinates explicitly.

Three function signatures take unpacked inputs so a single summary argument cannot bury a required clinical reading:

- SM4's affected-proband column takes **three separate booleans** corresponding to its three conjuncts rather than a
  single pre-combined boolean;
- phenotype specificity takes the **numerator and denominator** counts rather than a pre-divided percentage; and
- the exon inclusion inventory takes the **admitted transcript set explicitly**, keeping SM18's admission filter visible
  at the call site.

In addition, the library enforces SM4's gating invariant directly: because the affected-proband code is available only
under two specific `POP_FRQ` outcomes, `themis.svcv4` rejects any tally that awards affected-proband points outside that
gate, when `POP_FRQ` is missing from the tally, or when `POP_FRQ` was marked undetermined.

**Where a clinical reading is unsettled in the standard, the library requires an explicit caller argument rather than
supplying a default.** Supplying a default boolean for SM18's waiver would silently permit double-counting the same
pathogenic variants, and defaulting a specific-versus-consistent phenotype flag to `False` would treat the *absence* of
a clinical examination as a negative examination. Those four open clinical readings are documented in
[`svcv4-interpretations.md`](svcv4-interpretations.md#clinical-and-locus-observations-the-categorical-readings), and the
fifth is the mechanism level, where `Uncertain` must never be supplied as a silent default. (Transcribing SM5's
per-co-segregation row table is the single remaining reference transcription needed to complete this module.)

The same rule — deterministic computation downstream of stated inputs belongs in `themis.svcv4` — also places six
further helpers in the library:

- the NMD/NSD structural inputs (exon structure from a transcript and PTC position from a protein HGVS);
- SM20's control-count-to-points lookup tables;
- splice-colour routing;
- exon-inventory tabulation with its pext comparison;
- the coding-prediction protein-fraction tier; and
- the missense informative-variant positional predicates (returning the matching list rather than a bare boolean).

### The shipped package carries the rules, not the evidence for them

Because `themis.svcv4` ships inside the sandbox image, its docstrings and reference data tables are readable by the
agent during an evaluation run. If a docstring or data comment included a worked example naming an evaluation gene, its
parameters, and its resulting point score, an agent classifying that gene could read the answer directly out of the
package — particularly since our reference evaluation set is drawn from the same SVCv4 practice corpus used to work out
our interpretations. Because no sandbox image copies `docs/`, all worked gene examples and empirical evidence live in
`docs/design/` (accessible to human developers and invisible to sandboxed runs).

Two authoring rules prevent benchmark leakage into `themis/svcv4/` — and **neither rule may be mentioned in comments
inside the shipped files**, because a comment noting that an example was withheld tells the model it is being evaluated
and turns every remaining gene name into a negative signal:

1. **The shipped scoring reference illustrates no rule with a specific gene.** Each scenario states its policy as a
   general condition/action pair with no gene-specific examples.
1. **Elsewhere in `themis.svcv4`, any worked docstring example must use a gene absent from the reference evaluation
   set** and must show how the result is derived from the inputs rather than stating a bare classification answer.

Because benchmark leakage is semantic (an example on a non-corpus gene can still leak a corpus case's answer if it
mirrors the case's exact parameters), this screen is enforced during code review when authoring or updating
`themis.svcv4`. Furthermore, when transcribing tables from the SVCv4 supplements into `themis.svcv4`, any worked
examples in the supplement that name genes or exon ranges in the reference set must be stripped or replaced with
non-corpus genes before transcription. General lookup grids that name no gene — such as SM3's DAFT grids for X-linked
MDEs and SM20's control-count grids for small functional assays — are kept in full because classifications cannot be
computed without them.

Unit test suites inevitably name specific test subjects and expected answers, so **sandbox container images must exclude
all test directories when packaging `themis.svcv4`**.

> **Guard deliberate omissions when re-reading from the SVCv4 corpus.** The shipped reference tables in `themis.svcv4`
> deliberately omit several cells and rules that would otherwise give away live evaluation questions, and carry no
> in-code marker saying so. Because the private SVCv4 corpus holds complete transcriptions of those tables, anyone
> syncing values from the corpus into `themis.svcv4` must check that those deliberate omissions are not accidentally
> filled in.

### Eval: the contract, runs read against curations, then the calculator oracle

Evaluation of the interfaces and rule library is split across three layers, each testing a distinct boundary:

- **Unit tests against recorded upstream fixtures and micro-cases.** Every RPC adapter is tested in CI against recorded
  upstream HTTP responses (requiring no live network access), and every `themis.svcv4` function, door, and workflow
  builder is tested against worked micro-cases. Driving each door from the service's protobuf response message and
  `classify_variant` from the variant's consequence exercises the full path from wire message to scored tally.

- **Agent runs evaluated against expert curations.** Unit tests on `themis.svcv4` only prove that the library matches
  our transcription of the rules. Whether a classification is *right* for a real variant is evaluated by the run-review
  loop, which compares full agent runs against expert-curated worksheets on the reference set
  ([`run-review-loop.md`](run-review-loop.md)). Where a curator's worksheet derives `MIS_PRD` using a different
  predictor than our frozen per-gene policy selects, both choices are valid pre-selected predictors; the run-review
  reader records that **predictor-attributable divergence** with both predictor names and scores rather than marking the
  run wrong (whereas diverging on the policy's *own* predictor is scored as an error).

- **The ClinGen Pilot Calculator oracle.** Because the
  [ClinGen Pilot Calculator](https://calculator.clinicalgenome.org/v4/pilot/ui/classification) evaluates classifications
  client-side in JavaScript, our oracle script fetches its bundle at runtime and executes it in an isolated JavaScript
  context to diff cap tables, class-boundary banding, the missense-vs-splice maximum path, and score clamping against
  `themis.svcv4`.

  Because the oracle requires a JavaScript runtime and live network fetch, it runs as a **manual verification gate
  rather than a CI job**. To prevent silent regressions between manual runs, every intentional divergence between
  `themis.svcv4` and the calculator
  ([`svcv4-interpretations.md`](svcv4-interpretations.md#where-the-standard-contradicts-itself)) is backed by a CI test
  pinning our side of the divergence against the loaded reference. The calculator oracle does not cover per-code point
  lookups (its bundle takes pre-scored code points rather than raw evidence) or the GDV gate (which the calculator does
  not implement); across everything the bundle does compute, `themis.svcv4`'s combining engine matches it outside the
  pinned divergences.

  > **Licensing of the calculator bundle (© Baylor College of Medicine).** The oracle script fetches the calculator
  > bundle at runtime and evaluates it in memory; no code or tables from the bundle are vendored, quoted, or copied into
  > tracked files, apart from noting the numeric ranges on the calculator side of our pinned divergences.

### Proto layout

The wire contract consists of **one `.proto` file per interface** under
[`schema/proto/themis/rpc/`](../../schema/proto/themis/rpc), each in package `themis.rpc.<source>`, plus
[`evidence.proto`](../../schema/proto/themis/evidence/models/evidence.proto) for the value types exchanged across
multiple interfaces.

**A message or enum lives in the shared `evidence.proto` file only when more than one interface exchanges it:**

- `Provenance` and genomic coordinate spans are exchanged by most interfaces (for example, both a ClinVar record and a
  transcript exon are placed by the same genomic span), so defining them in `evidence.proto` avoids cross-importing one
  source's RPC proto into another's.
- The molecular consequence routing enum crosses an even wider boundary: an evidence interface returns it for a variant,
  a curator records it on a worksheet, and the run-review loop compares the two using that single shared enum
  ([`curation-surface.md`](curation-surface.md#one-vocabulary-per-concept)).
- By contrast, the mode of inheritance is not in `evidence.proto` because no retrieval interface returns it in SVCv4's
  vocabulary; it lives in `svcv4.proto`. Coding-coordinate (`c.`) types live in `clinvar.proto` and the transcript
  namespace enum lives in `transcript.proto` because each has a single owning interface. `evidence.proto` declares no
  gRPC service and imports no RPC proto, keeping the dependency graph acyclic.

**Every RPC response layers three kinds of fields** following [`proto.md`](proto.md):

1. **Typed decision-bearing fields.** Any state that downstream code or the caller branches on is strongly typed,
   regardless of how raw its upstream origin is. For example, the ClinVar pool census (`search_term`, `total_count`,
   `fetched_count`, truncation flag) is typed because it determines whether a pool is complete or a prefix.
1. **Untyped upstream pass-throughs (`google.protobuf.Struct`).** Upstream payload fields that nothing in code branches
   on remain in a `Struct` so the model can read them without brittle hand-written schemas. When an RPC composes two
   upstreams, their pass-through payloads are stored in separate named fields (for example, `ClinVar.SearchCodingSpan`
   returns the VariantValidator projection in its own field rather than merging it with ClinVar's payload into a single
   `raw` field), so any field in a replay log can be attributed to its source.
1. **Generated schemas where the upstream publishes a formal schema.** For `ClinVar.DescribeVariant`, the queried
   allele's full VCV record carries rich per-submission observations, assertion methods, and citations governed by an
   official NCBI XSD. Rather than hand-transcribing that schema or collapsing it to a `Struct`, the protobuf message is
   generated from NCBI's XSD in a dedicated repository and consumed as a pinned wheel
   ([`proto.md`](proto.md#generated-upstream-schemas)).

Finally, every response ends with the repeated `Provenance` list. An RPC becomes callable from the sandbox when
annotated with the `agent_exposed` method option in its `.proto` file, from which the hatch allowlist and guest
forwarders are generated ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)).

## Alternatives considered

### Shape of the service surface

- **A single gRPC service carrying all twelve RPCs.** Rejected on both contract maintainability and naming clarity.
  Putting twelve RPCs and all of their message types into one `.proto` file forces every consumer of a gnomAD ID to
  compile ClinVar's submission model, collapses breaking-change detection into a single baseline across twelve
  independently evolving upstreams, and bloats the file beyond what a reader or model context window should hold. Naming
  also degrades under a single service: three interfaces each define a `DescribeVariant` RPC qualified by their service
  name (`Gnomad`, `ClinVar`, `MaveDb`), whereas a single service would have to bake the source names into the method
  names. Splitting one service and `.proto` file per source aligns the contract boundary with the upstream that owns it
  while still running all nine services in one deployment.
- **Deploying each evidence interface as a separate Cloud Run service.** Rejected because the sandbox's bubblewrap guest
  has no network egress of its own and reaches every tool through the same gRPC hatch; separate deployments would add
  cold starts, container images, and hatch-routing configuration without adding security isolation or improving per-RPC
  failure isolation.
- **Exposing the `themis.svcv4` scoring engine as a gRPC service.** Rejected because rule arithmetic requires no network
  egress or secrets and iterates frequently; wrapping it in an RPC would add a network hop on every re-score and couple
  scoring changes to service deployments.
- **Adding a standalone transcript-inventory RPC.** Rejected for now because `Transcript.AssessExonRelevance` already
  takes the exact gene/exon key that scopes the inventory and already joins the transcript sources by genomic interval.
  Adding a thirteenth RPC would pay the full forwarder, allowlist, fixture, and servicer cost for a second consumer that
  does not exist yet.
- **Fully typing every field of every upstream JSON payload in protobuf.** Rejected because the agent parses raw JSON in
  `Struct` fields reliably; hand-maintaining protobuf schemas for non-load-bearing upstream fields adds churn whenever
  an upstream adds a field. Where an upstream publishes an official schema (such as ClinVar's VCV XSD), we generate the
  protobuf schema rather than hand-authoring it ([Proto layout](#proto-layout)).
- **Letting the model perform the SVCv4 point arithmetic in prose.** In the gene-first assessment run, the model applied
  SVCv4 formulas and tables in prose with no library check. Even though the point total is shown rather than
  load-bearing, the shown tally and derivation lines must still be arithmetic that the curator can trust: a prose tally
  silently drifts on rounding, caps (`NUL` + `SPL` cap, `*_INF` + `CMM` cap), and table boundaries
  ([`../PRODUCT.md`](../PRODUCT.md) §6). Moving the published arithmetic into `themis.svcv4` makes the displayed tally
  deterministic and auditable while leaving the categorical judgements and the verdict reasoning with the analyst.
- **Letting the model choose which in-silico predictor to use per variant.** Rejected because SM6 prohibits choosing a
  predictor after seeing the scores (which constitutes multiple testing across correlated metapredictors). Predictor
  selection is therefore fixed once per gene in advance as frozen policy.

### Identifiers and keys

- **Accepting a positional variant ID on `Vep.Annotate` and routing it to Ensembl's region endpoint.** Although
  Ensembl's region endpoint returns the required predictor scores, accepting positional IDs on `Vep.Annotate` was
  rejected for two reasons: `Variant.Normalize` returns both GRCh37 and GRCh38 positional IDs whose strings do not
  self-identify their assembly (risking silent wrong-build queries), and converting a VCF-style padded indel ID into an
  Ensembl region span introduces a second coordinate normaliser prone to off-by-one errors.
- **Accepting Ensembl `ENST*` accessions on `Variant.Normalize` or `Transcript.GetStructure`.** Rejected because
  VariantValidator — which underpins both canonicalisation and coding-coordinate arithmetic — serves RefSeq coding
  accessions and holds no Ensembl records. The cross-namespace join needed for exon relevance is solved on genomic
  interval coordinates instead, testing inclusion natively inside each namespace.
- **Taking a ClinGen allele ID (`CA*`) directly on `MaveDb.DescribeVariant`.** Rejected for now because a canonical
  nucleotide `CA*` ID matches only nucleotide-level MaveDB deposits, whereas missense and synonymous variants are
  frequently assayed in protein-level deposits keyed on a `PA*` protein allele ID that is not currently surfaced on
  `Variant.Normalize`. Taking the variant expression lets `MaveDb.DescribeVariant` expand both the nucleotide and
  protein allele IDs through the Allele Registry in one place. Surfacing the `PA*` protein allele ID on the
  `Variant.Normalize` response would remove that extra hop, at the cost of a proto field and a per-transcript choice the
  caller then has to make correctly.
- **Accepting genomic HGVS (`NC_*:g.*`) on `MaveDb.DescribeVariant`.** Omitted because no upstream tool in the workflow
  emits genomic HGVS as its primary key, and keeping the accepted input forms small keeps validation errors clear.
- **Using Ensembl's transcript-lookup endpoint for `Transcript.GetStructure`.** Rejected on three counts: it is keyed on
  Ensembl accessions rather than RefSeq, it returns genomic exon bounds without coding (`c.`) coordinates (forcing the
  caller to sum CDS lengths by hand), and Ensembl's transcript alignment can differ from RefSeq's alignment used across
  the rest of the surface.
- **Requiring the caller to pass the premature termination codon (PTC) position into `Splice.PredictSkipOutcome`**
  rather than fetching the transcript sequence. Rejected because the caller cannot determine where a frameshifted
  reading frame hits a stop codon without the nucleotide sequence either; fetching the cached RefSeq sequence from NCBI
  Nucleotide lets the RPC read the actual PTC off the spliced sequence.
- **Enforcing a short character-length cap on positional variant IDs as a syntax rule.** Replaced with a separate
  transport-length check (~8 kB) that returns a dedicated error message. A tight per-allele character limit rejected
  valid 1 kb deletions returned by `Variant.Normalize` with a misleading "invalid format" error, blocking frequency and
  splice calls on large indels. Against the live upstreams, gnomAD sends the ID in a `POST` body and accepts any length
  within the small-variant release, whereas the Broad splice hosts place the ID in a `GET` request line that fails past
  the HTTP server's URI length limit. There is no second route for an allele past that transport bound: the splice
  predictors cannot be handed a sequence directly, and an allele of that size belongs to gnomAD's structural-variant
  release, which these RPCs do not serve.

### ClinVar and gene-disease scoping

- **Fetching a gene-wide all-classification ClinVar pool** instead of `ClinVar.SearchCodingSpan`. Rejected because
  SM19's informative-variant rules key on a single codon or exon, and on large genes an all-classification gene-wide
  pool exceeds the fetch bound and truncates before covering the target codon.
- **Sweeping a ClinVar string search across transcript versions** (`.3`, `.4`, `.5`) to find an allele indexed under an
  older RefSeq version. Rejected as the primary lookup route because each version guess costs a rate-limited E-utilities
  round trip, and the motivating `HOXA13` failure is indexed under a repeat notation that no transcript-version sweep
  would generate.
- **Running a fallback string search when the Allele Registry's ClinVar crosswalk is empty**, to catch variants
  classified since the last crosswalk sync. Rejected because it reintroduces the exact string-search hazards that
  motivated identity keying (including NCBI E-utilities tokenising an unquoted HGVS string and matching a *different*
  allele at the same codon) and forces every novelty result to carry an enum explaining which path answered. Crosswalk
  lag is bounded, stamped in `Provenance`, and eliminated completely when moving to the weekly VCV dump.
- **Keeping ClinVar on the live E-utilities query API permanently.** Rejected as an end state because a local index
  built from ClinVar's weekly VCV release resolves truncation, search-term sensitivity, missing pool-wide submission
  detail, unplaced repeat notation, and crosswalk lag simultaneously; what the query API does not cost is any change to
  the RPC surface, which is why living with the query API in the meantime is not a dead end
  ([above](#goal-what-else-has-been-classified-at-this-codon-this-exon-and-across-the-gene)).
- **Scoping `GeneDisease.DescribeGene` by a free-text disease substring filter** and taking the maximum validity across
  matching records. Rejected because substring matching fails silently in three ways: a natural clinical phrasing that
  is not a substring of the curator's label drops the ClinGen curation; a short substring matches multiple distinct
  disease entities and the maximum erases the class cap required by SM21's multiple-disorder rule; and in proto3 an
  empty string is indistinguishable from an unset filter, silently returning the gene-wide maximum by default. Passing a
  MONDO ID and checking OLS4's ontology subclass closure separates the two questions cleanly: whether one disease term
  is a subtype of another is answered by the ontology in code, while choosing which entity fits a patient's presentation
  is answered by the analyst from the full returned record list.
- **Removing the disease-term parameter from `GeneDisease.DescribeGene` altogether** and always returning every record
  on the gene. Rejected because keeping the optional MONDO term parameter preserves a code-side check when the caller
  names a specific disease: an unmatched or ambiguous disease term raises `FAILED_PRECONDITION`, whereas picking the
  wrong entity off an unfiltered list produces a valid `GateLevel` without raising.
- **Defining a finer harmonised inheritance-mode enum** that splits X-linked dominant from X-linked recessive across
  ClinGen and GenCC. Rejected because ClinGen's undifferentiated `XL` code still cannot map to either split member
  without inventing a subtype, any GenCC mode outside the enum (such as digenic `HP:0010984` on `CLCNKB`) still fails
  the lookup for the entire gene, and nothing downstream requires the service to filter records by mode
  ([above](#goal-is-the-gene-disease-relationship-valid-and-by-what-mechanism)).
- **Shipping a precomputed MONDO subclass closure in the GCS reference bucket.** Deferred: precomputing the closure in
  the weekly refresh job would remove `GeneDisease.DescribeGene`'s one live call to EBI OLS4, at the cost of a fifth
  mandatory startup object in GCS.
- **Querying PanelApp Australia live on the request path.** Rejected because live calls tie `GeneDisease.DescribeGene`'s
  availability to PanelApp's API and re-fetch large panel payloads on cold starts; pre-aggregating the Mendeliome and
  Incidentalome panels in the weekly refresh job removes PanelApp from the hot path and keys records by stable HGNC ID.
  The price of that choice is scope: the dump covers those two panels rather than all Australian panels.
- **Mounting the GCS reference bucket via Cloud Run FUSE** instead of using the `google-cloud-storage` client library.
  We chose the client library because it matches repository convention, adds no deploy-time volume mount lifecycle, and
  reads the four small reference files once into memory at startup.

### Exon relevance

- **Returning an `All / Most / Few` tier enum on `AssessExonRelevanceResponse`.** Rejected because two of SM18's three
  admission filters state no numeric test or threshold in the standard, and its single quoted pext figure is for an exon
  already settled by the MANE Select rule — leaving the `All`-versus-`Most` boundary uncalibrated. Emitting a tier enum
  from the RPC would encode an invented cutoff as if it were the standard's verdict.
- **Returning a `"no transcript omits the exon"` boolean** instead of the four-outcome transcript inventory. Rejected
  because a boolean reads as a finished `All` verdict, whereas SM18 licenses `All` only together with an expression
  abundance check that the boolean cannot establish. An empty `spans_but_skips` bucket carries the exact same structural
  fact without reading as "the RPC concluded `All`."
- **Building the transcript inclusion inventory from the splice predictor's response payload.** Rejected as a gene-wide
  denominator because SpliceAI Lookup's payload reflects that predictor's internal annotation snapshot (which holds
  fewer transcripts than VariantValidator's curated set) and is only fetched when a splice prediction call is made.
- **Renaming the evaluation module's exon-inclusion bucket keys to match the proto enum names now.** Deferred to the
  next evaluation-corpus schema bump: the evaluation module partitions the exact same four outcomes under key names
  pinned by existing reference measurements in the corpus.

### Scale and framework encoding

- **Operating local mirrors of gnomAD and the other per-variant databases now.** Staged behind explicit triggers rather
  than rejected: mirroring hundreds of gigabytes of genomic data to answer a couple of dozen single-variant queries per
  session is unnecessary when live retrievals stamped with `Provenance` are reproducible and the trusted `evidence`
  service egresses normally. The triggers that move a per-variant source to a local mirror are multi-session throughput
  binding against per-IP upstream rate limits, or a hard latency, availability, or offline requirement. ClinVar is the
  one source with its own decided end state (the weekly VCV index above), while the gene-disease reference tables sit
  outside the question because they have no per-variant live API.
- **Remaining on the frozen public Ensembl VEP REST API indefinitely.** Because EMBL-EBI has shipped the final release
  of the legacy Ensembl REST endpoint, its Ensembl release, MANE pairings, and dbNSFP snapshot are frozen and will
  gradually drift from current annotations (making `Provenance`'s release stamps essential). The announced Ensembl
  GraphQL successor is a lookup of pre-computed consequences for *catalogued* variants only — so it cannot annotate
  novel variants — and is scoped to gene and transcript consequences while reporting errors inside HTTP 200 responses.
  The end state for this upstream is therefore a self-hosted `ensembl-vep` deployment, which accepts HGVS directly.
  Nothing forces the move while the frozen endpoint serves.
- **Leaving the clinical and locus codes (`CLN_*`, `LOC_*`) out of `themis.svcv4`** and having a linter require the
  classification document to show per-cell arithmetic in prose. Rejected because only three of the seven codes in SM4
  and SM5 are 2D grids, one is a formula, one ships as an unkeyed value list, and capped code families cannot
  simultaneously satisfy "terms name cells" and "terms sum to the capped total." Having `themis.svcv4` compute the score
  and emit the derivation line retires the prose-math check altogether.
- **Adopting an upstream machine-readable encoding of SVCv4 instead of writing `themis.svcv4`.** No upstream executable
  encoding of the scoring rules exists:
  [`clingen-data-model/svcv4-model`](https://github.com/clingen-data-model/svcv4-model) (MIT) models only the
  *Classification* output structure while explicitly leaving the *Method* model (the rules and thresholds that evaluate
  evidence and produce workflow scores) to CSpec outside that repository and outside GA4GH VA-Spec. In `svcv4-model`,
  scores are bare floats with a single numeric constraint across the model, its classification enum is a placeholder
  without VUS subclasses, its one worked example contradicts its own threshold and cites a nonexistent code, and only
  the clinical workflows are sketched. Meanwhile, GA4GH VA-Spec ships no SVCv4 profile and CSpec's published
  specifications are all ACMG-2015 vintage. When a VA-Spec SVCv4 profile matures, it should be adopted at the output
  boundary where classifications are exported; only CSpec shipping machine-executable SVCv4 rule sets could replace
  `themis.svcv4`'s scoring engine.
- **Naming interfaces for the SVCv4 evidence category (`frequency`, `criteria`) rather than for the upstream source
  (`gnomad`, `cspec`).** Rejected because a caller must know the source's identifier forms, absence semantics, and
  release cadence to interpret the response, and source names remain stable when SVCv4 renames a code or moves a rule
  between supplements. The four interfaces not named for a single vendor (`variant`, `transcript`, `splice`,
  `gene_disease`) are the four that compose multiple upstreams behind one contract.

## Open questions

Unresolved points in this design are listed below; the framework's own contradictions and the clinical readings applied
to them live in [`svcv4-interpretations.md`](svcv4-interpretations.md).

- **Absence carries no coverage distinction.** A caller receiving `NOT_FOUND` from `Gnomad.DescribeVariant` cannot
  distinguish "not observed at a well-covered position" from "position not covered in exomes or genomes," which is the
  distinction rarity evidence (`POP_FRQ`) rests on. Whether absence becomes a typed coverage-annotated field on
  responses whose absence is scored is undecided.
- **Which ClinVar requirement pulls the weekly VCV dump forward first.** Whether NCBI E-utilities can sustain both the
  classified-variant pool behind `*_INF` and SM18's exon-level pathogenic density at usable query cost, or whether exon
  density — a count that must be complete across the gene to be meaningful — triggers the local VCV dump ingest ahead of
  rate limits.
- **Rate limits at multi-session scale.** Because gnomAD's and the Broad splice hosts' rate limits are enforced per IP
  address, concurrent curation sessions sharing an egress IP share one budget. How many concurrent curators that budget
  tolerates before firing the mirror or self-hosting trigger has not yet been measured.
