# Design: annotating a paper's figures

**Related:** [`pedigree-extraction.md`](pedigree-extraction.md) (the first step that reads this record);
[`variant-extraction.md`](variant-extraction.md) (the shape of a spending step);
[`evidence-fulltext.md`](evidence-fulltext.md) (the steps that bring a paper up to date, and the rule that naming a
target is the decision to spend); [`litcache-manifest.md`](litcache-manifest.md) (the records and refs a step writes,
and where a paper's PDF is kept); [`cost-monitoring.md`](cost-monitoring.md) (how a model priced by prompt length is
counted). Terms in [`../../GLOSSARY.md`](../../GLOSSARY.md).

## Overview

Some of what a curator needs from a paper is only in its figures. A pedigree draws who in a family is affected; a
chromatogram shows the base that changed; a protein schematic places the variant in a domain. Before any of that can be
read, something has to say where the figures are and what each one shows. The pedigree step is the first that needs
this, and choosing the papers worth reading (triage) will need it too.

The obvious way to find a figure is through the words around it: a pedigree is introduced as "pedigree" or "family
tree", and its drawing prints labels like "II-3". On 60 papers from the literature cache, those cues found 25 of 31
pages that carry a pedigree, and 37 of the 62 pages they picked carried none. The misses are not random. A pedigree
drawn without printed labels, whose legend is on another page or does not use any of the words, is invisible to text.

So Themis looks at the pages instead. One step shows a small vision model, Haiku 5.5, each page of a paper's PDF on its
own, and records every figure it finds: each panel with its page, a box around it, and every kind drawn in it, one of
which is "pedigree". The pages' answers are then joined into the paper's figures, each with its label and the place of
its legend. Most drawings are joined by the figure label they print. The rest are joined by matching the panel tags a
legend names, letters or names such as a family's, to the panels that print them. On the 60 papers this found all 31
pedigree pages that Sonnet 5.5 finds, for 0.6 cents a paper. The record is stored under a `figures` ref, the manifest's
pointer to a paper's current record of a kind ([`litcache-manifest.md`](litcache-manifest.md)), and the steps that read
figures start from it.

## Background

### What a paper's figures look like in its PDF

The literature cache keeps a paper's PDF and a markdown rendering of its text
([`litcache-manifest.md`](litcache-manifest.md)). It does not keep a publisher's figure images, so the PDF's pages are
the only image of a figure that Themis has. A paper with no PDF has no figures to annotate.

A figure on a PDF page can be drawn as vector graphics, with its labels as real text, or pasted in as a raster image,
with its labels part of the picture. The PDF's text layer sees the labels of the first kind only. A figure is usually
made of panels, lettered A, B, C, and a panel is the unit a reader needs: one figure can hold a pedigree in panel A and
the chromatograms of its family in panel B. A single panel can also draw two kinds of thing at once. A common layout
draws a family's pedigree with a sequence trace under each person, and that panel is both a pedigree and a chromatogram.

The figure's legend is a block of text, usually directly below the figure. It is sometimes on the facing page, and in an
author manuscript or a supplement the legends are often gathered on pages of their own, away from the figures. A large
figure can also run across two or more pages, its label printed once. The legend describes the panels by their letters,
"(A) ... (B) ...", and a panel can be tagged with a name instead, such as a family's or a case's.

### Asking a model where something is

A Claude model can report where things are in an image as pixel coordinates of the image it was given, with the origin
at the top left. Two facts about this shape the design. The coordinates are in the image as the model received it, after
any shrinking the API applied, so the step sends each page at a size the API will not shrink and converts the
coordinates back to the page. And the boxes are approximate: a box drawn by the model can clip a figure's edge or take
in some of its margin.

### The papers measured

Two sets of papers are measured throughout. The test set is 60 papers drawn from the literature cache, described in the
Appendix, with Sonnet 5.5 reading every page as the reference. The canary is the 488 papers of an earlier run of
case-record extraction: the first 500 papers, by random id, of the cache's seed corpus. 479 of them have a PDF. The seed
corpus is mostly case reports, so the canary has far more pedigrees than the literature at large (36% of its papers with
a PDF draw one), and the share of pages that are pedigrees in it is not the share elsewhere.

