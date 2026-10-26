# Design: the litcache manifest

**Related:** [`proto.md`](proto.md) (how at-rest records are serialised, and how their schemas may change),
[`evidence-fulltext.md`](evidence-fulltext.md) (the steps that bring a paper up to date, including its text, and how
readiness is read off the store), [`literature-evidence-layer.md`](literature-evidence-layer.md) (the interface that
serves the store to the agent and the browser), [`../plans/literature-cache.md`](../plans/literature-cache.md) (the
build plan).

## Overview

Themis's full-text store keeps each paper as a directory in a bucket, named by the paper's `doc_id`. Most of what a
directory holds is bytes named by their own content hash: the PDF and XML a paper was fetched as, and the markdown made
from them. A name of that kind says what the bytes are, but not whether they belong to the paper. One object in each
directory says that: the manifest. A paper exists for every reader once its manifest is committed, and it holds exactly
what the manifest names.

The manifest records two kinds of thing. It records what was *captured*: the source files, each a lineage of revisions
that carries its own licence. And it records what was *derived* from them. Today the only derived artifact is a
rendering, the markdown a reader quotes. The extractions that come next read a rendering and feed each other. A variant
set is extracted from a rendering, a pedigree is built from variant sets, and a case report is written from a pedigree
and the text.

That chain is what shapes the design. Every derived artifact is recorded the same way, as a *record* keyed by the hash
of its bytes, naming the hashes it was made from and how it was made. A chain of any depth is then a path from record to
record. A paper can also hold several candidates at one level, such as a rendering converted from the publisher's XML
and an older one transcribed from a PDF. So the manifest holds *refs* as well: one name per kind, each pointing at the
record a reader should use. The run that produces a record moves the ref when the new record is the better one. The
choice is therefore made once, when the record is written, and every reader follows the same name.

