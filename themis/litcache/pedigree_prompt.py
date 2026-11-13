"""What the pedigree reader is told: grus's extraction prompt, its page, zoom and citation addenda, and genotypes.

Folded in from grus's internal extraction work: the extraction prompt at version 13 and the page addenda at version
6, the versions grus's reading ladder measured. Themis changes them in one way: genotypes the figure, its legend or
the paper states are recorded as grus's genotype table (`GENOTYPE_ADDENDUM`), so the reader is also given the
paper's checked variants and the lines that print them. Every text here is part of the extraction's harness
version, so a change to one gives existing records work for a run that names the step.
"""

from __future__ import annotations

# "CH0175", the family label in the examples, is printed in a published paper: Front Genet 2025, PMID 40406060.
EXTRACTION_PROMPT = """\
You transcribe the human pedigree (family-tree) diagrams in a figure into a structured, meaning-only
representation. A figure may contain ZERO, ONE, or SEVERAL distinct pedigrees, possibly beside
non-pedigree panels (sequencing traces, gels, scans, photographs, plots). Read the drawn symbols and
lines and report, for EACH pedigree, WHO is in it, their gender and clinical status, and HOW they relate.
Output only the JSON the schema requires: a set of pedigrees.

MEANING ONLY — NEVER EMIT GEOMETRY OR LAYOUT.
Do NOT emit coordinates, pixel positions, x/y, spacing, canvas or symbol sizes, colours, or fonts. A
renderer derives all positions from the relationships alone. The only spatial fact that carries meaning is
sibling BIRTH ORDER (left to right within one sibship), carried by the order of a mating's offspring list.

IDENTITY — every individual has a POSITION: generation + index (both integers).
- `generation`: the row, as an integer — the top generation is 1 (drawn "I"), the next 2 ("II"), and so on.
- `index`: the individual's arabic number within that generation ("II-3" -> generation 2, index 3).
- Assign an `index` to EVERY drawn individual, left to right, even when the figure prints no number for
  them (e.g. a married-in spouse). Within one pedigree the (generation, index) pair is unique and is how
  matings and offspring refer to a person.

GENDER + SEX (Bennett 2022):
- The SYMBOL SHAPE is the person's gender: square -> GENDER_MAN, circle -> GENDER_WOMAN, diamond ->
  GENDER_NONBINARY if the figure annotates a sex assigned at birth or gender-diverse identity, else
  GENDER_UNKNOWN. Most figures draw phenotypic sex under the older convention — map square -> MAN, circle
  -> WOMAN, and set nothing else. A TRIANGLE is NOT a gender symbol and NOT a diamond — it is a pregnancy or
  loss; record it via `reproductive_outcome` (below), never as `gender`.
- `sex_assigned_at_birth` (AMAB -> SAB_MALE, AFAB -> SAB_FEMALE, UAAB -> SAB_UNASSIGNED): ONLY when the
  figure explicitly annotates it. Omit otherwise.

CONDITIONS — declared ONCE, then referenced by id:
- The answer's top-level `conditions` lists every condition the figure's symbols show, ONCE each, for all its
  pedigrees together: `{id, name, inheritance}`. `id` is a short handle you choose ("k1", "k2", ... unique in the
  answer). `name` is the condition as the legend, key or caption names it (a disease, OR a specific variant when
  the halves distinguish alleles); leave it empty when nothing names it — the common "filled = affected" figure is
  one declaration with an empty name. Two declarations never share a name.
- `inheritance`: set it ONLY when text in the figure, its legend or its caption STATES the mode for that
  condition ("X-linked recessive", "autosomal dominant ...": AD / AR / XLD / XLR / Y-linked / mitochondrial).
  Never infer it: not from the symbols, the pattern of affected relatives, "de novo", or what you know of the gene
  or disease. Omit it otherwise.
- Declare only conditions some symbol shows; a declaration no symbol references is invalid.

CLINICAL STATUS — each person's `conditions`, one entry per condition the symbol shows, `{condition_id, status}`,
`condition_id` naming its declaration. A person has at most ONE entry per condition:
- A fully filled / shaded symbol -> one entry with status CONDITION_STATUS_AFFECTED.
- A symbol PARTITIONED into regions — quadrants, or a HALF / SECTION filled (NOT a small central dot) —
  encodes one condition PER region. Emit one entry per filled region, each referencing the declaration of the
  legend key that region maps to (a compound heterozygote whose two halves key to two variants references two
  declarations; the sequence/segregation panels tell you which). Give each the region's own status — AFFECTED
  for a solid disease region; CARRIER when the region marks carrying a variant (a heterozygous, clinically
  unaffected parent). Do NOT collapse a partitioned symbol to a single AFFECTED, and do NOT reduce a keyed
  half/section fill to a carrier of an UNNAMED condition — the region's legend name (the disease or variant it keys
  to) is the load-bearing detail that distinguishes it from another carrier.
- A carrier glyph (a central dot, a half-filled or hatched section, or a fill the legend keys to carrying) ->
  CONDITION_STATUS_CARRIER. The glyph NEVER sets the declaration's `inheritance`: a central dot marks a carrier
  of an autosomal or an X-linked condition alike (Bennett 2008), and a fill says nothing about the mode. Name
  the condition only if the legend or caption does.
- A vertical line through the symbol -> CONDITION_STATUS_PRESYMPTOMATIC. A "?" inside ->
  CONDITION_STATUS_UNKNOWN.
- An ordinary empty (unaffected) symbol needs NO conditions — omit the list.

OTHER PER-PERSON MARKS:
- A diagonal slash through the symbol -> `deceased: true`. Look for it deliberately on EVERY symbol — a thin
  slash is easy to overlook and is clinically load-bearing; do not assume a person is living by default.
- An arrow with "P" (always on an affected person) -> `proband: true`. A bare arrow: `proband: true` if it
  points at an affected person, else `consultand: true`. Trace the arrow to the EXACT symbol its tip touches;
  do not shift it to a neighbour. A pedigree may mark more than one proband; report every arrow.
- A number inside the symbol (several individuals collapsed) -> `count` (that number); an "n" -> leave
  `count` unset and set `count_unspecified: true`.
- A pregnancy or loss is an individual with a `reproductive_outcome`: pregnancy -> RO_PREGNANCY, stillbirth
  (SB) -> RO_STILLBIRTH, a triangle for a miscarriage (SAB) -> RO_MISCARRIAGE, a slashed triangle (TOP) ->
  RO_TERMINATION, an ectopic (ECT) -> RO_ECTOPIC. Put gestational age / karyotype in `annotations`.
- A gamete donor (D) -> `reproductive_role: RR_GAMETE_DONOR`; a surrogate (S) -> RR_GESTATIONAL_CARRIER.

ANNOTATIONS — capture EVERY other piece of as-drawn text tied to a person, one entry each, `{text, type}`,
`text` copied verbatim. This text is the most frequently MISSED information, so read it deliberately:
- text set inside or directly beside the symbol, AND text in a BOX / bracket / callout drawn around or next
  to it. A box around a symbol is an annotation container, NOT part of the symbol — the shape inside (square /
  circle / diamond) still determines gender; read the box's contents as annotations.
- a footnote / superscript marker attached to a person ("*", "†", "a") — emit it verbatim as _OTHER.
Types: ANNOTATION_TYPE_GENOTYPE ("N/M", "+/-", "+/+"), _VARIANT ("c.797G>A", "p.Arg266*"), _PHENOTYPE,
_AGE ("d.72", "42y"), _GESTATIONAL_AGE ("16 wk"), _KARYOTYPE ("47,XY,+21"), _MEASUREMENT, _PRONOUN,
_CHOSEN_NAME, _OTHER. Do NOT put the individual's own number here — it is the `index`.

RELATIONSHIPS:
- A mating joins its partners and lists its offspring in birth order. Each partner and each child is named
  by its {generation, index} Position. TRACE each descent line to its true parents: every sibling in one
  sibship is an offspring of the SAME mating; never split one drawn sibship across two matings, merge two
  families' descendants, or attach a child to the wrong couple. Preserve the figure's OWN numbering — an
  individual the figure labels "II-4" is generation 2, index 4; do not renumber or move it.
- Two partners: set `partner_a` and `partner_b`. A double line joining them -> `consanguineous: true`. A
  single slash on the line -> `status: RELATIONSHIP_STATUS_SEPARATED`; a double slash -> _DIVORCED.
- SINGLE PARENT: when only ONE parent is drawn over a sibship, set `partner_a` to that parent and OMIT
  `partner_b`. Do NOT invent an undrawn partner.
- FOUNDER SIBSHIP: when a sibship's sibling line hangs with NO parents drawn above it (a top-generation sib
  row, or aunts/uncles shown without their parents), emit a mating with BOTH `partner_a` and `partner_b`
  OMITTED, listing those siblings as its offspring in birth order. Do NOT invent parent symbols. This applies
  only to TWO OR MORE siblings sharing one bare sibship line; a single individual with no drawn parents is
  just a founder — leave it in no mating.
- Twins: offspring whose descent lines CONVERGE to one point on the sibship line (instead of each dropping
  straight down) share a `twin_group` number — look for this convergence deliberately, it is easy to miss.
  A horizontal bar joining the twins -> MONOZYGOTIC, none -> DIZYGOTIC, a "?" -> ZYGOSITY_TYPE_UNKNOWN.
  Adoption: a bracketed / dashed-descent child ->
  `adoption` (AD_IN / AD_OUT) and `parentage` (PA_ADOPTIVE, or PA_DONOR for a donor-conceived child).
- No children by choice / infertility -> the mating's `childlessness` (reason in the mating's
  `annotations`). Look for it deliberately: a couple whose descent line ends in a short horizontal bar with
  nothing below it, or a crossed/double-crossed descent line, marks infertility or childlessness.

ENUMS — NEVER emit a `*_UNSPECIFIED` value; it is a sentinel. Every individual needs a real `gender`, every
condition entry a real `status`. For an OPTIONAL field whose value is the ordinary default (a live individual, a
biological/current/childless-none relationship), simply OMIT it rather than emitting the default member.

ENUMERATION — A FIGURE IS A SET OF PEDIGREES.
Emit a `pedigrees` array with one entry per DISTINCT pedigree drawn:
- Several families ("Family 1" / "Family 2", or a labelled panel grid) are SEPARATE pedigrees — one entry
  each. NEVER merge distinct families; each family's positions are scoped to that pedigree.
- Give each pedigree its `labels`: short NAMES, one `{text, kind}` per label, `text` copied VERBATIM.
  - LABEL_KIND_FAMILY: the family's name or number as printed ("Family 3", "DUH family", "CH0175").
  - LABEL_KIND_PANEL: the panel letter, only when the figure has several panels and this pedigree is one.
  - LABEL_KIND_GENE: a gene named for this pedigree ("FLNA").
  - LABEL_KIND_PHENOTYPE: a condition's name as printed ("Leber congenital amaurosis"), the same text as the
    `name` of the condition declaration it labels.
  - LABEL_KIND_OTHER: another short title printed with the pedigree (an assay such as "WGS").
  Copy a printed title WHOLE even when it carries a variant ("Family B (c.797G>A)"), and keep its gene or
  phenotype as its own label too. Names from the legend or caption count ("the CH0175 family"). A label is a
  name, never a sentence: never copy a caption sentence, and an identifier the caption mentions that is not
  this pedigree's own name (a patient code such as "P03") is not a label.
- A KEY drawn in the figure (sample symbols beside text, often boxed or set apart, e.g. "Affected: X",
  "Carrier: Y") defines what each fill or pattern means. Use it to name conditions and statuses; its sample
  symbols are NOT individuals and never enter the pedigree.
- Ignore non-pedigree panels. If the figure contains NO pedigree, emit an EMPTY `pedigrees` array; never
  invent a pedigree from a non-pedigree panel.

Report only what the figure, its legend and the paper state. Do not infer relationships that are not drawn,
or genotypes that are not stated.
"""

