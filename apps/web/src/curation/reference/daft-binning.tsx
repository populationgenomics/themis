"use client";

import { Fragment } from "react";

// The calculator's "Disease Allele Frequency Threshold - Binning Approach" reference: SM3's six DAFT
// lookup tables, transcribed verbatim from the calculator's rendering of them.
//
// Verbatim includes the calculator's own inconsistencies, which are its wording and not ours to tidy:
// the prevalence heading reads `i.e.` in the first table and `ie` in the other five, the penetrance
// heading reads `ie` in the X-linked dominant female table and `i.e.` in the other five, the male
// table labels two rows `1/1000` and `1/5000` where the others write `1/1,000` and `1/5,000`, and two
// allele-count cells are printed empty.
//
// Three readings of these tables disagree, and none is adjudicated here:
//
//   - Six DAFT cells differ from `themis/svcv4/data/population.py`, our own transcription of the same
//     tables read independently off SM3's images: SM3 prints `0.05` with a `*` where the calculator
//     prints the uncapped `0.0632`, `0.1` and `0.0707`. The star marks a value SM3 capped at 0.05; the
//     calculator does not cap. `themis/curation/tests/test_daft_tables.py` pins exactly those six, so
//     a seventh divergence fails.
//   - Two AC columns contradict each other across tables: the same threshold `0.1` is given as
//     `160,659` here and `60,659` in autosomal recessive, and `0.000025` as `51` and `13`.
//   - Two AC cells are blank where a threshold is printed (X-linked dominant combined, at 20%),
//     carried as `null` so a value nobody was given stays distinguishable from one nobody read.

export const DAFT_BINNING_TITLE =
  "Disease Allele Frequency Threshold - Binning Approach";

/** The three penetrance columns, and the allele-count heading the calculator repeats under each.
 *  The per-column `DAFT` and `gnomAD v4 AC` labels below are this rendering's own — the calculator
 *  heads the threshold column with nothing and the count column with the full sentence, which renders
 *  here as the table's caption. */
export const DAFT_PENETRANCE_COLUMNS = ["80%", "50%", "20%"];
const AC_HEADING = "Equivalent to gnomAD v4 AC of: (based on AN = 1,600,000)";
const PENETRANCE_HEADING_STANDARD =
  "Penetrance Estimate (round down - i.e. less penetrant)";
const PENETRANCE_HEADING_TERSE =
  "Penetrance Estimate (round down - ie less penetrant)";
const PREVALENCE_HEADING_STANDARD =
  "Prevalence Estimate (round down - i.e. more frequent)";
const PREVALENCE_HEADING_TERSE =
  "Prevalence Estimate (round down - ie more frequent)";

/** One threshold and the allele count the calculator prints beside it; `null` where it prints none. */
type BinningCell = [daft: string, gnomadAc: string | null];

interface BinningTable {
  title: string;
  prevalenceHeading: string;
  penetranceHeading: string;
  rows: { prevalence: string; cells: BinningCell[] }[];
}