### Haiku's price depends on the length of the request

Haiku 5.5 has two list prices. A request whose prompt is over 100,000 tokens pays five times as much for every one of
its tokens ([`cost-monitoring.md`](cost-monitoring.md)). A page sent at the size this step uses costs about 4,800
tokens, so a request carrying more than about 20 pages crosses the boundary. In the 479 canary papers, 56 had more than
20 pages, and they held 36% of all pages.

## Non-goals

- **Reading what a figure says.** This step says where a figure is and what it shows. Reading the family in a pedigree
  or the base in a chromatogram is the job of the step for that kind.
- **Tables set as text.** A table typeset as text is in the markdown rendering already. A table pasted in as an image is
  annotated, with its own kind.

## Design

### One request per page

A request that names a target needing figures runs the `figures` step on the paper
([`themis/litcache/figure_annotate.py`](../../themis/litcache/figure_annotate.py)). The obvious design shows the model
every page of the paper in one request. It sees the paper's layout as a whole: it can join a figure on one page to its
legend on the next, and follow a figure that runs across pages. This design was built first, and two facts broke it.

The first is the price boundary. A paper of more than about 20 pages pays five times as much for every page, and long
papers hold a third of all pages, so one request per paper costs an estimated 1.7 cents a canary paper against 0.7 cents
for requests kept under the boundary. The second is that seeing the whole paper did not find more pedigrees. On 221
canary papers, per-page requests found every pedigree page the whole-paper request found, and 7 more, each confirmed by
Sonnet: three pages of a supplementary figure holding 18 family pedigrees, a pedigree in a graphical abstract printed
twice, a figure page with four families' pedigrees, and an unlabelled illustration of a family. The whole-paper request
flagged one page per-page did not, and it held references, not a pedigree.

So the step renders each page and sends it to Haiku 5.5 as a request of its own, at most eight of a paper's pages at
once. The model answers through a tool: every figure drawn on the page, with its label as printed there, the box of its
legend if the legend is on the page, and its panels, each with a box, its tag as printed, and every kind drawn in it;
and every legend on the page, with the label it begins with and every panel tag it names. The page number comes from the
request, never from the model.

```mermaid
flowchart LR
    R["request names<br/>pedigrees"] --> F
    F["figures (spends, ~0.6 cents)<br/>12 pages, 12 requests"] -->|"p.3: (no label) A pedigree+chromatogram<br/>p.4: legend 'Figure 1'<br/>p.6: Figure 2, A gel, legend here"| J["join"]
    J -->|"Figure 1: A p.3, legend p.4<br/>Figure 2: A p.6, legend p.6"| FR[("figures record")]
    FR --> P["pedigrees step<br/>reads Figure 1"]
```

The instructions and the tool are the same for every page of every paper, and are cached; the page is not, since only a
repair would send it again. A page costs about 5,000 input tokens and 120 output tokens.

The step sends only images. Giving the model each page's text layer as well seemed likely to help it find legends, and
it did not: on the 60 papers it found the same pedigree pages with or without the text, and the text doubled the cost.
Sending only images also means a paper's cost depends only on how many pages it has, and that a scanned PDF with no text
layer is annotated like any other.

Each page is sent at the largest size the API accepts without shrinking it, 2,576 pixels on its long side. At the
smaller standard size, a third of the tokens, the model missed 2 of the 31 pedigree pages and drew 16 boxes that were
malformed or off their page.

### What a pedigree is, and why a panel has several kinds

With one kind per panel, a pedigree drawn with a chromatogram under each person forces a choice. The model usually
chooses "pedigree", but not always: shown one such page five times, it called the panel a chromatogram once. Seeing the
legend on the next page tipped it to "pedigree" every time, which is why the whole-paper request did not show the
problem. So a panel names every kind drawn in it. In the canary, 12% of panels had more than one kind, and 52 of them
were a pedigree with chromatograms.

