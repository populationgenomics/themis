# litcache test fixtures

Curated sample papers for litcache (see
[`../../../docs/plans/literature-cache.md`](../../../docs/plans/literature-cache.md)
for the design). Each fixture maps to the behaviour it exercises.

This tree is mirrored to the **public** `themis` repo. Only redistributable bytes
live here: the one real paper is **CC-BY 4.0**; everything else is **synthetic**
(no copyrighted full text). The non-OA branch is exercised with a synthetic
DoclingDocument rather than a real subscription paper for exactly this reason —
the docling json *is* the extracted full text.

## Fixtures

| Path | Kind | Exercises | Notes |
|---|---|---|---|
| `oa/` | real, CC-BY 4.0 | S7, S9 | OA paper: docling json + pdf + JATS xml |
| `nonoa/` | synthetic | S7, S8 | non-OA branch: docling json + text pdf |
| `image_only/source.pdf` | synthetic | S10 | image-only pdf, no text layer |
| `ids/*` | synthetic | S7 | mixed-id keys; filename == bucket-style key |

### `oa/` — real OA paper (CC-BY 4.0)

> Sifrim A, Hitz M-P, Wilsdon A, *et al.* "Whole exome sequencing in 342
> congenital cardiac left sided lesion cases reveals extensive genetic
> heterogeneity and complex inheritance patterns." *Genome Medicine* 2017.
> DOI `10.1186/s13073-017-0482-5`, PMID `29089047`, PMCID `PMC5664429`.
> Licensed CC-BY 4.0 (http://creativecommons.org/licenses/by/4.0/).

- `docling.json` — the bucket DoclingDocument
  (`gs://cpg-themis-dev-fulltext/ingest/10.1186%2Fs13073-017-0482-5.json`),
  **minimized**: every `pages[].image` and `pictures[].image` raster (base64 page
  bitmaps, ~12 MB) nulled out. All structural content (`texts`, `tables`, `prov`,
  `origin`) is retained verbatim. docling schema `1.10.0`. Used by S7 (identity:
  `origin.filename` + `origin.binary_hash`); the OA conversion branch (S9) renders
  from the xml, not this json.
- `source.pdf` — the bucket pdf (the published article), retained as source bytes.
- `fulltext.xml` — JATS full text from Europe PMC
  (`https://www.ebi.ac.uk/europepmc/webservices/rest/PMC5664429/fullTextXML`),
  `article-type="research-article"`. The deterministic input for S9 (xml → litdown,
  `xml-faithful`) — committed so the conversion test needs no live fetch.
- `efetch.xml` — the NCBI efetch `PubmedArticleSet` for PMID `29089047`
  (`eutils.ncbi.nlm.nih.gov/.../efetch.fcgi?db=pubmed&id=29089047&retmode=xml`,
  DTD `pubmed_250101.dtd`). Deterministic input for S11b-1 (efetch → proto →
  pydantic → `metadata.json`, cross-id harvest) — committed so the resolver test
  needs no live fetch. Redistributable as the article's CC-BY record.
- `crossref.json` — the Crossref `works` response for DOI
  `10.1186/s13073-017-0482-5` (`api.crossref.org/works/<doi>`). Deterministic input
  for S11b-2 (Crossref → `PubmedArticle` mapping); the response carries no PMID, so
  it exercises the DOI-only path. Crossref metadata is CC0, so redistributable.

### `nonoa/` — synthetic non-OA paper

- `docling.json` — a from-scratch synthetic `DoclingDocument` (title + headings +
  paragraphs; schema `1.10.0`). Input for S8 (`export_to_markdown()` → markdown,
  `pdf-derived`). No real content.
- `source.pdf` — a synthetic pdf carrying a real text layer
  (`has_text_layer=true`); the retained source-bytes counterpart.

The non-OA-ness lives in id/access classification (the pipeline takes the docling
branch when no OA xml is retrievable), not in the bytes — so a synthetic paper
exercises S8 faithfully.

### `image_only/source.pdf` — image-only pdf

A synthetic pdf whose only content is a rasterized image (text drawn into a PNG,
no text operators). `pypdfium2` recovers **0** positioned characters, so S10 must
record `has_text_layer=false`. Counterpart: `nonoa/source.pdf` (text layer →
`true`). Verified (pypdfium2 5.10.1): `count_chars()` is 0 here, 171 there — the
S10 probe flags char-addressability on the ≥1-vs-0 boundary, not the exact count.

### `ids/` — mixed-id keys (S7 identity)

Each file is named with the **bucket-style encoded key** (the GCS object name in
`ingest/`); the key is the thing under test. Each holds a minimal
`DoclingDocument` whose `origin.filename` is the second id S7 harvests.

| Key (filename) | Scheme | Expected resolution |
|---|---|---|
| `10.1234%2Fsynthetic.fixture.001.json` | DOI (single-encoded `%2F`) | `doi:10.1234/synthetic.fixture.001`; origin `30000001` → `pmid` |
| `30000002.json` | bare-digit PMID | `pmid:30000002` |
| `1-s2.0-S0000000000000001-main.json` | Elsevier PII | `pii:S0000000000000001` |
| `10.1234%252Fsynthetic.fixture.002.json` | double-encoded DOI (`%252F`) | decode twice → `doi:10.1234/synthetic.fixture.002` |
| `qims-synthetic-001.json` | opaque | no external scheme → content-hash fallthrough |

Real bucket counts at capture time (2026-06): ~38k keys — mostly DOIs, 4432 bare
PMIDs, 2 Elsevier PII, 3 double-encoded DOIs.

## Regenerating the synthetic fixtures

`oa/` is captured (re-fetch from the bucket + Europe PMC; minimize per the note
above). The synthetic fixtures (`nonoa/`, `image_only/`, `ids/`) are reproducible
with the dependencies the consuming slices use — none are themis runtime deps, so
run them ephemerally:

```sh
uv run --with docling-core --with reportlab --with pillow python - <<'PY'
import json
import reportlab.lib.pagesizes
import reportlab.pdfgen.canvas
import PIL.Image, PIL.ImageDraw
from docling_core.types.doc import document as ddoc
from docling_core.types.doc import labels as dlabels

def write(path, doc):
    with open(path, "w") as f:
        json.dump(doc.export_to_dict(), f, indent=2)

# nonoa/docling.json
doc = ddoc.DoclingDocument(name="synthetic-nonoa")
doc.origin = ddoc.DocumentOrigin(mimetype="application/pdf", binary_hash=1111111111111111111, filename="synthetic-nonoa.pdf")
doc.add_title(text="Synthetic Non-OA Fixture: A Case Study in Nothing")
doc.add_heading(text="Abstract", level=1)
doc.add_text(label=dlabels.DocItemLabel.TEXT, text="This document is entirely synthetic. It exists to exercise the docling-to-markdown rendering path (S8) without redistributing any copyrighted full text.")
doc.add_heading(text="Methods", level=1)
doc.add_text(label=dlabels.DocItemLabel.TEXT, text="We invented three fictional samples and measured imaginary quantities.")
doc.add_heading(text="Results", level=1)
doc.add_text(label=dlabels.DocItemLabel.TEXT, text="Sample 1 was alpha. Sample 2 was beta. Sample 3 was gamma. No real data were involved.")
write("nonoa/docling.json", doc)

# nonoa/source.pdf — text layer present
c = reportlab.pdfgen.canvas.Canvas("nonoa/source.pdf", pagesize=reportlab.lib.pagesizes.letter)
c.setFont("Helvetica", 12); y = 720
for line in ["Synthetic Non-OA Fixture: A Case Study in Nothing",
             "This PDF carries a text layer; pypdfium2 recovers positioned characters.",
             "Sample 1: alpha    Sample 2: beta    Sample 3: gamma"]:
    c.drawString(72, y, line); y -= 24
c.showPage(); c.save()

# image_only/source.pdf — rasterized text, no text layer
img = PIL.Image.new("RGB", (1000, 300), "white")
d = PIL.ImageDraw.Draw(img)
d.text((20, 20), "Image-only fixture: this text is pixels, not characters.", fill="black")
d.text((20, 80), "pypdfium2 must recover NO positioned characters (has_text_layer=false).", fill="black")
img.save("image_only/_page.png")
c = reportlab.pdfgen.canvas.Canvas("image_only/source.pdf", pagesize=reportlab.lib.pagesizes.letter)
c.drawImage("image_only/_page.png", 72, 500, width=450, height=135)
c.showPage(); c.save()
import os; os.remove("image_only/_page.png")

# ids/* — minimal docs; filename is the bucket-style key
def minimal(filename, bh):
    doc = ddoc.DoclingDocument(name=filename)
    doc.origin = ddoc.DocumentOrigin(mimetype="application/pdf", binary_hash=bh, filename=filename)
    doc.add_text(label=dlabels.DocItemLabel.TEXT, text="Synthetic identity fixture; body intentionally trivial.")
    return doc

for key, origin_name, bh in [
    ("10.1234%2Fsynthetic.fixture.001.json", "30000001.pdf", 2000000000000000001),
    ("30000002.json", "30000002.pdf", 2000000000000000002),
    ("1-s2.0-S0000000000000001-main.json", "1-s2.0-S0000000000000001-main.pdf", 2000000000000000003),
    ("10.1234%252Fsynthetic.fixture.002.json", "30000003.pdf", 2000000000000000004),
    ("qims-synthetic-001.json", "qims-synthetic-001.pdf", 2000000000000000005),
]:
    write(f"ids/{key}", minimal(origin_name, bh))
print("regenerated synthetic fixtures")
PY
```

Run from this directory. docling-core ≥ 2.84.0 (emits schema `1.10.0`); any
docling-core the conversion slices adopt reads it. Synthetic `docling.json` files
are committed `indent=2`; the captured `oa/docling.json` is compact (matching the
bucket corpus).