PAGE_ADDENDUM = """\
READING A PAGE.
You are shown a whole page of a paper: body text, one or more figures, and their legends.
- Read the figure legend: it can define symbols, fills and panel identities that the figure itself does not.
- The caption, the page's body text and the lines of the paper given with its variants (before the page) may
  add facts the figure does not draw (a stillbirth, a condition's
  name, a stated mode of inheritance, who the proband is), but only when the text clearly refers to this
  family and person, and only what it states: "de novo" does not make inheritance dominant, and a gene's usual
  mode is not a statement about this family. Every such fact needs a support quoting the text (CITING
  SOURCES). Text about other families, cohorts or earlier reports never enters the record.
"""

PAGE_ZOOM_ADDENDUM = """\
ZOOMING.
The pedigrees are small at page size, so thin marks may be unreadable or invisible. Use the `zoom` tool,
which re-renders a region of the page from the source document at higher resolution.
- Verify every thin or small mark in a zoom before reporting it: consanguinity double lines, deceased
  slashes, twin bars and converging twin lines, relationship slashes, symbol shapes and fills, carrier dots,
  arrows, and the printed labels.
- `zoom` coordinates, and citation regions (below), are the only exceptions to "never emit geometry". Give
  each region as [x0, y0, x1, y1], the top-left and bottom-right corners in pixel coordinates of the PAGE
  image (the first image), always in that image's pixels, including after you have zoomed.
When you have read everything you need, reply with the JSON only.
"""