Several kinds per panel invites the opposite error. A workflow diagram with three person icons over the words "Family
members" came back as a pedigree. The prompt therefore defines a pedigree by its drawing: individuals as squares,
circles or diamonds, joined by lines between partners and lines down to their children. Person icons, the word "family",
or a key of pedigree symbols on its own are not a pedigree, and a panel has a kind only when that kind of drawing is in
it, not because its text mentions it. On the disputed canary pages this cut the pages wrongly called pedigrees from 14
to 8 and kept every pedigree a curator would read. The one drawn family it now leaves out is an unlabelled illustration
of identity by descent, which carries nothing a reader could extract.

Most of the 8 that remain are pages carrying only a legend that describes a pedigree; seen alone, the page invites the
model to report the figure the legend describes. Each costs one Sonnet read, about 8 cents, which ends in "no pedigree".
A prompt rule against them cost a real pedigree page in 40, so they are accepted (§Open questions).

### Joining pages into figures

A page seen alone cannot say which legend on another page belongs to its drawing, or whether its drawing continues a
figure from the page before. So once every page has answered, the step joins the answers into figures, by rules that use
no model. A drawing here is what one page shows of one figure: the panels the model reported together on that page.

The obvious join uses page order. A drawing with no label and no legend of its own continues the figure on the page
before. That is right for a figure drawn across pages: one paper's supplement prints a legend alone on a page, then
three pages of family pedigrees with no label, and they are one figure. But page order cannot tell that case from two
others. Re-reading 171 papers' pedigrees under this join turned up both. Of the 6 figures it had joined across pages
there, 4 were wrong. Two of them:

- One paper draws two photographs on page 4, lettered A and B, with no label or legend. Page 5 draws photograph C under
  the legend of Figure 2, which describes A, B and C. The join gave page 4 to Figure 1, the charts on page 3, because
  Figure 2's legend was taken by page 5's drawing.
- Another paper's supplement draws one family's pedigree per page for eight pages, each headed with its case id, none
  with a legend. The join made them a single figure of eight pages. That is more pages than the pedigree step shows the
  model at once, so none of the families was read.

Position on the page looks like the next fix: a figure whose legend sits below its panels ends at the legend, so a
drawing on the next page starts a new figure. It breaks on layouts that papers use all the time. A legend can sit beside
its figure. In two columns, a figure in one column can have its legend at the foot of the other. A full-width figure can
sit above two columns of text.

The fact that settles most of these cases is printed on the page. A figure's panels carry tags, mostly letters, and its
legend names them: "(A, B) photographs of the proband's hands; (C) radiograph". A tag can also be a name, such as a
family ("F1", "Family 3") or a case id. So the model reports each panel's tag as printed, and for each legend its label
and every panel tag it names. A drawing showing panels A and B belongs to a nearby legend that names A and B, if no
drawing already placed under that legend shows them.

The step places each drawing by the first of these rules that applies:

1. **Its label.** A drawing whose printed label names a numbered figure, such as "Fig. 2", and which some legend in the
   paper carries, belongs to that figure. "Figure S2" and "Supplementary Figure 2" are the same label. Drawings on
   neighbouring pages that share a label are one figure. The same label far away starts another, since one of the two is
   a misreading. The figure keeps a legend the model reported with one of its drawings. Failing that, it takes the
   legend with its label on a page next to its own, then on its own pages, then the nearest. A neighbouring page comes
   first because a legend listed on the figure's own page is sometimes the model echoing the drawing's label. Two
   figures with the same label far apart share its legend.
1. **Its own legend.** A drawing whose answer gave it a legend on its own page takes that legend.
1. **Its tags.** A drawing's tags are its panels' tags, and its own label when that names no figure, such as "Family 2".
   A drawing whose tags are all named by a legend on its own page or the next or previous page takes that legend, as
   long as no drawing already under the legend shows those tags. The nearest page wins. On a tie the page after wins,
   since a legend more often follows its figure.
1. **A free legend on its page.** A legend is free when no figure holds it, the model did not report it with some
   drawing, it is not a table's caption, and its label names no figure a drawing prints. A drawing takes a free legend
   on its own page that carries a figure number, if the legend names the drawing's tags or either has none. The model
   sometimes lists a block of unlabelled body text as a legend; the number keeps it out. Tags run first, so a legend
   naming the drawing's tags on the next page wins over an untagged one on its own.