const TABLES: BinningTable[] = [
  {
    title: "AUTOSOMAL DOMINANT",
    prevalenceHeading: PREVALENCE_HEADING_STANDARD,
    penetranceHeading: PENETRANCE_HEADING_STANDARD,
    rows: [
      {
        prevalence: "1/500",
        cells: [
          ["0.00125", "2,074"],
          ["0.002", "3,293"],
          ["0.005", "8,147"],
        ],
      },
      {
        prevalence: "1/1,000",
        cells: [
          ["0.000625", "1,052"],
          ["0.001", "1,666"],
          ["0.0025", "4,104"],
        ],
      },
      {
        prevalence: "1/5,000",
        cells: [
          ["0.000125", "224"],
          ["0.0002", "350"],
          ["0.0005", "847"],
        ],
      },
      {
        prevalence: "1/10,000",
        cells: [
          ["0.0000625", "117"],
          ["0.0001", "181"],
          ["0.00025", "433"],
        ],
      },
      {
        prevalence: "1/50,000",
        cells: [
          ["0.0000125", "28"],
          ["0.00002", "42"],
          ["0.00005", "95"],
        ],
      },
      {
        prevalence: "1/100,000",
        cells: [
          ["0.00000625", "15"],
          ["0.00001", "23"],
          ["0.000025", "51"],
        ],
      },
      {
        prevalence: "1/500,000",
        cells: [
          ["0.00000125", "5"],
          ["0.000002", "6"],
          ["0.000005", "13"],
        ],
      },
      {
        prevalence: "1/1,000,000",
        cells: [
          ["0.000000625", "3"],
          ["0.000001", "4"],
          ["0.0000025", "8"],
        ],
      },
    ],
  },
  {
    title: "AUTOSOMAL RECESSIVE",
    prevalenceHeading: PREVALENCE_HEADING_TERSE,
    penetranceHeading: PENETRANCE_HEADING_STANDARD,
    rows: [
      {
        prevalence: "1/500",
        cells: [
          ["0.05", "80,466"],
          ["0.0632", "101,717"],
          ["0.1", "60,659"],
        ],
      },
      {
        prevalence: "1/1,000",
        cells: [
          ["0.0354", "56,960"],
          ["0.0447", "71,994"],
          ["0.0707", "113,692"],
        ],
      },
      {
        prevalence: "1/5,000",
        cells: [
          ["0.0158", "25,560"],
          ["0.02", "32,295"],
          ["0.0316", "50,967"],
        ],
      },
      {
        prevalence: "1/10,000",
        cells: [
          ["0.0112", "18,109"],
          ["0.0141", "22,875"],
          ["0.0224", "36,088"],
        ],
      },
      {
        prevalence: "1/50,000",
        cells: [
          ["0.005", "8,147"],
          ["0.00632", "10,285"],
          ["0.01", "16,208"],
        ],
      },
      {
        prevalence: "1/100,000",
        cells: [
          ["0.00354", "5,781"],
          ["0.00447", "7,295"],
          ["0.00707", "11,489"],
        ],
      },
      {
        prevalence: "1/500,000",
        cells: [
          ["0.00158", "2,613"],
          ["0.002", "3,293"],
          ["0.00316", "5,177"],
        ],
      },
      {
        prevalence: "1/1,000,000",
        cells: [
          ["0.00112", "1,859"],
          ["0.00141", "2,341"],
          ["0.00224", "3,676"],
        ],
      },
    ],
  },
  {
    title: "X-LINKED DOMINANT - COMBINED (combined male and female prevalence)",
    prevalenceHeading: PREVALENCE_HEADING_TERSE,
    penetranceHeading: PENETRANCE_HEADING_STANDARD,
    rows: [
      {
        prevalence: "1/500",
        cells: [
          ["0.00167", "2,752"],
          ["0.00267", "4,374"],
          ["0.00667", null],
        ],
      },
      {
        prevalence: "1/1,000",
        cells: [
          ["0.000833", "1,394"],
          ["0.00133", "2,210"],
          ["0.00333", null],
        ],
      },
      {
        prevalence: "1/5,000",
        cells: [
          ["0.000167", "294"],
          ["0.000267", "461"],
          ["0.000667", "1,121"],
        ],
      },
      {
        prevalence: "1/10,000",
        cells: [
          ["0.0000833", "153"],
          ["0.000133", "238"],
          ["0.000333", "572"],
        ],
      },
      {
        prevalence: "1/50,000",
        cells: [
          ["0.0000167", "35"],
          ["0.0000267", "54"],
          ["0.0000667", "124"],
        ],
      },
      {
        prevalence: "1/100,000",
        cells: [
          ["0.00000833", "20"],
          ["0.0000133", "29"],
          ["0.0000333", "66"],
        ],
      },
      {
        prevalence: "1/500,000",
        cells: [
          ["0.00000167", "6"],
          ["0.00000267", "8"],
          ["0.00000667", "16"],
        ],
      },
      {
        prevalence: "1/1,000,000",
        cells: [
          ["0.000000833", "3"],
          ["0.00000133", "5"],
          ["0.00000333", "9"],
        ],
      },
    ],
  },
  {
    title: "X-LINKED DOMINANT OR RECESSIVE - MALE (sex-specific prevalence)",
    prevalenceHeading: PREVALENCE_HEADING_TERSE,
    penetranceHeading: PENETRANCE_HEADING_STANDARD,
    rows: [
      {
        prevalence: "1/500",
        cells: [
          ["0.0025", "4,104"],
          ["0.004", "6,532"],
          ["0.01", "16,208"],
        ],
      },
      {
        prevalence: "1/1000",
        cells: [
          ["0.00125", "2,074"],
          ["0.002", "3,293"],
          ["0.005", "8,147"],
        ],
      },
      {
        prevalence: "1/5000",
        cells: [
          ["0.00025", "433"],
          ["0.0004", "682"],
          ["0.001", "1,666"],
        ],
      },
      {
        prevalence: "1/10,000",
        cells: [
          ["0.000125", "224"],
          ["0.0002", "350"],
          ["0.0005", "847"],
        ],
      },
      {
        prevalence: "1/50,000",
        cells: [
          ["0.000025", "51"],
          ["0.00004", "77"],
          ["0.0001", "181"],
        ],
      },
      {
        prevalence: "1/100,000",
        cells: [
          ["0.0000125", "28"],
          ["0.00002", "42"],
          ["0.00005", "95"],
        ],
      },
      {
        prevalence: "1/500,000",
        cells: [
          ["0.0000025", "8"],
          ["0.000004", "11"],
          ["0.00001", "23"],
        ],
      },
      {
        prevalence: "1/1,000,000",
        cells: [
          ["0.00000125", "5"],
          ["0.000002", "6"],
          ["0.000005", "13"],
        ],
      },
    ],
  },
  {
    title: "X-LINKED DOMINANT - FEMALE (sex-specific prevalence)",
    prevalenceHeading: PREVALENCE_HEADING_TERSE,
    penetranceHeading: PENETRANCE_HEADING_TERSE,
    rows: [
      {
        prevalence: "1/500",
        cells: [
          ["0.00125", "2,074"],
          ["0.002", "3,293"],
          ["0.005", "8,147"],
        ],
      },
      {
        prevalence: "1/1,000",
        cells: [
          ["0.000625", "1,052"],
          ["0.001", "1,666"],
          ["0.0025", "4,104"],
        ],
      },
      {
        prevalence: "1/5,000",
        cells: [
          ["0.000125", "224"],
          ["0.0002", "350"],
          ["0.0005", "847"],
        ],
      },
      {
        prevalence: "1/10,000",
        cells: [
          ["0.0000625", "117"],
          ["0.0001", "181"],
          ["0.00025", "433"],
        ],
      },
      {
        prevalence: "1/50,000",
        cells: [
          ["0.0000125", "28"],
          ["0.00002", "42"],
          ["0.00005", "95"],
        ],
      },
      {
        prevalence: "1/100,000",
        cells: [
          ["0.00000625", "15"],
          ["0.00001", "23"],
          ["0.000025", "13"],
        ],
      },
      {
        prevalence: "1/500,000",
        cells: [
          ["0.00000125", "5"],
          ["0.000002", "6"],
          ["0.000005", "13"],
        ],
      },
      {
        prevalence: "1/1,000,000",
        cells: [
          ["0.000000625", "3"],
          ["0.000001", "4"],
          ["0.0000025", "8"],
        ],
      },
    ],
  },
  {
    title: "X-LINKED RECESSIVE - FEMALE (sex-specific prevalence)",
    prevalenceHeading: PREVALENCE_HEADING_TERSE,
    penetranceHeading: PENETRANCE_HEADING_STANDARD,
    rows: [
      {
        prevalence: "1/500",
        cells: [
          ["0.05", "80,466"],
          ["0.0632", "101,717"],
          ["0.1", "160,659"],
        ],
      },
      {
        prevalence: "1/1,000",
        cells: [
          ["0.0354", "56,960"],
          ["0.0447", "71,994"],
          ["0.0707", "113,692"],
        ],
      },
      {
        prevalence: "1/5,000",
        cells: [
          ["0.0158", "25,560"],
          ["0.02", "32,295"],
          ["0.0316", "50,967"],
        ],
      },
      {
        prevalence: "1/10,000",
        cells: [
          ["0.0112", "18,109"],
          ["0.0141", "22,875"],
          ["0.0224", "36,088"],
        ],
      },
      {
        prevalence: "1/50,000",
        cells: [
          ["0.005", "8,147"],
          ["0.00632", "10,285"],
          ["0.01", "16,208"],
        ],
      },
      {
        prevalence: "1/100,000",
        cells: [
          ["0.00354", "5,781"],
          ["0.00447", "7,295"],
          ["0.00707", "11,489"],
        ],
      },
      {
        prevalence: "1/500,000",
        cells: [
          ["0.00158", "2,613"],
          ["0.002", "3,293"],
          ["0.00316", "5,177"],
        ],
      },
      {
        prevalence: "1/1,000,000",
        cells: [
          ["0.00112", "1,859"],
          ["0.00141", "2,341"],
          ["0.00224", "3,676"],
        ],
      },
    ],
  },
];