# `{fields}` is filled with the support paths grus's loader accepts.
PAGE_CITATION_ADDENDUM = """\
CITING SOURCES.
Say where each pedigree was read, and where any fact the figure does not draw came from.
- `citations` is one list for the whole answer. Each citation has an `id` ("c1", "c2", ... unique in the answer)
  and exactly one of `region` (x0, y0, x1, y1: a box in pixel coordinates of the PAGE image) or `quote` (the
  shortest verbatim span of the caption, legend or a line given with the variants that states the fact, copied
  exactly). Never write
  document ids or page numbers.
- Every pedigree's `evidence` lists the id of a region citation boxing the panel it is drawn in: the whole
  pedigree and its labels, nothing else.
- A fact you record that the figure does not draw (a stillbirth, a condition's name, a mode of inheritance, a
  proband named only in the caption) gets a `supports` entry on its pedigree: `field` (one of: {fields}),
  `condition_id` naming the condition's declaration when the field starts "condition.", `loci` naming the loci
  when it starts "genotype." or "locus.", the `individuals` (or
  `matings`, by their partners) it applies to, none for the whole pedigree, and `citations` listing the quote that
  states it. `condition.name`, `condition.inheritance` and the `locus.` fields describe a declaration, so their
  supports list no individuals. One field per support; several supports may share one citation.
"""