1. **Legends printed together.** Many supplements print all their legends one after another, then the figures, one to a
   page and unlabelled. The rule looks for two or more free legends with a figure number, on one page or on consecutive
   pages. It counts the drawings without a figure number on the pages after the last of those legends, to the end of the
   paper. If the two counts are equal, the first such drawing takes the first legend, the second the second, and so on.
   If any one pair's tags disagree, no drawing is paired. For example, legends for Supplementary Figures 1 and 2 on page
   13, then unlabelled drawings on pages 16 and 17, give page 16 to Supplementary Figure 1 and page 17 to Supplementary
   Figure 2. A third unlabelled drawing on page 30 would make the counts differ, and neither would be paired.
1. **A free legend on the next page.** A drawing with no label and no tags takes a free legend with a figure number on
   the next page.
1. **Continuation.** A drawing with no label and no tags continues the figure before it, the last on its page or else on
   the page before, if that figure has a legend.

A drawing no rule places is a figure of its own, with no legend. A drawing labelled as a table, such as "Table 1", is
also a figure of its own with no legend, whatever the rules say, and nothing after it continues the figure before it.
The model reports a table pasted in as an image, and under the rules it would take a figure's legend. Drawings placed
under one legend are one figure, their panels in page order, and the figure takes its label from the legend.

The model reports a figure's legend twice, once on the figure and once in the page's list, with boxes drawn by eye. Two
boxes on a page that mostly overlap are one legend, unless their labels name different figures, or one names a table and
the other a figure.

For example, take the paper with the photographs:

| page | drawing                                    | legends on the page                    |
| ---- | ------------------------------------------ | -------------------------------------- |
| 3    | "Figure 1", panels A, B, C                 | "Figure 1", naming A, B, C             |
| 4    | no label, panels A, B                      | none                                   |
| 5    | "Figure 2", panel C; "Figure 3", one panel | "Figure 2", naming A, B, C; "Figure 3" |

The label rule gives page 3 to Figure 1 and page 5's drawings to Figures 2 and 3. Page 4's drawing has no label and no
legend, so it falls to its tags. Figure 1's legend names A and B, but its drawing on page 3 shows them already. Figure
2's legend names A and B, and its drawing shows only C. So page 4 joins Figure 2. The eight pages of case pedigrees come
out as a figure for each drawing: their tags are case ids that no legend names, so no rule joins them, and the pedigree
step reads each one.

Continuation now needs a drawing with neither a label nor tags. Under the page-order join, a drawing whose numbered
label no legend carries counted as unlabelled, and continued the figure before it. One paper's page 35 draws
Supplementary Figure 5, panels a to e, and the paper prints no legend for it. The page-order join added it to
Supplementary Figure 4 on the page before. Now it stands alone. A label the model misread is now a figure of its own
too, where the page-order join would have attached it to the figure before. If its tags are named by a nearby legend,
the tag rule still places it.

These rules err towards splitting a figure. A figure split in two costs the pedigree step two reads, one of them perhaps
without the legend. A wrong merge can push a figure past the pages the step shows at once, so that none of it is read.
It can also show the reader another figure's legend.

The rules were checked against Opus 5.5 on 471 papers, using about 2,100 drawings that Haiku reported with the tags
above (§Appendix). Opus was shown the pages with each drawing and legend outlined. It did not see the rules' answers and
said which legend each drawing belongs to. A drawing agrees when it has the legend Opus gives it and is grouped with the
drawings Opus groups it with. 95.0% of drawings agreed under these rules, against 91.7% under the page-order join on the
same answers. On drawings with a pedigree panel the figures were 95.9% and 92.7%.

| rule                                 | drawings | Opus agrees |
| ------------------------------------ | -------- | ----------- |
| label                                | 1,848    | 96.5%       |
| own legend                           | 51       | 94.1%       |
| tags                                 | 57       | 93.0%       |
| free legend on its page              | 0        | —           |
| legends printed together             | 7        | 100%        |
| free legend on next page             | 2        | 100%        |
| continuation                         | 6        | 17%         |
| table                                | 22       | 100%        |
| none, a figure of its own            | 81       | 68%         |
| on a page whose answer needs repairs | 14       | 79%         |