/** The tables, for the checks that read them: verbatim wording, and the DAFT values against the
 *  library's own reading of SM3. */
export const DAFT_BINNING_TABLES: readonly BinningTable[] = TABLES;

/** Every transcribed string of the modal's chrome, for the verbatim check. */
export const DAFT_BINNING_VERBATIM: string[] = [
  DAFT_BINNING_TITLE,
  AC_HEADING,
  PENETRANCE_HEADING_STANDARD,
  PENETRANCE_HEADING_TERSE,
  PREVALENCE_HEADING_STANDARD,
  PREVALENCE_HEADING_TERSE,
  ...TABLES.map((table) => table.title),
];

export function DaftBinningReference() {
  return (
    <div className="space-y-7">
      {TABLES.map((table) => (
        <Table key={table.title} table={table} />
      ))}
    </div>
  );
}

function Table({ table }: { table: BinningTable }) {
  return (
    <section>
      <h3 className="framework-voice font-medium text-[13.5px] text-ink-primary">
        {table.title}
      </h3>
      <p className="framework-voice mt-0.5 text-[12px] text-ink-faint">
        {table.penetranceHeading}
      </p>
      <table className="mt-2 w-full border-collapse text-right font-mono text-[12px]">
        <thead>
          <tr className="border-line-row border-b">
            <th
              scope="col"
              className="framework-voice py-1.5 pr-3 text-left font-normal text-ink-muted"
            >
              {table.prevalenceHeading}
            </th>
            {DAFT_PENETRANCE_COLUMNS.map((column) => (
              <th
                key={column}
                scope="colgroup"
                colSpan={2}
                className="framework-voice border-line-row border-l py-1.5 pr-2 pl-3 font-normal text-ink-label"
              >
                {column}
              </th>
            ))}
          </tr>
          <tr className="border-line-row border-b">
            <th scope="col" className="py-1 pr-3" />
            {DAFT_PENETRANCE_COLUMNS.map((column) => (
              <Fragment key={column}>
                <th
                  scope="col"
                  className="framework-voice border-line-row border-l py-1 pr-2 pl-3 font-normal text-[11px] text-ink-faint"
                >
                  DAFT
                </th>
                <th
                  scope="col"
                  title={AC_HEADING}
                  className="framework-voice py-1 pr-2 pl-2 font-normal text-[11px] text-ink-faint"
                >
                  gnomAD v4 AC
                </th>
              </Fragment>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.prevalence} className="border-line-row/60 border-b">
              <th
                scope="row"
                className="py-1 pr-3 text-left font-normal text-ink-body"
              >
                {row.prevalence}
              </th>
              {row.cells.map(([daft, ac], column) => (
                <Fragment key={DAFT_PENETRANCE_COLUMNS[column]}>
                  <td className="border-line-row border-l py-1 pr-2 pl-3 text-ink-body">
                    {daft}
                  </td>
                  <td className="py-1 pr-2 pl-2 text-ink-muted">{ac ?? "—"}</td>
                </Fragment>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="framework-voice mt-1 text-[11.5px] text-ink-faint">
        {AC_HEADING}
      </p>
    </section>
  );
}
