"use client";

import { Inheritance } from "@/gen/themis/evidence/models/evidence_pb";
import type { Cell } from "../ui/primitives";
import { countBody, crossCells } from "./shared";
import type { WorkflowDef } from "./types";

// Clinical observations (CLN), transcribed from the ClinGen Pilot Calculator.

const RARITY_NOTE =
  "Note: The variant must meet ‘rarity’ definition (POP_FRQ >= -1.0)";
const SEMIDOMINANT_NOTE =
  "Applicable if the mode of inheritance is semi-dominant.";

const UAF_AD: Cell[] = [
  {
    id: "cln_uaf_ad.near_100",
    cell: "CLN_UAF.ad.near_100",
    label: "Age-matched penetrance of MDE is near 100%",
  },
  {
    id: "cln_uaf_ad.80_100",
    cell: "CLN_UAF.ad.80_100",
    label: "Age-matched penetrance of MDE is 80 - 100%",
  },
  {
    id: "cln_uaf_ad.lt_80",
    cell: "CLN_UAF.ad.lt_80",
    label: "Age-matched penetrance of MDE is less than 80%",
  },
];

const UAF_ARXL = crossCells(
  "CLN_UAF.arxl",
  [
    { id: "near_100", label: "Age-matched penetrance of MDE is near 100%" },
    { id: "80_100", label: "Age-matched penetrance of MDE is 80 - 100%" },
    { id: "lt_80", label: "Age-matched penetrance of MDE is less than 80%" },
  ],
  [
    { id: "hom_hemi", label: "VBC is homozygous or hemizygous" },
    { id: "trans_p", label: "VBC is confirmed in trans with a P variant" },
    { id: "trans_lp", label: "VBC is confirmed in trans with an LP variant" },
  ],
);

const ALT_VARIANT: Cell[] = [
  {
    id: "cln_alt_var.more_severe",
    cell: "CLN_ALT.variant.more_severe",
    label:
      "Phenotype is more severe than expected for the MDE OR Is the same severity as expected for >1 allele contributing to the presentation (i.e., both alleles contributing to phenotype)",
  },
  {
    id: "cln_alt_var.not_more_severe",
    cell: "CLN_ALT.variant.not_more_severe",
    label:
      "Phenotype is NOT more severe than expected for the MDE (i.e., only one allele contributing to phenotype)",
  },
  {
    id: "cln_alt_var.not_consistent_recessive",
    cell: "CLN_ALT.variant.not_consistent_recessive",
    label:
      "Phenotype is NOT consistent with that expected for established recessive disease entity, with age-matched penetrance >80%",
  },
];

const ALT_GENE: Cell[] = [
  {
    id: "cln_alt_gene.more_severe",
    cell: "CLN_ALT.gene.more_severe",
    label:
      "Phenotype is more severe than expected for the MDE OR Is the same severity as expected for >1 allele contributing to the presentation (i.e., both alleles contributing to phenotype)",
  },
  {
    id: "cln_alt_gene.not_more_severe",
    cell: "CLN_ALT.gene.not_more_severe",
    label:
      "Phenotype is NOT more severe than expected for the MDE (i.e., only one allele contributing to phenotype)",
  },
];