The last row is drawings on pages whose answer the step would send back for a repair. The measurement did not repair
them, and the join never saw them.

The disagreements say where the rules fall short:

- **Misread labels** make most of the label rule's misses. One paper's page 6 came back as "Fig. 2", but the figure it
  draws is the third. That drawing takes Figure 2's legend, and Figure 2's real drawing on page 5 is left with none.
- **Body text listed as a legend.** In two papers the model listed unlabelled body text on a drawing's page as a legend,
  while the figure's legend was on the next page. The rules now leave such text alone, so the drawing stands without a
  legend.
- **Continuation**, 6 drawings. Five are pages of uncropped source blots after a figure's sections: Opus gives them no
  legend, and a reader of the paper takes them as part of the figure.
- **A legend alone on the page before**, among the drawings left without one. The supplement with three pages of family
  pedigrees prints their legend alone on the page before them, naming no tags, so the three pages are three figures
  without a legend. A rule giving such a drawing the legend on the page before agreed with Opus in 1 case of 5. In the
  others the legend belonged to an earlier figure.

### The record

The record ([`figures.proto`](../../schema/proto/themis/litcache/models/figures.proto)) lists a paper's figures. Each
figure has its label as printed, the place of its legend, and its panels. A place is a page of the PDF and a box on that
page. A panel has its tag as printed, its kinds, and its place. The tags a legend names are not kept: the join uses them
to decide which panels make a figure, and the record holds that answer. For example:

```
figures {
  label: "Figure 1"
  legend { pdf_page: 4  box { ... } }
  panels { label: "A"  kinds: [FIGURE_KIND_PEDIGREE, FIGURE_KIND_SEQUENCE_CHROMATOGRAM]  place { pdf_page: 3  box { ... } } }
  panels { label: "B"  kinds: [FIGURE_KIND_GEL_OR_BLOT]                                   place { pdf_page: 3  box { ... } } }
}
figures {
  label: "Figure 2"
  legend { pdf_page: 6  box { ... } }
  panels { label: ""  kinds: [FIGURE_KIND_CHART]  place { pdf_page: 6  box { ... } } }
}
```

Boxes are stored as fractions of the page, measured from its top-left corner, as the variant records' boxes are, rather
than in the pixels of the image the model saw. A step that reads the figure re-renders that part of the page at whatever
size it needs, and the box still means the same place. Each panel carries its own page, so a figure drawn across two
pages is two sets of panels under one figure.

The kinds are a closed list: pedigree, clinical photograph, medical imaging, microscopy, sequence chromatogram, gel or
blot, sequence alignment, gene or protein schematic, protein structure, chart, table image, diagram, and other. It
covers what the pedigree step needs and the kinds triage is likely to ask about. A new kind is added to the list, which
leaves existing records valid, and the harness version changes with it, so a request that cares re-annotates.

Each record carries a construction, a description of how it was made: here the model and the harness version. The
harness version is a digest of everything the model is given, as for the variant extraction, with a revision number for
changes the digest cannot see, such as the join rules. A change to either makes existing records stale for a request
that names the target.

### The step checks every box before it accepts a page's answer

Each box has to be four numbers making a box of at least a few pixels on the page image, and each kind has to be on the
list. An answer that fails goes back to the model with the reason, in the same conversation, as the variant and pedigree
steps do. About 2% of pages need a repair. A figure reported with only its legend on the page is read as that legend,
which is how the model reports a page that carries a legend whose drawing is elsewhere.

A page whose answer still fails after two repairs, that the model refuses, or that the API refuses as malformed, ends
the step without a record, and the job log keeps the reason as a permanent failure: the same page would fail the same
way again. The paper's other pages still run to their end, so every turn that was paid for is counted. A failure another
attempt could clear, such as a stream that breaks off mid-answer or a request that fails in transit, leaves the step to
be retried, as for the other spending steps.