Beside the manifest sits `metadata.pb`, the paper's bibliographic record as each index published it
([The bibliographic record](#the-bibliographic-record-metadatapb)).

## Background

### A paper's directory, and why it needs a manifest

A paper's files are *content-addressed*: each is stored under a name made from the sha256 of its bytes. That has three
consequences the rest of the design leans on. Writing the same bytes twice produces one object, so a write can be
repeated safely. An object never changes after it is written, since different bytes would have a different name. And a
reference to an object by its hash is also a check that the bytes are the ones meant.

What a content-addressed name cannot say is whether the bytes belong to the paper. A writer that crashes halfway through
storing a paper leaves objects behind, and they look exactly like the objects of a finished paper. So one object in the
directory is named by its role rather than its content: `manifest.pb`. It is written last, and only if no manifest is
there yet, so the first writer to finish wins and a second learns it lost. Readers go to the manifest and nowhere else.
An object no manifest names is inert, which is what makes a crashed write harmless.

The manifest is a binary protobuf, defined in
[`litcache.proto`](../../schema/proto/themis/litcache/models/litcache.proto). The examples below show it as JSON for
reading only.

### What the manifest has to say

A paper's primary artifact can change while its identifiers stay the same. PubMed Central reissues an article's XML as
`PMC2992036.1` and later `PMC2992036.2`, and a publisher corrects a PDF without minting a new DOI. The change is to one
file, not to the paper as a whole: a corrected XML leaves the PDF beside it untouched.

Licence varies the same way, file by file. The JATS XML a paper is fetched as from PubMed Central's open-access subset
can carry a CC-BY licence inside it, while a PDF of the same paper supplied by a person's institutional access carries
none. A licence recorded once for the whole paper would be wrong for one of the two.

A paper's text reaches a reader as markdown, and there is more than one way to make it. Converting the publisher's XML
reproduces the paper faithfully. Transcribing a PDF with a vision model gets close, and an older converter got less
close. So the route a rendering took decides how far it can be trusted, and a paper can hold renderings made by several
routes at once.

Quotes are stored against a rendering's bytes, because quoting is done on text. A quote made last month may refer to a
rendering made from a revision the store has since replaced, and it has to keep resolving. It also has to be upgradeable
to the current revision, by finding the same words in the newer text.

### Derived artifacts depend on each other

Renderings are the first artifact the store derives, and they will not be the last. Each extraction that follows reads
something the store already holds and produces something a later step reads in turn. A rendering is made from a source
revision, a variant set is extracted from a rendering, a pedigree is built from variant sets, and a case report is
written from a pedigree together with the paper's text, so it depends on two artifacts of different kinds.

At any level a paper can hold more than one candidate: two renderings, or two variant sets extracted with different
prompts. A step can read only one of them, and which one it read is part of what it produced. A variant set extracted
from an approximate rendering is a different result from one extracted from the faithful one.

## Design

### Captured files are lineages of revisions

The obvious way to record what a paper was fetched as is one entry per paper: its PDF, its XML. That breaks on the first
reissue, because there is then a second XML with the same role and nowhere to put it. A paper-wide snapshot, where each
change produces a new version of the whole record, breaks the other way: it records a change to the XML as a change to
the PDF too.

So each captured artifact is a *lineage*, named by a stable handle such as `xml` or `pdf`, and a lineage holds an
append-only list of *revisions*, one per fetched byte-set. A lineage's current revision is the one captured most
recently, judged by its capture time and never by its position in the list. A reissue appends a revision to the `xml`
lineage and leaves the `pdf` lineage alone. The handles in use are `pdf` and `xml`, one per media type. The handle is an
open namespace rather than the media type, because two lineages could share a media type: XML converted from a scraped
web page would enter as XML under a handle of its own.

Licence and access sit on the lineage, because that is the level at which they vary. They hold across all of a lineage's
revisions and differ between a paper's lineages. A lineage also records where its licence string came from. It was
either read out of the fetched bytes, as a JATS `<license>` element is, or asserted by an access authority because the
bytes carry none. The store keeps the raw string as the fetcher returned it, without mapping it onto an SPDX identifier.

Each revision records where its bytes came from: the open-access source that served them, the seed corpus, a person's
upload, or the open-access location Unpaywall lists for the paper. The last is how a paper ingested by its identifier
alone comes to hold a PDF. Take the preprint `doi:10.21203/rs.3.rs-4272138/v1`. No source serves its XML, so the
conversion lane fetched the PDF at Unpaywall's best open-access location and recorded it as the one revision of a `pdf`
lineage. A PDF carries no licence in its bytes, so the lineage's terms are the ones Unpaywall states for that same
location, `cc-by` and free to read, recorded as asserted. Unpaywall's terms describe one copy of a paper, which is why
they are taken only with the copy they describe.

Because the terms belong to the lineage, a revision joins a lineage only on that lineage's terms. A PDF obtained on
different terms from a PDF the paper already holds, such as an open-access copy beside one captured through
institutional access, is a lineage of its own under a handle of its own.

Two lineages can therefore hold the same bytes. Say a curator supplies a PDF, captured through their library, and the
identical file is later found at an open-access location. The same revision hash then sits in both lineages, under
different terms. A record's dep names bytes, not a lineage, so a rendering transcribed from that PDF resolves to both.
That needs no tie-break, because the bytes are the same: the least restrictive terms of any lineage holding them apply
to them, and to everything derived from them. Free to read outranks every other access, so in the example the rendering
is open access, whichever lineage the transcription read its bytes from. Nothing yet reads more than whether a text is
open, so the order among the restricted kinds of access waits until something does.

Supplementary files, such as figures and data tables, are recorded in a separate list that doubles as the register of
files known but not yet fetched. A paper can list hundreds of them: one paper in the dev corpus lists 340, and more than
half of all papers list some. A known file has a name but no content hash until it is fetched. A fetched file records
the sha256 of its bytes and when it was fetched, and its bytes sit content-addressed beside the paper's other files.

The hash is what lets a derived artifact name a supplementary file as its input, as a variant set extracted from a
supplementary spreadsheet does. That only works if the hash stays listed for as long as anything depends on it. So the
list is append-only. When a publisher reissues `mmc1.xlsx` with a corrected row, the corrected file is a second
`mmc1.xlsx` entry with a later capture time, and the first entry stays. A variant set extracted from the first version
still names bytes the manifest lists.

A file's role is read from the XML rather than recorded. The publisher's original file name is kept, and the XML refers
to files by that name: in a sample of 1,680 papers, 98.5% of their files are referenced, two thirds inside a figure and
a quarter as supplementary material. A figure that the XML refers to by `href` is matched to the list by name when the
XML is rendered.

### Every derived artifact is a record, keyed by its bytes

The obvious way to record a new kind of derived artifact is a map of its own on the manifest, the way renderings were
recorded until now: a map from each rendering's hash to how it was made. Each kind would then be fully typed, and a
reader of variant sets would never see a pedigree. That works for one level. It breaks at the second, because the
artifacts depend on each other across kinds. Each per-kind map has to invent its own way of naming its inputs.
Renderings named theirs by a lineage handle plus a revision hash. A reader following a case report back to the words it
cites would have to learn every kind's scheme on the way down, and every new kind would add a map and a write path of
its own.

So every derived artifact is a *record* in one map on the manifest, keyed by the sha256 of the artifact's bytes. A
record holds three things: the hashes it was derived from (its *deps*), when it was produced, and how it was made (its
*construction*). The construction is a choice with one arm per kind. A rendering's arm names its converter, the
converter's version, and the model where a vision model transcribed a PDF. A dep is a bare hash. Its role is read from
what it resolves to: a source revision, a fetched supplementary file, or a record whose construction says what kind it
is.

Every extraction takes this shape, however many inputs it reads. A knowledge-unit extraction is one document of units,
with the rendering it read as its dep. A variant extraction is likewise one document, with the rendering and any
supplementary spreadsheet it read as its deps. A written description of a figure has two: the figure's image, and the
PDF revision its bounding box was found in. The artifact's bytes sit beside the manifest, content-addressed at a path
its kind determines, exactly as a rendering's markdown always has.

The chain from the Background then looks like this, with toy hashes:

```mermaid
flowchart RL
    R1["revision r1<br/>xml lineage"]
    R2["revision r2<br/>pdf lineage"]
    M1["record m1<br/>rendering: llm-ocr"]
    M2["record m2<br/>rendering: litdown"]
    V1["record v1<br/>variant set"]
    P1["record p1<br/>pedigree"]
    C1["record c1<br/>case report"]
    M1 -->|dep| R2
    M2 -->|dep| R1
    V1 -->|dep| M1
    P1 -->|dep| V1
    C1 -->|dep| P1
    C1 -->|dep| M1
```

Following a case report back to its text is now one walk: `c1` names `m1`, and `m1` names revision `r2` of the PDF
lineage. The walk is the same whatever kinds it passes through.

A new kind needs a new arm in the manifest's schema, which is a contract change and is reviewed as one. That is the
right place for the review, because the construction is the part of a record a later reader relies on to judge it.

A dep does not say which input it was, so a reader that needs to know resolves it. A case report that read both the
paper's rendering and a supplementary file's rendering tells them apart by following each one step further, to the
source it was made from.

Keying by bytes has a cost of its own. Two runs that produce identical bytes produce one record, and the record keeps
only the deps and construction of the run that finished most recently. A rendering re-made by a newer converter version
that happens to output the same markdown therefore records the newer version and loses the older. That is the right
answer for the store's readers. A quote cites bytes, and identical text is the same text to everything that reads it.

### A ref names the record a reader should use

Several records of one kind can sit side by side, and a reader needs one of them. The obvious way to pick it is at read
time: rank the candidates on each read and serve the best. That is how the canonical rendering was chosen until now, by
converter fidelity with recency breaking a tie. It has real virtues. A re-render can never leave the choice stale,
because nothing stored records the choice. And a change to the ranking takes effect on the next deploy.

It breaks at the second level of the chain. Which rendering a variant set was extracted from is part of the variant set,
so the choice of rendering has to be made before the extraction runs and recorded with what it produced. Read-time
ranking also puts the ranking in every reader. One reader ranks renderings by converter, another ranks variant sets by
prompt version, and each has to agree with the writers about what "better" means. The read surface depends on that
agreement: the agent's read, the quote matcher and the document pane have to serve the same text, or a quote validated
against one rendering fails to locate in another ([`literature-evidence-layer.md`](literature-evidence-layer.md)).

So the manifest stores the choice, as a map of *refs* from a name to a record's key. The run that produces a record
moves the ref when its record is better than the one named. Each kind has its own rule for "better"; for an extraction
it might be the newer prompt version. For a rendering it is the fidelity ranking: a rendering converted from source XML
is preferred to a PDF transcription, a vision-model transcription is preferred to the older converter, and recency
breaks a tie. The record and the ref that names it land in one write, and the manifest's schema refuses a ref that names
no record it holds. Nobody moves a ref by hand.

Names are simple. A kind's name refers to the paper itself: `markdown` names the rendering a reader is served, and
`variants` names the current variant set. A supplementary file's record of the same kind is named after the file, as in
`markdown/table-s2.docx`. Each question a reader asks then has a direct answer from the one manifest it has already
read. It looks up the paper's text by name. It reaches each supplementary file's text by walking the manifest's list of
files and looking up each file's name. And it finds every rendering ever made by filtering the records by kind.

A stored ref can point at a record built on an input that has since been replaced. Take the figure above. Rendering
`m2`, converted from the XML, lands after `m1`, so its run moves `markdown` from `m1` to `m2`. The `variants` ref still
names `v1`, which was extracted from `m1`. Nothing is wrong with that. Rendering `m1` is still held, so `v1` and
everything built on it stays consistent, and every quote in it still resolves. But `v1` is no longer built on the
current text, and that is visible: a record is current when each of its deps is the record its kind's ref names, and
`v1`'s dep is not. Records that are not current are the extraction lane's work, found by reading the store rather than
recorded anywhere else. A reader that needs a consistent chain, such as a variant together with the words it cites,
follows deps down from the record it starts at rather than looking up the current ref at each level.

The cost of storing the choice is the read-time design's virtue turned around. A change to the ranking reaches existing
refs only when something recomputes them, where a read-time ranking would have applied it on deploy.

### Path layout

The manifest and the bibliographic record sit at the paper's root, named by role. Everything else is content-addressed,
under a directory for its kind:

```
manifest.pb
metadata.pb
sources/pdf/<rev-hex>.pdf
sources/xml/<rev-hex>.xml
renderings/<md-hex>.md
supplementary/<hex>.jpg
```

A second revision appends `sources/xml/<new-rev-hex>.xml`, and a re-render appends `renderings/<new-md-hex>.md`. Old
objects persist, so old quotes keep resolving. Every hash in the manifest and every filename stem is the same bare
sha256 hex digest, with no prefix and no transform between the two.

`metadata.pb` sits beside the manifest and is the one artifact that may be regenerated after the commit: the manifest
holds no hash of it, and its bytes derive from the paper's identifiers through the resolver ladder rather than from
anything in the directory, so re-deriving it refreshes the bibliographic record while every source and rendering stays.
Which changes that refresh serves, and which need the corpus rebuilt, is decided with the record
([The bibliographic record](#the-bibliographic-record-metadatapb)).

The obvious place to write `metadata.pb` is with the other objects, before the manifest commits, so that a reader never
finds a manifest without its record. That order is safe for the content-addressed objects because each is named by its
content: two writers storing the same bytes produce one object, and two storing different bytes produce two.
`metadata.pb` is named by the `doc_id` alone, and two seeds reach the same `doc_id` when they share an identifier. Say
one seed is keyed by a DOI, and a second seed, keyed by the PMID of a different paper, names that DOI in its Docling
origin by mistake. The crosswalk gives both one `doc_id`. Resolution is keyed by the identifiers each seed carries
rather than by the DOI they share, so the second seed gets its PMID's PubMed record. If both writers pass the manifest
check before either commits, the create-only commit stops the second manifest but not the second record, and whichever
manifest commits can end up beside the other seed's record with nothing to notice it.

So `metadata.pb` is written after the commit, and only from a record of the paper the manifest describes. The writer
whose commit wins writes it straight away. Any other writer that finds the manifest committed, by the manifest check or
by losing the commit, writes the record only when the paper is missing one and the ids its seed claimed are the ones the
manifest records, since only then is its record the one a metadata refresh would resolve. That check covers the loser
that is really the winner, too: a commit whose response is lost is retried by the storage client and fails as if another
writer had won. Whichever of the two seeds above loses claims ids the other's manifest does not record, so it writes
nothing.

The cost is a window in which a committed paper has no record, a state the reader already handles by titling the paper
with its DOI or PMID. If the writer stops inside that window, the record stays missing until the paper is next ingested,
and a metadata refresh fills any paper still missing its record.

The same race costs more when the loser has done real work. Bulk ingestion claims a paper's ids, then spends minutes
fetching and converting before it commits. If an agent asks for the same paper in that window, the on-demand ingest
commits a manifest holding only the paper's ids, and the bulk writer's commit fails. Its PDF and rendering are in the
bucket, but nothing names them. For a paper that is not open access, the conversion lane cannot reach that PDF, so the
paper ends with no text although its seed was converted.

So a writer that finds the manifest committed joins it rather than discarding its work. In one read-modify-write
conditional on the manifest's generation, it adds its sources, renderings and files, and moves the `markdown` ref if a
rendering it adds outranks the one named. It joins only when every id its manifest would record is one the committed
manifest records. The weaker test, that none of its ids contradicts the manifest's, admits the second seed above: its
PMID contradicts nothing in a manifest that records only the DOI, and its PDF would land in the other paper's directory.

An on-demand ingest records the DOI, PMID and PMCID it was asked for or the index returned, which usually covers what a
seed carries. Where it does not, the bulk writer still loses its work: a seed carrying a PMCID the index did not return,
an alias DOI where the index returns the primary one, or a Bookshelf chapter, whose accession the on-demand path does
not record. An open-access paper among them recovers when the conversion lane renders it again.

A join can leave a paper holding two seeds' work where a sequential run would have kept one: two PDFs of the same paper,
each with its rendering, when both seeds reached the `doc_id` concurrently. The ranking picks the rendering served, as
it does for any paper holding more than one.

### A change to the manifest's shape pauses the writers instead of carrying both forms

[`proto.md`](proto.md#schema-evolution) sets the rule for a change to a stored record that the compatibility gate would
refuse. The new form lands as a new field beside the old one, every stored record is rewritten into it, and only then is
the old field retired, with its number and name reserved. The usual way across the gap is for every reader to accept
either form for a while, and for every writer to fill both.

For the manifest, that period would be code written only to be deleted. The manifests live in one bucket, and the only
code that reads or writes them is code this repository deploys. So the gap is crossed by ordering the steps instead:

1. Every manifest writer that can be paused pauses: the conversion queue is paused, and no bulk ingestion runs. A
   metadata refresh writes only `metadata.pb`, so it can run throughout. The evidence service's on-demand ingest cannot
   be paused, because it runs whenever a caller asks for a paper the store does not hold, so it keeps writing manifests
   in the old form until the new code is deployed.
1. A one-off tool rewrites each manifest into the new form, leaving the old field in place, so readers still running the
   previous code keep working.
1. Readers and writers that use only the new form deploy. Once the rollout has finished, the rewrite runs again, to
   catch any manifest the on-demand ingest wrote in the meantime, and a check confirms that no manifest is left in the
   old form.
1. Writers resume. The old field is then retired, and the tool is deleted with it.

Renderings moved into records this way. Each manifest had held them in a map of their own, and the rewrite copied each
entry into a record, set the `markdown` ref by the fidelity ranking, and filled each fetched supplementary file's hash
from the path its bytes are stored at, which already names them. One manifest's renderings, before and after:

```json
"renderings": {
  "dd4306e5…": { "from_source": "xml", "from_revision": "647ab726…", "converter": "litdown", "converter_version": "0.3.0" }
}
```

```json
"records": {
  "dd4306e5…": { "deps": ["647ab726…"], "rendering": { "converter": "litdown", "converter_version": "0.3.0" } }
},
"refs": { "markdown": "dd4306e5…" }
```

Every quote survived the move, because a record's key is the same markdown hash the map used.

The rewrite replaces each manifest with a read-modify-write conditional on the manifest's generation, so a manifest a
writer touches during the run is read again rather than overwritten. The bucket keeps each object's earlier generations
for 30 days, which is the way back until writers resume; after that, a manifest written by the new code is one the
previous readers cannot read. The cost is the pause: conversions wait in the queue for as long as it lasts, rather than
run.

## The bibliographic record: `metadata.pb`

Beside `manifest.pb`, at the paper's root rather than under a content hash, sits `metadata.pb`: the paper's
bibliography, meaning its title, authors, journal, abstract and the identifiers its indexes list. The manifest says what
we hold of a paper and under what terms; `metadata.pb` says what the paper is. The indexes' records in it are
regenerated wholesale from the indexes, never edited in place ([`proto.md`](proto.md), bucket 1), and Themis's own
metadata fields beside them are recomputed from them. This section decides what the record holds, what makes ingest
refuse one, and which fields a consumer reads from it.

**Each index's record is stored as that index states it.** A paper's metadata is stored as the record its index
published, rather than as a subset of its fields chosen at write time. A subset carries only what its author foresaw
needing, and what it dropped is gone until someone re-fetches; the whole record costs kilobytes and answers questions
nobody has asked yet. The same test rules out storing one index's record mapped into another's schema. A Crossref work
squeezed into `PubmedArticle` keeps only the fields PubMed happens to have a slot for, and a reader can no longer tell
which index answered ([`literature-evidence-layer.md`](literature-evidence-layer.md), a discovery rpc is a query against
one named source). How each record is held differs by index, for reasons the paragraphs below give: PubMed's as a typed
proto generated from NLM's schema, Crossref's and OpenAlex's as JSON text, re-serialised from the value each served.

**Metadata, and its source record.** `metadata.pb` holds a `SourcedMetadata`
([`litcache.proto`](../../schema/proto/themis/litcache/models/litcache.proto)) in two parts: the paper's `Metadata`,
which is what a consumer reads, and the metadata's source record, a `MetadataSource`, which holds the record of the
index that resolved the paper, with OpenAlex's record of the same work beside it, and is what the metadata is filled
from. It is unrelated to two messages of the manifest with similar names: a `Source` is a captured file and its
revisions, and a `Record` is a derived artifact. The two parts are separate messages so that a consumer can be handed
`Metadata` alone. A consumer handed the whole record could reach into the source record for whatever the metadata lacks,
and each such reach is code that handles index records without knowing which index answered. Given `Metadata` alone, it
cannot do that by accident; a reader that needs the source record is given it on purpose. Until the move off the mirrors
(below) lands, `metadata.pb` holds the older `PaperMetadata` envelope, which carries the index's record alone, and
Crossref's and OpenAlex's records also in hand-written protos of each index's keys, called *mirrors*. Here is a preprint
that PubMed does not index, resolved through OpenAlex (values shortened):

```
SourcedMetadata                                metadata.pb, binary proto
├── metadata         Metadata                  what a consumer reads
│     ├── title             "Opportunistic partner choice among arctic plants…"
│     ├── authors           [{name: "Bastien Parisy", orcid: ""}, …]
│     ├── year              "2024"
│     ├── container_title   "bioRxiv (Cold Spring Harbor Laboratory)"
│     ├── abstract          "Interactions between plants and soil microbes…"
│     ├── mesh_headings     []    (OpenAlex states MeSH only for a work with a PMID)
│     └── publication_types ["Preprint"]
└── source           MetadataSource            what the metadata is filled from
      ├── pubmed          PubmedRecord         (not set: PubMed does not index it)
      ├── crossref_json   string               (not set)
      └── openalex_json   string               '{"id":"https://openalex.org/W4401…","title":"…",…}'
```

**The resolving index's record, and OpenAlex's beside it.** A paper is resolved by a ladder that stops at the first
index that answers. Bulk ingest and the refresh ask PubMed, through efetch for a PMID that is given or that litfetch
finds for a DOI, and then OpenAlex. When litfetch finds no PMID for a DOI, or gives up on it, the bulk ladder asks
OpenAlex for the DOI with Themis's key, and a work that states a PMID sends the paper to PubMed as litfetch would have.
Where PubMed's record for that PMID states another DOI, the two indexes disagree about which paper the DOI names, and
OpenAlex's work resolves the paper instead, so no DOI is claimed for another paper's record. The evidence service's
on-demand ingest asks PubMed and then Crossref. Whichever index resolves the paper, the writer then asks OpenAlex for
the same work by its PMID and, when OpenAlex holds no work under the PMID, by its DOI, which catches a work OpenAlex has
not yet linked to its PMID. A work found by the DOI that states another PMID is taken for a different paper's, and the
paper keeps its resolving index's record alone. A paper's authors are taken from OpenAlex's record wherever it has one,
as described below. So a source record holds the resolving index's record and, beside it, OpenAlex's record of the same
work. The exceptions are a paper OpenAlex resolved itself and a paper OpenAlex holds no work for. A protovalidate rule
on `MetadataSource` requires at least one record, and at most one of PubMed's and Crossref's.

Each branch of the two ladders and of the lookup that follows them ends in one of the outcomes below.

```mermaid
flowchart TD
    P["paper: a PMID or a DOI"] --> W{"which writer?"}
    W -- "bulk ingest, refresh" --> L{"PMID given, or<br/>found by litfetch?"}
    W -- "on-demand ingest" --> D["PubMed, then Crossref"]
    L -- "yes" --> E["PubMed via efetch"]
    L -- "no: missed or gave up" --> X["OpenAlex by DOI,<br/>with the key"]
    X -- "work names PMID P, and PubMed's<br/>P names no DOI or this one" --> E2["PubMed via efetch,<br/>the work found kept"]
    E2 --> B
    X -- "work names no PMID, or<br/>PubMed's P names another DOI" --> OA["stored: OpenAlex's record alone"]
    E --> C{"OpenAlex lookup: by PMID,<br/>then by DOI on a miss"}
    D --> C
    C -- "one work" --> B["stored: resolving record,<br/>OpenAlex's beside it"]
    C -- "no work, or the DOI's<br/>work names another PMID" --> R["stored: resolving record alone"]
    C -- "several works" --> DL["dead-lettered, naming them"]
    C -- "OpenAlex down" --> F["write fails; the caller retries"]
```

The lookup does not always find one work. When OpenAlex cannot be reached or answers with a server error, the write
fails, as it does when the resolving index cannot be reached, and the writer's caller retries it; no paper is stored
without the OpenAlex record it should carry. When OpenAlex holds no work for the paper's PMID or DOI, the paper is
stored with its resolving index's record alone. When OpenAlex holds more than one work for the identifier, the paper is
dead-lettered, naming the works, because taking either could give the paper another work's authors. A response that
cannot be an answer to the lookup, such as a work stating none of the identifiers asked for, fails the whole lookup, and
with it a bulk run, as a malformed efetch response does. A list of works names at most a work's first 100 authors, where
a consortium paper can have over a thousand, so a work listed with 100 is fetched again on its own and stored whole.

Every writer sends Themis's OpenAlex API key, which gives Themis a daily budget of its own. Without a key, OpenAlex
shares one small daily budget among every caller behind an IP address, about a thousand requests in October 2026. The
services of an environment reach OpenAlex through one address, so another caller could spend that budget, and every
write would then fail until it resets. A request carries up to a hundred identifiers, so refreshing a store of 41,000
papers costs about 450 requests. litfetch's DOI resolution, which the bulk ladder runs before OpenAlex, still asks
OpenAlex without the key.

The obvious way to say in a proto which records may be set is a `oneof`, which also gives readers a generated accessor
for the arm that is set. A `oneof` holds one arm at a time, so it cannot hold PubMed's record and OpenAlex's together; a
constraint states the combination instead. The fields stay one per index, so a further index is a new field and changes
no reader of the others.

**Crossref's and OpenAlex's records are held as JSON text.** The obvious way to store a JSON record in a store of protos
is a proto per index whose fields are the index's keys, which gives every reader typed fields, generated stubs and
protovalidate. Neither index publishes a schema that a generator can turn into such a proto. Each publishes an OpenAPI
description of its API, and each description lags and mis-states the records the API serves; OpenAlex's misses about a
dozen top-level keys. So the proto has to be written by hand against live records, and a hand-written schema of someone
else's records has to follow every change they make. If the loader refuses a key the proto lacks, nothing lossy is
written, but each key the index adds stops ingestion through that index until the proto gains the field. If the loader
drops such a key instead, every record written in the meantime loses it for good.

OpenAlex adds keys often, so this choice comes up in practice. A strict proto of OpenAlex's work dead-letters every
paper resolved through OpenAlex as schema drift from the day OpenAlex starts serving a key the proto lacks until the
proto gains it, whether or not anything reads the key. In September 2026 four top-level keys arrived together
(`study_designs`, `x_sdgs`, `observed_orcids` and `listed_in`, #672). Three weeks later three more did: a `doi` on every
location of a work, `volume`, and `sustainable_development_goals_aurora`. Each time every OpenAlex ingest failed, and
nothing in Themis reads any of these keys. Keeping a schema in lockstep with an index we don't control costs more than
the typed fields buy, when most of those fields have no reader.

So the source record holds each of these two records as JSON text: the index's work object, re-serialised from the
parsed response with nothing dropped or renamed. The text parses back to a value equal to the one the index served; it
need not match the bytes the index sent. A key the index adds is stored like any other, and a question nobody has asked
yet can still be answered from the stored record. The field is a `string` holding the text, rather than a
`google.protobuf.Struct`, because a `Struct` stores every number as a double ([`proto.md`](proto.md), Upstream JSON held
as text). The cost is that a reader of these records gets no generated type. It reads the JSON through a reader written
for the fields it uses, described next.

**Ingest fails only on the fields Themis reads.** Each JSON index has a reader for the fields Themis takes from its
record: the title, the authors and their ORCID iDs, the year, the container title, the abstract, the MeSH headings and
the type that fill the metadata fields below, the DOI, the PubMed and PMC identifiers an OpenAlex work lists, and the
publisher, which the manifest records for a licensed source. From an OpenAlex record held beside another index's, the
reader reads only the authors: each authorship's profile name and iD. The readers are the one place these field names
and shapes are spelled out. A record fails when a field every record of that index states is missing, such as the
`title` key an OpenAlex work carries even when its value is null, or when a field the reader reads has a shape the
reader cannot read. The paper is then dead-lettered as schema drift, and the dead letter names the field. A field an
index may leave out, such as the PMID of a preprint, reads as not stated. Every other key stays in the text and never
fails anything.

Take two OpenAlex works. The first carries a key Themis has never seen; it is stored like any other work, with the key
in its text. The second states its PMID as an object where the reader expects a URL string; it is dead-lettered, naming
the PMID as the drifted field. Drift of the second kind needs a change to one of the handful of fields Themis reads,
rather than to any key the index adds.

The readers run only where the metadata fields below are filled: at ingest, at a refresh, and in a fill of the metadata
fields, which brings the stored fields up to a new rule. Serving never runs them, because a consumer reads the stored
metadata fields. The code check on the source record confirms only that each JSON record present is the text of a JSON
object. So when an index reshapes a field a reader reads, the records stored in the old shape keep serving their
metadata fields. They fail when a fill reads them again, and the fill reports each one by paper and field. There are two
repairs. The reader can accept both shapes, which keeps the stored records readable at the price of a branch in the
reader for as long as such records exist. Or the index's stored records are deleted and refreshed in the new shape.
OpenAlex's record sits beside most papers, so a reshaped OpenAlex author field leaves nearly every stored record in this
position, and the repair by deletion is a refresh of nearly the whole store. A reader that starts requiring a field the
stored records lack faces the same choice.

**Drift is reported where it is met.** A drifted paper stays a dead letter. In a bulk ingest or a refresh, the run lists
it in its report, which the operator who started the run reads. When the evidence service ingests a paper on demand, the
drifted record is charged to its id alone: the caller is told the store has no such paper, and only the service's log
names the field
([`evidence-fulltext.md`](evidence-fulltext.md#an-id-the-corpus-does-not-hold-is-brought-into-the-store)). Nothing
watches that log, so on-demand drift goes unseen until someone looks. Drift needs an index to reshape one of the few
fields Themis reads, so it should be rare. But OpenAlex's author fields are read for most papers, so one such reshaping
would stop on-demand ingest for nearly every paper, unseen until someone looks. Whether that warrants an alert is an
open question below.

**PubMed keeps its generated, typed record.** The lockstep argument does not apply to PubMed. NLM publishes a DTD for
its records, versions it, and changes it about once a year with notice. `pubmed-proto` generates the proto and its XML
converter from that DTD ([`proto.md`](proto.md), Generated upstream schemas), so a new DTD is a pin bump rather than a
hand edit, and holding PubMed's record typed costs nothing to maintain. It is also the record kind the literature
interface returns for a PMID, through the same converter, so a run's triage read and the store's copy have one shape.
They are not the same bytes, because the store's copy is taken at ingest and the run's at query time, and indexes revise
records.

The typed record is not quite the whole of what NLM sends. The converter steps over a few elements of an article without
keeping them, such as its languages and its pagination, so those are absent from the stored record. Themis reads only
the metadata fields and the identifiers from PubMed's record, so the typed form is kept because it is free, not because
a reader needs its types.

**PubMed's record has two kinds, and its field says which.** Most PMIDs name a journal article, but a PMID can also name
a book NCBI hosts on its Bookshelf, or a chapter of one. A GeneReviews chapter, the expert-written summary of a gene or
condition that a variant analysis cites routinely, is the common case. PubMed's schema gives the two kinds different
records, `PubmedArticle` and `PubmedBookArticle`, that share no top-level shape: a journal record hangs off a citation
with a journal, a book record off a document with a book. Protobuf's wire format carries field numbers, not a type, so
bytes read as the wrong message may decode without error into a record that means nothing, and a store that can hold
either has to write down which one it holds. The source record's `pubmed` field is therefore a message of its own,
`PubmedRecord`, whose `oneof` has an arm per kind and requires exactly one set, the structural move the access
disposition on a lineage makes too. The exclusivity is PubMed's own rule, a PMID names one record, stated in the type;
it is why the two kinds are arms of one field where other indexes are fields of their own.

**The metadata fields are stored, and filled from the record.** Consumers want a bibliography in one shape, such as a
title, a year and an author list, without switching on which index answered. The obvious design computes that shape on
read, in one function beside the proto. It stores nothing extra, and a change to the rule, such as a chapter with no
title of its own falling back to its book's, ships as a deploy. The cost is that every read parses the index records and
runs their readers. Say OpenAlex starts serving a work's PMID as an object rather than a URL string, and the reader is
changed to read the object. Every OpenAlex record stored before the change still holds the string, so describing any of
those papers fails with `INTERNAL`. The failure lands at serving time, in front of a curator, for a field that has
nothing to do with the title they asked for.

So `metadata.pb` stores the `Metadata` beside its source record, and the readers fill it where a record is written: at
ingest and at a refresh. A consumer reads the `Metadata` alone, and is handed nothing else. A change to the rule that
fills them then needs, after its deploy, a fill of the metadata fields: a run that recomputes the fields of every stored
record. The fill reads the records already stored, so it needs no network and touches nothing but `metadata.pb`, and the
store already rewrites its manifests the same way
([A change to the manifest's shape](#a-change-to-the-manifests-shape-pauses-the-writers-instead-of-carrying-both-forms)).
The fill arrives with the move off the mirrors (below), the change that first stores the fields.

A reader coming from [`literature-evidence-layer.md`](literature-evidence-layer.md) will recognise the risk it names: a
shape invented to be common across indexes carries only what it has a field for, bends an index's values to mean what
that index never stated, and invites code that handles records without knowing which index answered. These fields are
exactly such a shape, and some of their readers decide things from them, as described below.

The shape keeps that risk small. Nothing is lost, because the indexes' records stay beside the fields, whole and
unmapped, and the fields are filled from them again whenever the rule that fills them changes. Text is plain text
whichever index answered: take a title PubMed states as `*BRCA1* variants` and Crossref as `<i>BRCA1</i> variants`;
either way the title field holds `BRCA1 variants`, and the italics survive in the record it came from. And every
identifier or term comes from one named scheme, ORCID's for an author and NLM's for a MeSH heading or a publication
type, so code matching the heading `D006801` (Humans) or the type Case Reports needs no knowledge of the index. What an
index cannot state in that scheme is left empty. Two things approximate, both described below: OpenAlex's major-topic
marks, and the table that maps Crossref's or OpenAlex's publication type onto NLM's.

There are seven metadata fields: the title, the authors, the year, the container title, the abstract, the MeSH headings
and the publication types. The first five describe a paper to a person. `DescribePaper` reads the title today, and the
other four are for the workspace's UI surfaces that show a paper, such as the one that helps a curator decide which
papers to upload by hand because the corpus cannot fetch them itself: its `DescribePaper` returns a paper's authors,
journal, year, DOI and abstract ([`supplied-papers.md`](supplied-papers.md)). The MeSH headings and the publication
types are for the case-record pipeline's triage, which decides which papers are worth its expensive extraction passes; a
paper typed Case Reports, or headed Humans rather than Animals, answers much of what the triage would otherwise ask a
model to judge from the pages. An author's ORCID iD serves both kinds of reader. The UI links the author to their
profile. A check for two papers that may report the same patients compares the iDs of their authors, so that a proband
is not counted twice. The DOI is not a metadata field, because the manifest's external ids hold it already. One function
fills all seven from the source record: the authors from OpenAlex's record wherever the source holds one, and every
other field from the record of the index that resolved the paper.

Every text field is a plain string, empty when the record states nothing; the authors and the MeSH headings are repeated
messages and the publication types repeated strings, each empty when the record states none. The year could have been an
integer, which would check its own form; it is a display string instead, as `EuropePmcRecord.year` is
([`literature.proto`](../../schema/proto/themis/rpc/literature.proto)), because nothing sorts or computes on it and the
empty string says "not stated" without a presence bit, as for the other text fields. No field needs a presence of its
own to mark the fill: a filled record is a `SourcedMetadata` carrying its `Metadata`, which protovalidate requires.

Every text field holds plain text, whichever index answered. Crossref and OpenAlex mark up titles and abstracts with
JATS or HTML tags (`<i>BRCA1</i>`, `<jats:p>`), and PubMed's converter turns the same markup into a markdown of its own
(`*BRCA1*`, `GeneReviews^(®)`). The fill strips both and collapses whitespace, so a consumer shows a field as it is,
without knowing which index it came from. An abstract keeps its section labels as text ("Background: …"), the form
PubMed's structured abstracts take.

An author is a display name and, where one is known, an ORCID iD. The name is "Given Family" or a collective name,
rather than a family name and given names held apart. OpenAlex states only a display name for each author, so a display
name is the one shape all three indexes can produce. A citation style that abbreviates given names to initials, such as
Vancouver's "Doe JA", would need structured names, which would be new fields of the author. The container field is
`container_title`, Crossref's term, rather than `journal`, because a GeneReviews chapter's container is the book.

The authors come from OpenAlex's record wherever the source holds one. The obvious source is the resolving index's
record, which fills every other field and needs no second lookup. It falls short for the check for papers reporting the
same patients, which compares authors across papers and so has to know which authors are the same person. PubMed states
an iD only where the author deposited one with the paper: 21% of the authors in a sample of the store's PubMed records
carry one. OpenAlex groups every authorship into an author profile and gives the profile's iD, which covered 50 to 63%
of the authorships in samples of OpenAlex works.

Taking the whole author list from OpenAlex keeps each iD on the name it belongs to, with nothing to match between the
two records. They agree closely on who the authors are. In 100 works both indexes hold, 99 list the same number of
authors and 96 the same family names in the same order. The other 3 differ only in script, such as PubMed's "Manisali"
and OpenAlex's "Mehmet Manisalı".

The cost is OpenAlex's errors of attribution. All 45 iDs PubMed states in those 100 works agree with OpenAlex's, but
OpenAlex also gives iDs where a paper states none, among them iDs on papers from 1983 and 1996, years before ORCID began
issuing them in 2012. A wrongly merged profile puts one person's iD on another person's paper. The check accepts that,
because it raises a hint that someone then examines: a false overlap costs a closer look, and a missed one counts a
proband twice. The name is the profile's too, and can differ from the paper's byline, in script as well as spelling. And
since most papers carry an OpenAlex record, OpenAlex reshaping a key the author reader reads fails the ingest of most
papers, rather than only of the papers OpenAlex resolved.

Where the source holds no OpenAlex record, the names and iDs are the ones the resolving record states. So the iD field
holds OpenAlex's attribution for most papers, and the paper's own statement for the rest. Either way the iD is held in
its bare form (`0000-0002-1825-0097`): PubMed states it bare, Crossref and OpenAlex as an `https://orcid.org/` URL. An
iD whose check digit fails is left empty, as not stated; the record keeps what the index sent.

A MeSH heading is NLM's controlled subject index for a paper: a descriptor, such as Breast Neoplasms (`D001943`),
narrowed by qualifiers, such as genetics (`Q000235`), each marked as a major topic of the paper or not. PubMed's record
is their source; OpenAlex copies PubMed's into a work that has a PMID, and Crossref has none. A term is held as NLM's
identifier alone, which is what code matches on and all that PubMed's converter keeps. A reader that shows a heading
names it from NLM's MeSH vocabulary, a reference resource of its own, so the fill depends on nothing but the source
record. OpenAlex flattens a heading into one row per descriptor and qualifier, often repeated and in an order of its
own, with one major-topic mark per row. The fill groups the rows back into headings. OpenAlex's mark follows only the
descriptor's own mark in PubMed's record, so the fill marks a descriptor major when any of its rows is marked, and marks
no qualifier. A heading PubMed marks major only through a qualifier, such as Breast Neoplasms with the mark on genetics,
is unmarked when filled from OpenAlex. In 80 works PubMed also holds, 169 of the 269 headings PubMed marks major were
marked only through a qualifier, and OpenAlex marked none of their rows.

A publication type is one of NLM's Publication Types, the part of MeSH that says what kind of publication a paper is:
Journal Article, Case Reports, Review, Preprint. PubMed states one or more for an article, and the field keeps them all,
as the names PubMed states. They include funding facets such as "Research Support, Non-U.S. Gov't". The field holds
names rather than NLM's identifiers, unlike the MeSH headings, because its readers asked for the names PubMed shows. The
cost is that NLM renames a type now and then, as Retraction of Publication became Retraction Notice, so code that
matches on a type has to match every name it has carried. OpenAlex and Crossref each state one type, in a coarser
vocabulary of their own, and a short table maps it onto NLM's: OpenAlex's `review` becomes Review, its `article` and
Crossref's `journal-article` become Journal Article, and a type with no NLM counterpart, such as a book chapter, maps to
none. This mapping approximates. Crossref has no type for a letter or an editorial, so either arrives as
`journal-article` and is typed Journal Article, where PubMed would have typed it Letter or Editorial. The index's own
type stays in its record.

For a reader that decides, an empty MeSH list or a missing publication type is not a negative. NLM assigns headings only
to a paper it indexes for MEDLINE, which leaves out the journals MEDLINE does not cover and the weeks before indexing.
In a sample of 295 PubMed records from the store, the 78 without headings were all ones NLM had not indexed for MEDLINE:
72 that PubMed marks as outside MEDLINE, 5 supplied by their publisher, and 1 in process. Yet 23 of the 72 were typed
Case Reports. Neither OpenAlex nor Crossref has a type for a case report, so a case report they resolved is typed
Journal Article at most. A triage that skipped every paper without Humans, or without Case Reports, would skip those
papers for what their index has not stated.

The rule that fills the fields is not recorded beside them. A changed rule means another fill of every record, which
reads only the records already stored and takes minutes, so nothing has to find the records an older rule filled. Until
the move off the mirrors completes, the title a consumer reads is derived on read from the record.

**A source record with no index's record is corruption.** The constraint on which records are set is enforced where the
record crosses a boundary: the writer validates `metadata.pb` before it writes, and a reader that parses one fails
rather than reading it as a paper without metadata. A paper no index has a record for is not ingested at all, because
the resolve ladder dead-letters it. So an empty source record never means "metadata not yet resolved"; it means the
bytes are wrong.

**A book record carries an identifier of its own.** Beside the PMID, Bookshelf names a chapter by an accession (`NBK…`),
and the accession is how the chapter's text is addressed: Europe PMC serves a chapter's BITS XML under its accession at
the `bookXML` endpoint — a different endpoint and corpus from the JATS article XML behind the plain Europe PMC source
kind. The lineage it produces records a distinct source kind for two reasons: the producer maps the upstream that served
a body onto a `SourceKind` and refuses a body from an upstream it has no kind for as a permanent anomaly, so a new
upstream needs a member; and the member records which corpus the bytes came from. The accession joins the manifest's
external ids (`ExternalIds.bookid`) on the footing every external id there has: an id in the manifest is one the
crosswalk claimed — that is what keeps the crosswalk rebuildable from the manifests alone — so the accession is minted
like the others rather than merely recorded.

**A change to `metadata.pb` reaches the stored records through a refresh, a fill or a rebuild.** The compat gate holds
`litcache.proto` to additive evolution ([`proto.md`](proto.md#schema-evolution)), so every reader already parses the
records written before an additive change. What such a change can still need is the stored records brought up to it,
because a field `metadata.pb` gains is empty in every record written before it. Every `metadata.pb` in the store is
re-derivable from its indexes (PubMed's XML, Crossref's or OpenAlex's JSON) through a resolve ladder. A refresh takes
the bulk ladder, which does not ask Crossref, so a paper the on-demand ingest resolved through Crossref loses Crossref's
record: it comes back with OpenAlex's record alone, or with none if OpenAlex has no record of it. So an index's record
is brought up to a change by re-deriving it, not by rewriting stored bytes or teaching readers a second decoding, and
Themis's own fields by recomputing them from those records. Three operator runs do that
([`reingest-literature-seed-corpus.md`](../runbooks/reingest-literature-seed-corpus.md)), and which one a change needs
turns on what else has to change. Until the move off the mirrors (below) lands, the stored records carry no metadata
fields, so only the refresh and the rebuild apply:

- A *metadata refresh* resolves a bibliographic record, through that ladder, for every committed paper that has no
  `metadata.pb`: efetch for a PMID, and litfetch's DOI resolution and then OpenAlex for the rest. It never touches a
  record that exists, so bringing the stored records up to a change starts with the operator deleting them
  ([Refreshing the bibliographic records](../runbooks/reingest-literature-seed-corpus.md#refreshing-the-bibliographic-records)).
  Manifests, conversions and `doc_id`s stay untouched. The record derives from nothing in the paper's directory, and
  nothing there derives from it ([Path layout](#path-layout)), so it is the one artifact a committed paper can have
  re-derived. The refresh is the path for a change to the index's record, such as a field the source record gains.
- A *fill of the metadata fields* recomputes Themis's own fields of every stored record from the indexes' records it
  holds, with a runbook procedure of its own. It never edits those records and asks no index, so it is the path for a
  change to the rule that fills the fields, or for a field the `Metadata` gains.
- A *full rebuild* — the crosswalk table truncated, the `papers/` prefix cleared, the seed re-ingested on Dataflow — is
  the path when the conversions themselves must change: ingestion skips a paper whose manifest exists, and nothing
  regenerates a committed paper's sources or renderings in place.

After an additive change, none of these runs leaves the readers a fallback to carry. Every record the store holds parses
under the current schema, because the gate holds every schema change to additive, and a change merged past it first
deletes every record it would strand. A destructive change to a stored record needs more, and how much turns on whether
the record can be re-derived. A manifest or a conversion holds what the store keeps and only a full rebuild could
produce again, so a destructive change to one is an expand/contract change, as [`proto.md`](proto.md#schema-evolution)
sets out for every at-rest contract: the new form lands beside the old one, a rewrite brings every record to it, and
only then is the old field retired, with its number and name reserved. A `metadata.pb` is re-derived by a refresh, so a
destructive change confined to it may go in one step, under the condition [`migrations.md`](migrations.md#how-it-runs)
puts on a destructive change: its failing window costs nothing real.
[The move off the mirrors](#the-move-off-the-mirrors) is such a change, and sets out when the condition holds and how
the steps keep that window empty. A refresh fetches each record again, as its index states it at the time of the refresh
rather than at ingest. A paper the index no longer resolves comes out of a refresh with no `metadata.pb`, and so does a
paper OpenAlex holds more than one work for, or one whose OpenAlex record has drifted in a field the reader reads.
`DescribePaper` then titles each by its DOI or PMID.

Changing what a field means in place is never open. Protobuf bytes carry field numbers and wire types, not names or
meanings, so a record written before the change would decode without error into the new meaning.

### The move off the mirrors

Crossref's and OpenAlex's records were first held in hand-written protos of each index's keys, called *mirrors*, inside
a `PaperMetadata` envelope that held the index's record alone. The move off them writes every `metadata.pb` as a
`SourcedMetadata`, deletes the `PaperMetadata` message, the mirror protos and their loader, has ingest take a paper's
identifiers from readers of the JSON text, stores OpenAlex's record beside the resolving index's, and moves consumers to
the `Metadata`. It is a destructive change confined to `metadata.pb`, so it goes in one step, as the previous section
allows, in this order:

1. Start no bulk ingest and no metadata refresh, and delete every `metadata.pb`. Bulk ingest resumes only after step 4's
   refresh has finished, and only from a job built with the move, since a job built before it writes records in the old
   shape.
1. Deploy the change, and wait for the rollout to finish.
1. Delete every `metadata.pb` again. The evidence service's on-demand ingest (`MaybeIngestPapers`) cannot be paused
   ([A change to the manifest's shape](#a-change-to-the-manifests-shape-pauses-the-writers-instead-of-carrying-both-forms)),
   so during the rollout the old revision may write records in the old shape. The second deletion removes any such
   record. A record the new revision wrote is re-derived by the refresh like the rest.
1. Run the metadata refresh until it exits 0, then check with the fill of the metadata fields' verify mode that every
   record is in its final shape.

The failing window runs in both directions while the rollout lasts, because both revisions serve and neither reads the
other's records. A record the old revision writes fails to read on the new revision until the second deletion removes
it. A record the new revision writes fails to read on the old revision, so a request that reaches the old revision for
that paper fails until the rollout finishes. Between a paper's deletion and the refresh reaching it, `DescribePaper`
titles it by its DOI or PMID. A paper the refresh dead-letters stays so titled after the move, and the refresh's report
lists each one. The refresh rewrites PubMed's records as well: pubmed-proto 0.4.0's converter escapes the markdown
markers a title or an abstract states literally, so a record its 0.3.0 converter wrote cannot be read back to its text
exactly.

Rolling back means redeploying the previous revision and waiting for its rollout, then deleting every `metadata.pb`, so
that no record in the new shape is left for the previous revision to fail on, and then restoring each record's
generation from before step 1. The bucket keeps those generations for 30 days as noncurrent versions. A paper first
ingested during the move has no such generation, so it stays without a record until a refresh on the previous revision
resolves one.

The move deletes the `PaperMetadata` message and the two mirror files.
[`proto.md`](proto.md#retiring-an-rpc-or-a-message) keeps a retired message under the models trees declared, because
bytes already written could still need it, and the compat gate enforces that with no in-tool override. So the change
that makes the three deletions fails the required `backward-compatible` check on them alone, and an admin merges it past
the check ([`review-policy.md`](review-policy.md)). The deletions are open only because no production environment holds
this store yet. The one environment that does, dev, meets the condition [`migrations.md`](migrations.md#how-it-runs)
puts on a destructive change: it holds no data worth keeping and has no users to fail. Every stored record is deleted
before the deploy and again after it, so none holds a `PaperMetadata` or a mirror once the new revision reads the store.
Once a production environment holds the store, that condition no longer holds. From then on, proto.md's rule binds
`metadata.pb` as it binds every other at-rest contract: a retired message stays declared, and a destructive change no
longer goes in one step.

## Quote-reference model

The cite model the knowledge-unit layer anchors against. The manifest side is settled: a quote names a rendering by its
record's key, the hash of the markdown it was taken from. The anchor and the offsets cache below are provisional.

- **Durable anchor** (write-once, in the knowledge-unit record): `{paper_id, document_id, quote, exact}`, where
  `document_id` is a rendering's key. It holds no offsets, because offsets go stale on a re-render. The verbatim `quote`
  is the durable anchor instead. A quote is not immortal, since the source text itself can change, but it degrades
  detectably (`exact -> fuzzy -> unlocatable`) where offsets would silently point at the wrong words.
- **Exported without the quote** as `{paper_id, document_id, ref_id}`. The quote stays in the knowledge-unit record, so
  a share carries no copyrightable text.
- **Resolved-offsets cache** (derived, recomputable, mutable): `(ref, rendering) -> spans`, with a status of `exact`,
  `fuzzy` or `unlocatable`. Any offset hint lives only here, never in the durable anchor. Re-aligning can move the
  status either way: a `fuzzy` hit can recover to `exact` against a better rendering.
- **Resolution walk:** the rendering's key → its record → the record's dep, a source revision → that revision's bytes →
  re-align the quote. A PDF with a text layer gives a bounding box from its characters; otherwise the quote maps into
  the XML or text.
- **Upgrade across revisions:** from the rendering's record, find the lineage its dep belongs to and that lineage's
  latest revision. If it is newer, re-render by the same route over the latest revision and re-align the verbatim quote.
  `unlocatable` is the explicit signal that the source changed under the quote.
- **Two ways a quote is minted, one model:** the knowledge-unit extractor aligns its quotes offline. When the agent
  cites, the read tool serves markdown together with its rendering's key, verifies the agent's quote verbatim against
  the served bytes (rejecting a quote the text does not contain), and mints `{paper, document, quote}`. That also seeds
  the offsets cache, since the rendering is known and current.

### Reference and anchor types

Concrete shapes for the cite model. These live in knowledge-unit records and a derived cache, not in the manifest, and
are provisional: `ref_id`, the status values and the span type are not yet frozen.

```proto
message SourceAnchor {
  string paper_id = 1;     // doc_id
  string document_id = 2;  // a rendering's record key
  string quote = 3;        // verbatim, against the rendering's bytes
  bool exact = 4;          // false once only fuzzy realignment held
}
message SharedAnchor {     // exported form — quote stripped
  string paper_id = 1;
  string document_id = 2;
  string ref_id = 3;
}
message ResolvedSpan { int32 start = 1; int32 end = 2; }
message OffsetsCacheEntry {
  string ref_id = 1;
  string rendering = 2;    // rendering key the spans were computed against
  repeated ResolvedSpan spans = 3;
  string status = 4;       // "exact" | "fuzzy" | "unlocatable" — a proto-canonical enum once frozen
}
```

## Alternatives considered

- **A derived artifact stored beside the paper under a name computed from its input, outside the manifest.** A variant
  set extracted from rendering `m1` with prompt version 3 would live at a name made from `m1` and `3`, and its existence
  would mean it is done. That needs no write to the manifest, no schema change per kind, and no contention between an
  extraction and anything else writing the paper. It lost at the first level holding two candidates. The next step's
  input is then a choice rather than a computation, and a name computed from one particular input cannot record which
  candidate was chosen. Nothing would list what exists either, short of listing the bucket.
- **Refs in an object of their own beside the manifest.** Refs would then never contend with writes to the records. It
  lost because recording an artifact and moving the ref that names it could no longer happen in one write, so a ref
  could briefly name a record the manifest does not hold. Nor could one check confirm that every ref resolves.
- **A record keyed by its bytes, its deps and its construction together.** Every distinct way of producing the same
  bytes would then keep its own record, and no construction would ever be overwritten. It lost because no reader would
  answer differently: a quote cites bytes, and two routes to identical text yield the same text.

## Open questions

- **Licence on supplementary files.** A supplementary file carries no licence of its own, so it is read under the
  paper's. A data file published under different terms, CC0 beside a CC-BY article say, would need its own, which is a
  new optional field when something needs it.
- **Materialising references from primary to secondary artifacts.** They are matched by name at render time. An index of
  them is an additive change if the read path ever needs one.
- **Where the fidelity ranking lives, and how a model counts in it.** Writers now apply the ranking when they move the
  `markdown` ref. Whether a newer model outranks an older one on the same converter is not yet settled.
- **Converter-specific fields on a rendering.** A rendering's construction requires a model exactly when the converter
  is a vision model, enforced by a constraint on the message. If more converter-specific fields appear, they could
  arrive as a per-converter choice beside the common fields. Moving the common fields into that choice would be an
  expand/contract change.
- **A Bookshelf accession at the door.** Whether `MaybeIngestPapers` accepts `bookid:` beside `doi:`, `pmid:` and
  `pmcid:` is held in [`literature-evidence-layer.md`](literature-evidence-layer.md#open-questions).
- **Falling back to an equivalent work for entitlement.** A knowledge unit anchored to a work a reader is not entitled
  to see, such as a licensed paper, could be shown against an equivalent work they can see, such as a preprint linked to
  it by an equivalence edge, by re-aligning the same quote into that work's rendering. The pieces exist. Whether to
  expose it, and how entitlement composes across a set of equivalent works, is open.
- **An alert for drift met on demand.** A paper `MaybeIngestPapers` cannot ingest because its record drifted is named
  only in the evidence service's log. A log-based metric with an alert on it would surface it; it would post to the
  operational alerts channel each environment is still to get ([`prod-bring-up.md`](../plans/prod-bring-up.md)).
