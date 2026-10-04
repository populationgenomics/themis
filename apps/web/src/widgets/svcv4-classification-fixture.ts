import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  Consequence,
  Inheritance,
} from "@/gen/themis/evidence/models/evidence_pb";
import { GateLevel } from "@/gen/themis/rpc/gene_disease_pb";
import {
  AssessmentStatus,
  Classification,
  Confidence,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  type Svcv4Classification,
  Svcv4ClassificationSchema,
} from "@/models/widgets";
import { DOC_OCR } from "@/server/adapters/fixture/literature";

// An SVCv4 classification as the library's widget builder writes one, for the offline workbench and
// the tests that draw it: a missense variant in FBN1 against Marfan syndrome, at +7 and Likely
// pathogenic, with the proband's unconfirmed parentage the call a curator would look at first. The
// numbers are the library's own for these inputs.

type Code = MessageInitShape<typeof Svcv4ClassificationSchema>["codes"];

const RETRIEVED_AT = timestampFromDate(new Date("2026-09-24T03:12:00Z"));
const GNOMAD = { source: "gnomAD GraphQL", datasetVersion: "gnomad_r4" };
const GNOMAD_RETRIEVAL = {
  source: "gnomAD GraphQL",
  datasetVersions: ["gnomad_r4"],
  retrievedAt: RETRIEVED_AT,
};