The obvious alternative keeps the other pages: a record that lists the failed page with its reason, as the pedigree
record lists a figure it could not read ([`pedigree-extraction.md`](pedigree-extraction.md)). It lost because a figure
record with a page missing is wrong in ways no reader of it can see. The missing page may be the one that draws a
pedigree, and a paper whose record shows none settles as having nothing to read, which looks the same as a paper that
has no pedigree. It may hold the legend of the figure on the page before, which the join then gives to some other
figure, or leaves the figure without. Its drawings may continue a figure, and the next page's drawings then continue the
wrong one. The cost is that a paper with one such page has no figure record and no pedigrees read, and the job log names
the page.

That a box is on its page does not make it accurate. A reader of a figure pads its box before using it, and the pedigree
step shows the model the whole page anyway, with the figure's box as a pointer to where to look.

### Haiku 5.5, not Sonnet

The obvious choice is the stronger model, since finding figures is the gate in front of everything that reads them.
Sonnet 5.5 was run as the reference on the 60 papers, each seeing the whole paper. Haiku 5.5 agreed with it on every
paper and every page as to whether a pedigree was there. Where both models drew a box around the same panel, they gave
it the same kind 617 times in 645, and none of the 28 disagreements involved a pedigree. Haiku cost $0.46 for the 60
papers, and Sonnet $8.55. A hand check of the drawn boxes found them all in place, with Haiku's boxes a little looser
than Sonnet's more often than the reverse. Haiku seeing one page at a time found the same 31 pages.

## Alternatives considered

- **One request per paper.** It lets the model join a figure to a legend on another page, but a paper of more than about
  20 pages pays five times as much, and on the canary it found fewer pedigree pages, not more (§One request per page).
  It also asks the model for each panel's page number, which a request per page fixes.
- **Pages in groups of up to 20, overlapping by a page.** It keeps every request under the price boundary and some of
  the whole-paper context, at the price of merging the groups' answers, which is the same join with more cases. The
  whole-paper context it keeps found no extra pedigree in the measurement.
- **Text cues.** Free, and they need no model. They lost on the measurement in the Overview: a fifth of the pages that
  carry a pedigree are missed, and most pages they pick carry none. A wasted Sonnet read of a page costs more than
  annotating a whole paper.
- **grus's vision detector.** grus trained a small open-weight model to answer "is this a pedigree?" for a figure image,
  and it finds 92% of pedigree figures in the open-access corpus. It needs separate figure images, which the cache does
  not keep, and a GPU host the worker does not have. It answers one question about one kind, where this record serves
  every step that reads figures.
- **The PDF as a document block.** The API accepts a PDF directly and shows the model each page as an image with its
  text. The images are sized by the API, so the step could not convert the model's coordinates back to the page.
- **Joining by position on the page.** A figure would end at a legend printed below its panels. Legends beside their
  figure, or at the foot of the other column, defeat it (§Joining pages into figures).
- **A larger model joining the whole paper.** Opus 5.5, shown every page holding a figure, is the reference the join was
  checked against. It cost about 10 cents a paper, more than ten times the annotation itself.
- **A key of pedigree symbols in the prompt.** An image of the standard's symbols would cost little, read from the
  cache. The pedigree misses measured so far were a choice between two kinds both drawn in a panel, which a symbol key
  does not settle; it would help with unusual drawings, of which none has been seen missed.

## Open questions

- **Legend-only pages called pedigrees.** About 7 in 220 canary papers. A check that a pedigree panel's box covers drawn
  content in the PDF (images or vector paths, not text alone) would catch them in born-digital PDFs, though not in
  scanned ones, where every page is one image. Worth building if their Sonnet reads become a noticeable cost.
- **A gold set.** The measurements compare Haiku with Sonnet and the join with Opus, plus hand checks of the boxes and
  of six joined figures. A set of papers with hand-marked figures and legends would give recall against the truth.
- **Misread labels.** Most of the label rule's misses are a drawing labelled with a neighbouring figure's number. A
  check that a drawing's tags are among those its label's legend names would catch some of them.
