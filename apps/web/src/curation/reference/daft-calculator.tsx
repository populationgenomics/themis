"use client";

// The calculator's "Determine Maximum Credible Population Allele Frequency" reference, transcribed
// verbatim, including the source's own spelling of `Heterogenity` in the formula and its curly
// quotes. `reference.test.ts` checks every string below against a capture of the calculator's page,
// and checks that nothing of the modal is left untranscribed; the capture is not committed, so that
// check is a manual gate.
//
// The formula is the one fragment not checkable verbatim: the capture strips MathML, which leaves it
// letter-spaced (`M a x i m u m ...`). The spacing is an artifact of the capture, not the framework's
// wording, so it is set as the expression the calculator renders, and its letters are checked.

export const DAFT_CALCULATOR_TITLE =
  "Determine Maximum Credible Population Allele Frequency";

/** The definition, linearised. The capture cannot be matched verbatim here — stripping the MathML left
 *  it letter-spaced — so `reference.test.ts` compares this against the capture's letters, which pins
 *  the framework's own spelling of `Heterogenity`. */
export const DAFT_FORMULA =
  "Maximum Credible Population Allele Frequency = (Prevalence × Heterogenity) / Penetrance";

const INTRO =
  "Determining the disease allele frequency threshold (DAFT) i.e., the ‘maximum credible population frequency’ that a disease-causing allele could have for a given gene-disease pair. Maximum credible population allele frequency is defined by:";

const RATIONALE =
  "Rationale: A variant that is present in a control population at a frequency that is higher than the known frequency of the disease, likely does not cause that disease";

const APP_URL = "https://cardiodb.org/allelefrequencyapp/";
const APP_LINE = "Calculated using the Allele Frequency App:";
const SOURCE_LINE = "Source: Whiffin et al., 2017.";

interface Topic {
  heading: string;
  /** A named sub-heading the modal indents under the topic (the two heterogeneities), or none. */
  groups: { heading?: string; points: string[] }[];
}

const RECOMMENDATIONS: Topic[] = [
  {
    heading: "Inheritance - The mode of inheritance for the disease",
    groups: [
      {
        points: [
          "Enter the mode of inheritance for the monogenic disease entity (MDE), Monoallelic or Biallelic. We note that formally, these are not descriptors of inheritance but instead the genotype state of affecteds, the former associated with autosomal dominant inheritance and the latter associated with autosomal recessive inheritance. For semidominant inheritance, use the monogenic setting to give DAFT values that are useful for the monoallelic phenotype, which can also be used for the biallelic phenotype.",
        ],
      },
    ],
  },
  {
    heading:
      "Prevalence - The prevalence of the disease, expressed as ‘1 in X people’",
    groups: [
      {
        points: [
          "Enter the number X, where X is the denominator of the prevalence of the phenotype/s chosen to represent the MDE, expressed as ‘1 in X people’. We recommend the highest reasonable estimate for prevalence (i.e., the smallest value of X) to give the highest reasonable value for the DAFT. Note that this can be confusing, since the calculator user manipulates the denominator of the prevalence fraction - making the denominator smaller makes the prevalence estimate larger.",
          "For disorders with semidominant inheritance, use the prevalence for the monoallelic-associated phenotype to give maximum DAFT values that are also appropriate for the biallelic-associated phenotype. The analyst should err on the high side (round up) if there is a range of prevalences for a given MDE to give the highest reasonable value for the DAFT.",
        ],
      },
    ],
  },
  {
    heading: "Heterogeneity",
    groups: [
      {
        heading: "Genetic Heterogeneity",
        points: [
          "This is also known as locus heterogeneity. Enter the maximum reasonable proportion of the chosen phenotype prevalence attributable to variation in the gene under consideration. That is, for BRCA1-related cancer predisposition, for classifying a BRCA1 variant, enter the fraction of hereditary cancer families attributed to BRCA1.",
          "The value entered refers to the maximum genetic contribution of the gene for the VBC. We recommend the highest reasonable estimate for the contribution of the gene to give the highest reasonable value for the DAFT. Where gene-level prevalence estimates are not known, or to calculate a MDE-level DAFT, genetic heterogeneity can be set to ‘1’.",
        ],
      },
      {
        heading: "Allelic Heterogeneity",
        points: [
          "An allelic heterogeneity of ‘1’ would mean that 100% of disease attributable to that gene is caused by a single variant. The value entered refers to the maximum allelic contribution. For disorders with well-studied allelic spectrum, this value should be available from the literature.",
          "For disorders where pathogenic variants are not subject to significant negative selection, allelic heterogeneity can be estimated from gnomAD data. More guidance for attributing allelic heterogeneity is given in PMID 28518168. If there are high levels of uncertainty, allelic heterogeneity can be set to ‘1’ to give the highest possible DAFT value.",
        ],
      },
    ],
  },
  {
    heading: "Penetrance",
    groups: [
      {
        points: [
          "Enter the expected penetrance of the phenotype/s chosen to represent the MDE, considering the average age represented in the population dataset.",
          "Use the lowest reasonable estimate for penetrance to give the highest reasonable value for the DAFT. If penetrance is uncertain, values of 0.2 (low), 0.5 (medium), or 0.8 (high) could be considered (as used in the binning approach below).",
        ],
      },
    ],
  },
];

/** Every transcribed string, for the verbatim check. The formula is excluded by construction. */
export const DAFT_CALCULATOR_VERBATIM: string[] = [
  DAFT_CALCULATOR_TITLE,
  INTRO,
  RATIONALE,
  APP_LINE,
  APP_URL,
  SOURCE_LINE,
  "Recommendations",
  ...RECOMMENDATIONS.flatMap((topic) => [
    topic.heading,
    ...topic.groups.flatMap((group) => [
      ...(group.heading ? [group.heading] : []),
      ...group.points,
    ]),
  ]),
];

export function DaftCalculatorReference() {
  return (
    <div className="framework-voice space-y-4 text-[13.5px] text-ink-body leading-relaxed">
      <p>{INTRO}</p>
      <p className="rounded-sm bg-surface-warm-panel px-3 py-2 text-center font-mono text-[13px] text-ink-body">
        {DAFT_FORMULA}
      </p>
      <p>{RATIONALE}</p>
      <p>
        {APP_LINE}{" "}
        <a
          href={APP_URL}
          target="_blank"
          rel="noreferrer"
          className="text-link underline"
        >
          {APP_URL}
        </a>
        <br />
        {SOURCE_LINE}
      </p>
      <hr className="border-line-row" />
      <h3 className="field-eyebrow text-ink-label">Recommendations</h3>
      <div className="space-y-4">
        {RECOMMENDATIONS.map((topic) => (
          <section key={topic.heading}>
            <h4 className="font-medium text-ink-primary">{topic.heading}</h4>
            {topic.groups.map((group) => (
              <div
                key={group.heading ?? topic.heading}
                className={group.heading ? "mt-2 pl-4" : "mt-1"}
              >
                {group.heading ? (
                  <h5 className="font-medium text-ink-body">{group.heading}</h5>
                ) : null}
                <ul className="mt-1 list-disc space-y-1.5 pl-5 text-ink-muted">
                  {group.points.map((point) => (
                    <li key={point}>{point}</li>
                  ))}
                </ul>
              </div>
            ))}
          </section>
        ))}
      </div>
    </div>
  );
}