const AFF_AD: Cell[] = [
  {
    id: "cln_aff_ad.specific_full",
    cell: "CLN_AFF.ad.specific_full",
    label:
      "Phenotype is SPECIFIC for MDE AND All relevant genes for disorder tested AND Non-genetic etiology is unlikely AND No additional variant of interest",
  },
  {
    id: "cln_aff_ad.specific_partial",
    cell: "CLN_AFF.ad.specific_partial",
    label:
      "Phenotype is SPECIFIC for MDE AND (Not all relevant genes for disorder tested OR High number of unexplained cases) OR Non-genetic etiology cannot be excluded OR Additional plausible VUS in same or different gene identified",
  },
  {
    id: "cln_aff_ad.specific_other_variant",
    cell: "CLN_AFF.ad.specific_other_variant",
    label:
      "Phenotype is SPECIFIC for MDE AND Additional variant of interest - LP/P variant in trans in same gene OR LP/P variant in different gene that explains phenotype (consider scoring individual under CLN_ALT)",
  },
  {
    id: "cln_aff_ad.consistent_full",
    cell: "CLN_AFF.ad.consistent_full",
    label:
      "Phenotype is CONSISTENT with MDE AND All relevant genes for disorder tested AND Non-genetic etiology is unlikely AND No additional variant of interest",
  },
  {
    id: "cln_aff_ad.consistent_partial",
    cell: "CLN_AFF.ad.consistent_partial",
    label:
      "Phenotype CONSISTENT with MDE AND (Not all relevant genes for disorder tested OR High number of unexplained cases) OR Non-genetic etiology cannot be excluded OR Additional plausible VUS in same or different gene identified",
  },
  {
    id: "cln_aff_ad.consistent_other_variant",
    cell: "CLN_AFF.ad.consistent_other_variant",
    label:
      "Phenotype is CONSISTENT with MDE AND Additional variant of interest - LP/P variant in trans in same gene OR LP/P variant in different gene that explains phenotype (consider scoring individual under CLN_ALT)",
  },
  {
    id: "cln_aff_ad.not_consistent",
    cell: "CLN_AFF.ad.not_consistent",
    label:
      "Phenotype is NOT CONSISTENT with MDE AND (consider scoring individual under CLN_UAF)",
  },
];

const AFF_ARXL = crossCells(
  "CLN_AFF.arxl",
  [
    {
      id: "consistent_full_lt_0_0001",
      label:
        "Phenotype is CONSISTENT for MDE AND All relevant genes for disorder tested AND Non-genetic etiology is unlikely AND No additional variant of interest AND Likelihood of observing two heterozygous variants is <0.0001",
    },
    {
      id: "consistent_full_0_0001_0_01",
      label:
        "Phenotype is CONSISTENT for MDE AND All relevant genes for disorder tested AND Non-genetic etiology is unlikely AND No additional variant of interest AND Likelihood of observing two heterozygous variants is >0.0001- 0.01",
    },
    {
      id: "consistent_partial",
      label:
        "Phenotype is CONSISTENT for MDE AND (Not all relevant genes for disorder tested OR High number of unexplained cases) OR Non-genetic etiology cannot be excluded OR Additional plausible VUS in same or different gene identified",
    },
    {
      id: "consistent_other_variant",
      label:
        "Phenotype is CONSISTENT for MDE AND Additional variant of interest - LP/P variant in different gene that explains phenotype (consider scoring individual under CLN_ALT)",
    },
    {
      id: "not_consistent",
      label:
        "Phenotype is NOT CONSISTENT for MDE (consider scoring individual under CLN_UAF)",
    },
  ],
  [
    {
      id: "trans_plp_confirmed",
      label: "Heterozygous VBC is confirmed in trans with a P/LP variant",
    },
    {
      id: "trans_plp_assumed",
      label: "Heterozygous VBC is assumed^ in trans with a P/LP variant",
    },
    {
      id: "trans_vus_confirmed",
      label: "Heterozygous VBC is confirmed in trans with a VUS variant",
    },
    { id: "homozygous", label: "Homozygous VBC" },
    {
      id: "no_second_variant",
      label:
        "Heterozygous VBC with NO 2nd variant, or 2nd variant in cis or with unknown phase",
    },
  ],
);

const DNV = crossCells(
  "CLN_DNV",
  [
    {
      id: "specific",
      label: "Phenotype SPECIFIC for gene (use this row for mono-allelic only)",
    },
    {
      id: "consistent",
      label:
        "Phenotype CONSISTENT with gene (Use this row for mono- or bi-allelic)",
    },
    {
      id: "not_consistent",
      label:
        "Phenotype not CONSISTENT with gene (consider scoring individual under CLN_UAF)",
    },
  ],
  [
    { id: "confirmed", label: "Denovo with confirmed parental relationships" },
    {
      id: "unconfirmed",
      label: "Denovo with unconfirmed parental relationships",
    },
  ],
);

const isAdLike = (inheritance: Inheritance) =>
  inheritance === Inheritance.AUTOSOMAL_DOMINANT ||
  inheritance === Inheritance.SEMIDOMINANT;
