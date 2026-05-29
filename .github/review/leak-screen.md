### Concern: leak screening

The leaks here are the hardest for human reviewers to catch and the
most damaging if they reach the public mirror. Treat anything that
*might* be derived from real participants as suspect; lean toward
flagging when in doubt. A false positive here is cheap, a miss is not.

**Identifiers.** CPG internal identifiers and other participant- or
sample-level handles. A separate regex screen catches well-formed
CPG / XPG IDs (`[CX]PG\d+`) on its own; flag here:

- IDs in formats the regex doesn't know about (legacy cohort IDs,
  study codes, family or pedigree IDs).
- Real-looking participant identifiers in places that look like test
  fixtures. Convention in this repo: test fixtures must not contain
  strings shaped like CPG / XPG identifiers at all — use a different
  prefix (e.g. `TST123`) and monkeypatch the regex if an end-to-end
  test really needs to exercise the screen pattern. Flag any fixture
  that violates this convention.

**Phenotype and clinical content.** Anything that reads like it could
have come from a real case file, even if it's "anonymised":

- Snippets of phenotype descriptions, HPO term clusters tied to a
  narrative, free-text clinical observations.
- Excerpts that look like clinician letters, referrals, MDT meeting
  notes, or family-history narratives.
- Comments, docstrings, or commit messages that recount a specific
  case ("the patient who…", "in the family we were investigating last
  week…"). The fact that no name appears doesn't make it safe.
- Test fixtures or example inputs that look plausibly authored from a
  real case. Be suspicious of evocative or specific detail in anything
  marked "example".

**Genomic data with real-data provenance.**

- Reads, read windows, or alignment slices that could have come from a
  real CRAM/BAM (any string of bases longer than a few dozen
  nucleotides in test data deserves scrutiny — flag it and ask where
  it came from).
- Variant call tables, sample-level statistics, allele-frequency
  tables, or QC summaries that include real sample IDs or row counts
  that match real cohort sizes.
- Notebooks (`.ipynb`) containing data tables, plots, or summary
  statistics derived from real cohorts — even if the cells look
  innocuous, the outputs often carry sample identifiers and
  distributions.

**Other PII.** Names, dates of birth, addresses, MRN/Medicare numbers,
clinician names tied to specific cases, hospital identifiers — in any
context where the person is the *subject* (not the author) of the
content.

**Internal-only documentation or process detail.** Content written for
an internal audience that leaks information the public mirror should
not carry:

- Names or contact details of individual staff, collaborators, or
  participants where the context is operational (rosters, on-call,
  escalation lists). Authorship and reviewer attribution on commits
  and PRs is fine.
- Internal Slack channels, Confluence URLs, Jira project keys, or
  ticket numbers used as the primary reference for a decision. A
  passing mention is usually fine; a doc whose central content is "see
  CPG-1234" is not.
- Roadmap, prioritisation, or budget discussion that is not intended
  for an external audience.

**References to internal infrastructure.** Specifics of infrastructure
that are not already public:

- GCP project IDs, bucket names, dataset names, service-account
  emails, or other resource identifiers that disclose the layout of
  private infrastructure.
- Internal hostnames, IP ranges, VPN endpoints, or network topology.

Generic references ("we deploy to GCP", "we use Metamist") are fine.
The line is at concrete identifiers that name a specific private
resource.

**Embarrassing or unprofessional content.** Content that wouldn't be a
security or privacy problem but would reflect badly on CPG if it
surfaced on the public mirror:

- Snide, dismissive, or unprofessional comments about other people,
  teams, organisations, vendors, or tools — in code comments, commit
  messages, and test data.
- Jokes, slurs, or off-colour humour in identifiers, strings,
  fixtures, or comments.
- Frustration vented at code reviewers, the CI system, dependencies,
  etc. — fine in a Slack DM, not fine in a public commit.
- Placeholder text or scratch work left behind ("TODO: explain to my
  manager why this is so ugly", lorem-ipsum that's actually
  inappropriate, etc.).

The bar is "would I be comfortable with my name attached to this on a
public repo a future employer might look at?"

When you're not sure whether something is real or synthetic, that's
itself worth a comment. A reviewer can verify and resolve.
