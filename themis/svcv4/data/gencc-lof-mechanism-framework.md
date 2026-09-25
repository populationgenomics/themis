# GenCC loss-of-function mechanism framework

The criteria and confidence terms SVCv4 Supplementary Material 18 imports for its molecular-mechanism axis, from the
Gene Curation Coalition's *Recommendation for Loss of Function Mechanism Curation*, version 1.0, September 2025 — in
draft status upstream and subject to change. Source:
[`draft_lof_framework_position_statement.pdf`](https://clinicalgenome.org/site/assets/files/10698/draft_lof_framework_position_statement.pdf),
retrieved 2026-09-03. Names, thresholds and values only; the document's explanations are not reproduced.

The framework scores an entity whose gene-disease relationship is Moderate or above on the ClinGen gene-curation
framework, and caps the term at Suspected for one at Limited or below. The points of one criteria table — biallelic or
monoallelic, by the entity's inheritance — are summed, and the sum reads onto a confidence term in the last table.

## Criteria for a biallelic entity (document Table 1)

| Criterion                           | Condition                                                                                                                                                   | Points                                                              |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| pLOF spectrum                       | at least 20 unique pLOF, over 30% of all variants of the pLOF type, in at least 3 distinct exons                                                            | 0.5                                                                 |
| pLOF spectrum, alternative          | LOF at least one third of all LP/P variants, with at least 8 variants observed                                                                              | 0.3                                                                 |
| pLOF variant                        | meets PVS1 ignoring the mechanism requirement; NMD predicted                                                                                                | 0.2 per variant, plus 0.1 with functional evidence of the row below |
| non-pLOF P/LP variant               | functional variant-level evidence of absent or degraded transcript or protein                                                                               | 0.15 (range 0.1 to 0.2)                                             |
| Experimental evidence               | knockout model organism with consistent phenotype and zygosity                                                                                              | 0.3 cap (range 0.1 to 0.3)                                          |
| Consistency in population databases | homozygous variants of the types above absent or significantly reduced, given the disease's severity, age of onset and known affected individuals in gnomAD | 0.15                                                                |

Counting rules: a variant counts only within a case carrying biallelic variants of the types above, taken as causal on a
consistent phenotype; a pLOF whose second variant is rare and predicted damaging but not P/LP scores half; each variant
scores once, however often observed; one pLOF spectrum row scores, never both.

## Criteria for a monoallelic entity (document Table 2)

| Criterion                                           | Condition                                                                                                                                                  | Points                                                              |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| pLOF spectrum                                       | at least 20 unique pLOF, over 30% of all variants of the pLOF type, in at least 3 distinct exons                                                           | 0.5                                                                 |
| pLOF variant                                        | meets PVS1 ignoring the mechanism requirement; NMD predicted                                                                                               | 0.2 per variant, plus 0.1 with functional evidence of the row below |
| non-pLOF P/LP variant                               | functional variant-level evidence of absent or degraded transcript or protein                                                                              | 0.15 (range 0.1 to 0.2)                                             |
| Experimental evidence                               | knockout model organism with consistent phenotype                                                                                                          | 0.3 heterozygous knockout, 0.15 homozygous                          |
| Consistency in population databases: constraint     | gnomAD o/e upper limit below 0.3 for an early-onset disease, below 0.6 for a late-onset one                                                                | 0.15                                                                |
| Consistency in population databases: pLOF in gnomAD | at least 50% of the pLOF variants with more than one allele in gnomAD carry a criteria-provided P/LP ClinVar classification, over at least 3 such variants | 0.1                                                                 |

Counting rules: a variant counts only when taken as causal on a consistent phenotype, with inheritance consistent with
dominant or de novo occurrence; each variant scores once, however often observed; one knockout zygosity scores, the
heterozygous one where available.

## Confidence term (document Table 3)

| Confidence term | Evidence points | Also established by                                              |
| --------------- | --------------- | ---------------------------------------------------------------- |
| Established     | 0.99 or higher  | a ClinGen dosage-sensitivity score of 3, on a monoallelic entity |
| Likely          | 0.90-0.98       |                                                                  |
| Suspected       | 0.5-0.89        |                                                                  |
| Uncertain       | 0.49-0          |                                                                  |