const isArXlLike = (inheritance: Inheritance) =>
  inheritance === Inheritance.AUTOSOMAL_RECESSIVE ||
  inheritance === Inheritance.X_LINKED ||
  inheritance === Inheritance.SEMIDOMINANT;

export const CLN_WORKFLOWS: WorkflowDef[] = [
  {
    id: "cln_uaf_ad",
    code: "CLN_UAF",
    title:
      "Workflow for Unaffected Observations - Autosomal Dominant inheritance",
    applicability: "Applicable to: Autosomal Dominant Inheritance",
    cells: UAF_AD,
    applies: ({ inheritance }) => isAdLike(inheritance),
    Body: countBody(UAF_AD, "Number of individuals", [
      "Note: Applicable for semi-dominant inheritance",
    ]),
  },
  {
    id: "cln_uaf_arxl",
    code: "CLN_UAF",
    title:
      "Workflow for Unaffected Observations - Autosomal Recessive / X-linked inheritance",
    applicability:
      "Applicable to: Autosomal Recessive Inheritance OR X-linked Inheritance",
    cells: UAF_ARXL,
    applies: ({ inheritance }) => isArXlLike(inheritance),
    Body: countBody(UAF_ARXL, "Number of individuals"),
  },
  {
    id: "cln_alt_variant",
    code: "CLN_ALT",
    title: "Workflow for Alternate Cause of disease - Variant",
    applicability:
      "Pathogenic or Likely Pathogenic variant/s detected (in expected zygosity) in same gene as VBC or in another gene associated with the phenotype",
    cells: ALT_VARIANT,
    applies: () => true,
    Body: countBody(ALT_VARIANT, "Applicable individuals", [
      "Note: Applicable for semi-dominant inheritance",
    ]),
  },
  {
    id: "cln_alt_gene",
    code: "CLN_ALT",
    title: "Workflow for Alternate Cause of disease - Gene",
    applicability:
      "Pathogenic or Likely Pathogenic variants detected (in expected zygosity) in another gene associated with the phenotype",
    cells: ALT_GENE,
    applies: () => true,
    Body: countBody(ALT_GENE, "Applicable individuals", [
      "Note: Applicable for semi-dominant inheritance",
    ]),
  },
  {
    id: "cln_aff_ad",
    code: "CLN_AFF",
    title:
      "Workflow for Affected Observations - Autosomal Dominant /X-linked inheritance",
    applicability: "Applicable to: Autosomal Dominant or X-linked Inheritance",
    cells: AFF_AD,
    applies: ({ inheritance }) =>
      isAdLike(inheritance) || inheritance === Inheritance.X_LINKED,
    Body: countBody(AFF_AD, "Applicable probands", [
      RARITY_NOTE,
      SEMIDOMINANT_NOTE,
    ]),
  },
  {
    id: "cln_aff_arxl",
    code: "CLN_AFF",
    title:
      "Workflow for Affected Observations - Autosomal Recessive / X-linked inheritance",
    applicability:
      "Applicable to: Autosomal Recessive Inheritance or X-linked Inheritance",
    cells: AFF_ARXL,
    applies: ({ inheritance }) => isArXlLike(inheritance),
    Body: countBody(AFF_ARXL, "Applicable probands", [
      RARITY_NOTE,
      SEMIDOMINANT_NOTE,
      "^Assumed in trans can be used if both the VBC and the P/LP variant have never been observed in cis, have both been identified in other probands, and were confirmed in trans with different P/LP variants in those other probands.",
    ]),
  },
  {
    id: "cln_dnv",
    code: "CLN_DNV",
    title: "Workflow for De Novo Observations",
    applicability: "Applicable to: Autosomal Dominant Inheritance",
    cells: DNV,
    applies: ({ inheritance }) =>
      isAdLike(inheritance) || inheritance === Inheritance.X_LINKED,
    Body: countBody(DNV, "Applicable probands", [
      "Note: For this workflow to the applicable, the variant must meet ‘rarity’ definition (POP_FRQ >= -1.0",
      "Applicable for X-linked inheritance only for male cases",
      "Applicable for semi-dominant inheritance only for dominant cases",
    ]),
  },
];