const CODES: NonNullable<Code> = [
  {
    code: "POP_FRQ",
    family: "POP",
    title: "Population frequency",
    status: AssessmentStatus.SCORED,
    points: "0.0",
    rawPoints: "0.0",
    basis:
      "absent from gnomAD v4.1 exomes and genomes; FAF 0 against a DAFT of 1.18e-05",
    cells: [
      {
        cellId: "POP_FRQ.bin.lt_1_5x",
        description: "< 1.5x DAFT",
        count: 1,
        pointsEach: "0.0",
      },
    ],
    releases: [GNOMAD],
    evidence: [
      {
        statement:
          "No carrier in gnomAD v4.1 at chr15:48,427,768 across 807,162 individuals.",
        source: { case: "retrieval", value: GNOMAD_RETRIEVAL },
      },
    ],
    rationale:
      "Absence from gnomAD bins below 1.5 times the disease threshold, which scores 0 and admits the clinical codes.",
    confidence: Confidence.SETTLED,
  },
  {
    code: "POP_HMZ",
    family: "POP",
    title: "Homozygous observations",
    status: AssessmentStatus.NO_DATA,
    statusReason:
      "No homozygote in gnomAD v4.1, and SM3 determines POP_HMZ only from the second eligible observation.",
  },
  {
    code: "CLN_CCS",
    family: "CLN",
    title: "Case-control studies",
    status: AssessmentStatus.NO_DATA,
    statusReason: "No case-control study reports this variant.",
  },
  {
    code: "CLN_AFF",
    family: "CLN",
    title: "Affected proband observations",
    status: AssessmentStatus.NO_DATA,
    statusReason:
      "The one affected proband is counted under CLN_DNV, and no other published proband carries the variant.",
  },
  {
    code: "CLN_DNV",
    family: "CLN",
    title: "De novo observations",
    status: AssessmentStatus.SCORED,
    points: "2.0",
    rawPoints: "2.0",
    cells: [
      {
        cellId: "CLN_DNV.specific.unconfirmed",
        description: "SPECIFIC · unconfirmed parentage",
        count: 1,
        pointsEach: "2.0",
      },
    ],
    evidence: [
      {
        statement:
          "A proband with ectopia lentis and aortic root dilatation carries the variant de novo.",
        source: { case: "citation", value: { docId: DOC_OCR, quote: "" } },
      },
      {
        statement: "The referral says the parents have not been tested.",
        source: {
          case: "caseText",
          value: { quote: "parents not tested; no family history" },
        },
      },
    ],
    rationale:
      "Ectopia lentis with aortic root dilatation is specific to Marfan syndrome, so the proband sits in the specific row. The paper infers de novo status from unaffected parents without testing them, so parentage is unconfirmed.",
    nearestAlternative: {
      cellId: "CLN_DNV.specific.confirmed",
      description: "A specific phenotype with confirmed parentage, +7",
      reason: "Neither the paper nor the referral reports parental testing.",
    },
    confidence: Confidence.LEANING,
    confidenceNote:
      "A trio result, or the clinic's letter, would settle parentage and move the code to +7.",
  },
  {
    code: "CLN_ALT",
    family: "CLN",
    title: "Alternate cause of disease",
    status: AssessmentStatus.NO_DATA,
    statusReason: "No other candidate variant is reported for the proband.",
  },
  {
    code: "CLN_UAF",
    family: "CLN",
    title: "Unaffected individual observations",
    status: AssessmentStatus.NO_DATA,
    statusReason: "No unaffected carrier is reported.",
  },
  {
    code: "LOC_PHE",
    family: "LOC",
    title: "Specific phenotype",
    status: AssessmentStatus.SCORED,
    points: "4.0",
    rawPoints: "4.0",
    cells: [
      {
        cellId: "LOC_PHE.yield.ge_82",
        description: "diagnostic yield · > 82%",
        count: 1,
        pointsEach: "4.0",
      },
    ],
    evidence: [
      {
        statement:
          "Sequencing FBN1 in patients meeting the revised Ghent criteria finds a pathogenic variant in over 90% of them.",
        source: { case: "citation", value: { docId: DOC_OCR, quote: "" } },
      },
    ],
    rationale:
      "The proband meets the revised Ghent criteria, and the cohort's testing method matches the case's, so the yield bin applies.",
    confidence: Confidence.LEANING,
    confidenceNote:
      "Whether the cohort's phenotype definition fits a proband scored on two features.",
  },
  {
    code: "LOC_SEG",
    family: "LOC",
    title: "Segregation with disease",
    status: AssessmentStatus.NO_DATA,
    statusReason: "The proband is the only affected family member reported.",
  },
  {
    code: "MIS_PRD",
    family: "MIS",
    title: "Amino-acid change prediction",
    status: AssessmentStatus.SCORED,
    points: "1.0",
    rawPoints: "1",
    basis: "AlphaMissense 0.876, at or above 0.792: Supporting",
    decision: "Supporting, from the gene's calibrated predictor: +1",
    pathFamily: "MIS",
    evidence: [
      {
        statement:
          "AlphaMissense scores the change 0.876, the predictor the gene's policy selects.",
        source: {
          case: "retrieval",
          value: {
            source: "Ensembl VEP",
            datasetVersions: ["VEP 114", "GRCh38"],
            retrievedAt: RETRIEVED_AT,
          },
        },
      },
    ],
    rationale:
      "The policy fixes AlphaMissense for FBN1, and its score lands in the Supporting bin, +1 before the exon-relevance factor of 1.",
    confidence: Confidence.SETTLED,
  },
  {
    code: "MIS_FXN",
    family: "MIS",
    title: "Functional assessment of the amino-acid change",
    status: AssessmentStatus.NO_DATA,
    statusReason: "MaveDB holds no assay of this change.",
    pathFamily: "MIS",
  },
  {
    code: "MIS_INF",
    family: "MIS",
    title: "Informative variants",
    status: AssessmentStatus.NO_DATA,
    statusReason: "ClinVar holds no other classified variant at codon 2335.",
    pathFamily: "MIS",
  },
  {
    code: "SPL_PRD",
    family: "SPL",
    title: "Splice change prediction",
    status: AssessmentStatus.SCORED,
    points: "0",
    rawPoints: "0",
    basis: "SpliceAI maximum delta 0.02, below 0.1: unlikely",
    decision: "Splicing unlikely, the blue path: 0",
    pathFamily: "SPL",
    evidence: [
      {
        statement: "SpliceAI's largest delta for the change is 0.02.",
        source: {
          case: "retrieval",
          value: {
            source: "SpliceAI",
            datasetVersions: ["1.3.1"],
            retrievedAt: RETRIEVED_AT,
          },
        },
      },
    ],
    rationale: "A delta below 0.1 puts the splice path on its blue row at 0.",
    confidence: Confidence.SETTLED,
  },
  {
    code: "SPL_SPA",
    family: "SPL",
    title: "Splice assay",
    status: AssessmentStatus.NO_DATA,
    statusReason: "No RNA or minigene assay is reported.",
    pathFamily: "SPL",
  },
  {
    code: "SPL_FXN",
    family: "SPL",
    title: "Functional assessment of the splice product",
    status: AssessmentStatus.NOT_APPLICABLE,
    statusReason:
      "The splice path predicts no aberrant product to assay (SM6).",
    pathFamily: "SPL",
  },
  {
    code: "SPL_INF",
    family: "SPL",
    title: "Informative variants",
    status: AssessmentStatus.NO_DATA,
    statusReason: "No classified splice-acting variant at this position.",
    pathFamily: "SPL",
  },
];