- **A legend alone on the page before.** It is the commonest legend the rules leave unused. A rule taking it was wrong 4
  times in 5; one that also asks the legend to describe the drawing's kind might not be.
- **Very long PDFs.** A PDF with its supplement can run to hundreds of pages. The step annotates at most 100 pages, a
  bound on what one paper spends; whether longer papers deserve their supplement's figures is open.
- **Publishers' figure images.** A paper fetched as XML with no PDF has no figures to annotate. If the cache kept the
  publisher's figure images, the step could annotate them, each with its caption from the rendering.
- **Triage.** Triage decides which papers are worth reading before a request is made, and it will want to know what
  figures a paper has. Whether it reads this record or annotates figures itself is for its design to settle.

## Appendix

### The 60-paper measurement

Papers drawn at random from the literature cache's full-text papers with a PDF of at most 40 pages, in three groups: 30
whose text cues pick a page, 20 with family language (proband, kindred, affected relatives) but no cue, and 10 others.
620 pages in all, at the high-resolution size.

| run                                     | pedigree pages found (Sonnet: 31) | other pages called pedigree | cost, 60 papers |
| --------------------------------------- | --------------------------------- | --------------------------- | --------------- |
| Haiku 5.5, whole paper, images and text | 31                                | 0                           | $0.99           |
| Haiku 5.5, whole paper, images only     | 31                                | 0                           | $0.46           |
| Haiku 5.5, whole paper, standard size   | 29                                | 0                           | $0.12           |
| Haiku 5.5, one page per request         | 31                                | 1                           | $0.36           |
| Text cues (no model)                    | 25 (of 62 pages picked)           | 37                          | free            |

On the whole-paper runs, Haiku's pedigree boxes overlapped Sonnet's with a median intersection-over-union of 0.91; the
standard size drew 16 boxes that were malformed or off their page. Sonnet placed the legend of 18 figures on a different
page from the figure, 5 of them pedigrees. The 20 papers with family language and no cue had no pedigrees. Only 3 of
these 60 papers cross the price boundary, so the cost of one request per page against one per paper understates the
saving on real papers.

### The canary comparison

The 171 canary papers in which the whole-paper request found a pedigree, and 50 drawn at random from those in which it
found none: about 2,900 pages, $1.77 with one request per page. Sonnet judged each page on which the two designs
disagreed, asked only whether a pedigree is drawn on it.

| pages                                     | count |
| ----------------------------------------- | ----- |
| both designs call it a pedigree           | 194   |
| per-page only, and Sonnet sees a pedigree | 7     |
| per-page only, and Sonnet sees none       | 14    |
| whole paper only, and Sonnet sees none    | 1     |

The 14 are the disputed pages the stricter definition of a pedigree then cut to 8, while flagging all 40 of a sample of
the agreed pedigree pages.

### The join measurement

471 papers from the literature cache with a PDF: 300 drawn at random, and the 171 whose pedigrees had been read before
the per-figure pedigree record. Haiku 5.5 answered every one of their 5,979 pages with the panel tags and legend tags,
for $4.40, about 0.9 cents a paper. Opus 5.5 was shown each paper's 2,260 pages that hold a drawing or a legend, in
requests of up to 20 pages, with every drawing outlined in red and every legend in blue, and asked which legend each
drawing belongs to and whether each outlined legend is one. It cost $47.

| drawings                   | count | rules agree with Opus | page-order join agrees with Opus |
| -------------------------- | ----- | --------------------- | -------------------------------- |
| all                        | 2,088 | 95.0%                 | 91.7%                            |
| in the 300 random papers   | 1,261 | 95.6%                 | 92.5%                            |
| in the 171 pedigree papers | 827   | 94.0%                 | 90.4%                            |
| with a pedigree panel      | 317   | 95.9%                 | 92.7%                            |

A drawing agrees when it has the legend Opus gives it, or none when Opus gives none, and the drawings grouped with it
are those Opus groups with it. Where the model drew two boxes for one legend, Opus and the join can name different
boxes, which counts as a disagreement. Six of the joined figures were also checked by hand. The rules get five right,
and split the sixth, the three pages of family pedigrees, into three figures without their legend.
