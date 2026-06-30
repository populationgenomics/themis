# Themis glossary

Shared definitions so we use terms consistently (and so tooling has one source of truth). Product framing:
[`docs/PRODUCT.md`](docs/PRODUCT.md). Workspace model:
[`docs/design/workspace-model.md`](docs/design/workspace-model.md).

## Workspace

- **Project** — the access/data boundary: the datasets and users explicitly associated with a body of work; M:N to
  datasets (≈ a Claude.ai project).
- **Analysis** — a collaborative working session bound to a Project (≈ a Claude.ai "Chat", with subagents), with
  Claude.ai-style branching.
- **Working document** — the Analysis's evolving artifact (≈ a Claude.ai artifact: written, rewritten, versioned); the
  "opinion formed". *Not* the same as a Report.
- **Report** — the validated, approved form of a working document, linked to the Project's entities; Project-private;
  one accepted per entity, versioned.
- **Dataset** — a consented collection of samples + metadata (e.g. pedigrees) a Project may be granted. **Cohort** — a
  dataset of patients/cases (e.g. CaRDinal).

## Programme & organisations

- **AASGARD** — Australian Alliance for Secure Genomics and AI in Rare Disease; the MRFF-funded programme Themis is
  built under.
- **CPG** — Centre for Population Genomics.
- **MCRI** — Murdoch Children's Research Institute (an AASGARD partner).
- **VCGS** — Victorian Clinical Genetics Services: a clinical genetic-testing lab within MCRI (Victoria).
- **SA Pathology** — South Australia's clinical pathology service (a separate state lab, unrelated to VCGS).
- **CaRDinal** — CPG's managed, consented research rare-disease cohort; Themis's founding dataset.

## Ecosystem

- **Talos** — CPG's older, in-production heuristic reanalysis tool (no AI).
- **PanelApp** — gene-curation platform (Australian instance forked from Genomics England) that feeds Talos.
- **seqr / Metamist / Analysis Runner** — existing CPG infrastructure (case review / sample metadata / controlled
  analysis execution).

## Domain

- **ACMG (V4)** — the structured, points-based ACMG/AMP variant-classification criteria Themis adopts. **ACMG cell** — a
  single evidence criterion within it (e.g. PVS1, PM2).
- **VUS** — variant of uncertain significance. **HPO** — Human Phenotype Ontology (phenotype terms). **Proband** — the
  index patient of a case. **Segregation** — how a variant tracks with disease through a pedigree. **Matchmaking** —
  connecting patients who share a candidate variant/phenotype across datasets (with consent).
- **gnomAD / ClinVar / UCSC** — public variant-frequency / clinical-significance / genome reference resources.

## Method & infrastructure

- **Guiding prompt** — the scenario's "how to approach this analysis" instruction (kept minimal). **Working-document
  outline** — the scenario's starting structure for the working document (≈ the co-mathematician's initial paper).
- **Desire paths** — recurring custom analyses the agent reaches for, which graduate into new deterministic
  tools/annotations.
- **Lethal trifecta** — the exfiltration risk when private data × untrusted content × an external channel coincide.
- **Self-hosted sandbox / MCP tunnel** — Anthropic mechanisms that keep tool execution and MCP servers inside our
  network (orchestration stays on Anthropic's side).