GENOTYPE_ADDENDUM = """\
GENOTYPES — a table of loci, and each person's genotype at them.
- When the figure, its legend or the paper states the alleles a person carries, record them. Declare each locus
  ONCE in the answer's top-level `loci`, `{id, name, gene, alleles}`, and give each such person a `genotypes`
  entry `{locus_id, alleles}` per locus.
- A locus is one variant, a gene with several, or a marker. `id` is a short handle you choose ("l1", "l2", ...
  unique in the answer, and never a citation's or condition's id). `name` and `gene` as printed.
- Each allele: `symbol` as the genotypes use it ("+", "M", "M1", "G", "*"); `name`, what the figure, legend or
  paper says it is, as printed ("c.1234A>G", "p.Trp370*", "wild type"), empty when nothing says; `reference`
  when it is stated to be the wild-type or reference allele; and `identifiers` for each resolvable name printed
  for it (`IDENTIFIER_SCHEME_HGVS` with or without a reference sequence, an rsID, a ClinVar id).
- A genotype's `alleles` are copies: ["M", "+"] heterozygous, ["M", "M"] homozygous, ["M"] hemizygous. Set
  `phase_set` only when phase is stated ("in trans", maternal or paternal): genotypes sharing one align slot by
  slot, so l1 ["M1", "+"] and l2 ["+", "M2"] in one phase set are in trans.
- The paper's checked variants are listed before the page, each with the lines that print it. Use them to
  learn what a symbol stands for ("M1 and M2 denote c.123A>G and c.456del", a table of each family member's
  genotype), but only where a line clearly refers to this family. When an allele is one of them, give it the name
  the figure or paper prints, and the checked name as an `IDENTIFIER_SCHEME_HGVS` identifier. Otherwise leave the
  allele's `name` empty.
- Record a genotype only where it is stated for that person, by a label drawn beside them or by text naming them.
  Never infer one: not for an obligate carrier, an untested relative, or from a person's affected status.
- A GENOTYPE or VARIANT annotation names in its `loci` the loci whose genotype it shows. Its person then has a
  genotype at each, so a label naming a variant without saying how many copies ("c.797G>A" alone, with nothing in
  the legend or text saying heterozygous or homozygous) names no loci and gives no genotype. Keep the annotation's
  `text` verbatim: the genotype says what it means.
- Declare only loci some person has a genotype at: a variant named with no carriers is a label.
- A genotype or an allele's name learnt from text gets a supports entry citing the quote that states it:
  `genotype.alleles` or `genotype.phase_set` with its `loci` and `individuals`, or `locus.name`, `locus.gene` or
  `locus.alleles` with its `loci`.
"""

SCHEMA_GUIDE = (
    'Return ONLY a single JSON object that conforms to the JSON Schema below — nothing before or after it, '
    "no prose, no markdown code fences. Use the schema's exact field names and enum spellings. Omit any "
    'optional field that does not apply; never emit null or a *_UNSPECIFIED value.\n\nJSON Schema:\n'
)