/** The FBN1 classification's payload, ticked and noted where `judgements` says. */
export function fbn1Classification(
  judgements: {
    reviewed?: boolean;
    note?: string;
    codes?: Record<string, { reviewed?: boolean; note?: string }>;
  } = {},
): Svcv4Classification {
  return create(Svcv4ClassificationSchema, {
    reviewed: judgements.reviewed ?? false,
    note: judgements.note ?? "",
    framework: {
      name: "SVCv4",
      status: "draft-phase3-pilot",
      citationsRepository: "populationgenomics/SVCv4-info",
      citationsRevision: "4e7050dc79f12ea80e81ce03e65013f0271ba8e2",
      usage:
        "Evaluation only. Not a validated implementation, not for clinical or diagnostic use; output is not a clinical variant classification. Re-verify every value against the final published standard and the ClinGen Pilot Calculator (https://calculator.clinicalgenome.org/v4/pilot/ui/classification).",
    },
    routing: {
      variant: {
        transcriptHgvs: "NM_000138.5:c.7003C>T",
        proteinHgvs: "NP_000129.3:p.(Arg2335Trp)",
        genomicHgvs: "NC_000015.10:g.48427768G>A",
        geneSymbol: "FBN1",
        hgncId: "HGNC:3603",
        caid: "",
      },
      consequence: Consequence.MISSENSE,
      entity: {
        disease: "Marfan syndrome",
        mondoId: "MONDO:0007947",
        inheritance: Inheritance.AUTOSOMAL_DOMINANT,
        mechanism: "loss of function",
        validitySource: "ClinGen Gene Validity",
        validityClassification: "Definitive",
        gateLevel: GateLevel.DEFINITIVE,
      },
      routes: [
        { family: "MIS", label: "amino-acid (MIS_)" },
        { family: "SPL", label: "splice blue (SPL_)" },
      ],
      rationale:
        "The referral describes ectopia lentis and aortic root dilatation, which fit FBN1's Marfan syndrome entity over its other curated ones.",
    },
    codes: CODES.map((code) => ({
      ...code,
      reviewed: judgements.codes?.[code.code ?? ""]?.reviewed ?? false,
      note: judgements.codes?.[code.code ?? ""]?.note ?? "",
    })),
    paths: [
      {
        family: "MIS",
        selected: true,
        total: "1.0",
        multiplier: "1.0",
        exonRelevance: "All",
        releases: [{ source: "Ensembl VEP", datasetVersion: "114" }],
      },
      {
        family: "SPL",
        selected: false,
        total: "0",
        multiplier: "1",
        releases: [{ source: "SpliceAI", datasetVersion: "1.3.1" }],
      },
    ],
    tally: {
      total: "7.0",
      band: Classification.LIKELY_PATHOGENIC,
      finalClass: Classification.LIKELY_PATHOGENIC,
    },
    sensitivity: [
      {
        assumption: "parentage confirmed",
        codes: ["CLN_DNV"],
        tally: {
          total: "12.0",
          band: Classification.PATHOGENIC,
          finalClass: Classification.PATHOGENIC,
        },
      },
      {
        assumption: "MIS_PRD at 0",
        codes: ["MIS_PRD"],
        tally: {
          total: "6.0",
          band: Classification.LIKELY_PATHOGENIC,
          finalClass: Classification.LIKELY_PATHOGENIC,
        },
      },
    ],
    classification: Classification.LIKELY_PATHOGENIC,
    bands: [
      {
        name: "B",
        classification: Classification.BENIGN,
        upper: "-4.0",
        upperInclusive: true,
      },
      {
        name: "LB",
        classification: Classification.LIKELY_BENIGN,
        lower: "-4.0",
        upper: "-1.0",
        upperInclusive: true,
      },
      {
        name: "VUS-low",
        classification: Classification.VUS,
        lower: "-1.0",
        upper: "2.0",
      },
      {
        name: "VUS-mid",
        classification: Classification.VUS,
        lower: "2.0",
        lowerInclusive: true,
        upper: "4.0",
      },
      {
        name: "VUS-high",
        classification: Classification.VUS,
        lower: "4.0",
        lowerInclusive: true,
        upper: "6.0",
      },
      {
        name: "LP",
        classification: Classification.LIKELY_PATHOGENIC,
        lower: "6.0",
        lowerInclusive: true,
        upper: "10.0",
      },
      {
        name: "P",
        classification: Classification.PATHOGENIC,
        lower: "10.0",
        lowerInclusive: true,
      },
    ],
  });
}
