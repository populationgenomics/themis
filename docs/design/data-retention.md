# Design: data retention and erasure

**Related:** [`access-control.md`](access-control.md) (Projects, the datasets behind them, and how the agent reads a
dataset); [`sheaf.md`](sheaf.md) (how an Analysis's repository is stored);
[`workbench-workspace.md`](workbench-workspace.md) (the copy of a repository each browser keeps);
[`conversation-view.md`](conversation-view.md) (how the conversation is read from the session);
[`curation-surface.md`](curation-surface.md) (the lab's curation records); [`security.md`](security.md) (the log of
calls to upstream evidence services); [`infrastructure.md`](infrastructure.md) (the environments, their logs and their
backups).

## Overview

Every environment stores content about the cases its curators work on. Prod is built to hold consented research data
from CaRDinal, and a participant can withdraw consent. So this doc decides how long Themis keeps what it stores, and
what Themis does when a withdrawal reaches its operators.

An Analysis is the record of a piece of curation work. Curators come back to earlier Analyses as references when a later
case raises the same variant or the same question, so Themis keeps every Analysis indefinitely: its database rows, the
repository that holds its working document, and the session transcript at Anthropic. Nothing expires by age, and an
Analysis outlives the dataset it was opened on. Data leaves only through erasure, or when a whole environment is
deleted.

Erasure starts from a fact about what Themis stores: nothing in it links an Analysis to a participant. The structured
keys are the Project, which stands for a whole dataset, and the Analysis id. A participant appears only in free text,
such as the clinical context a curator typed or the agent's working document. So an operator finds the affected Analyses
by searching that text, and erases each of them whole. An erased Analysis is removed from every store Themis controls.

Some copies sit outside what an erasure can touch, and the doc names each with how long it lasts. Database backups age
out within 30 days. Anthropic keeps deleted session data for up to 30 days, or two years for content its safety review
flagged. Logs, which cannot be deleted entry by entry, are kept for 400 days, and they include the database audit log,
which records each statement with its values. Curation submissions are the lab's record of a variant's assessment and
are kept. Browser copies stay on the curator's machine.

Anthropic stores Managed Agents session data in the US only, and no zero-retention arrangement covers it. Prod therefore
carries no CaRDinal data until CPG's data governance confirms that the consent and ethics approval allow it.

## Background

### What counts as case content

Themis reads public evidence (literature, population frequencies, gene–disease curations) and case content. Case content
is anything about the case under study: the variant, the phenotype, the family structure, and in prod the sample files
the agent reads from the site's dataset
([`access-control.md`](access-control.md#the-agent-reads-its-projects-dataset-as-files)). It enters Themis in three
ways. A curator types it into a new Analysis as clinical context, or into a curation worksheet. The agent reads it from
the dataset. And the agent writes about it, in its working document, its scripts and its replies.

Once content is in an Analysis it spreads. The clinical context a curator types becomes part of the session's opening
instruction, so it sits in the database row and in the session transcript. The agent quotes it in the working document.
A lookup the agent makes against an upstream evidence service names the variant in the request URL, which the evidence
service logs.

### What an Analysis is made of

An Analysis is a curator's working session on one question, bound to one Project
([`../../GLOSSARY.md`](../../GLOSSARY.md)). Three stores hold it:

- a row in Cloud SQL with its inputs (the variant, the clinical context or a free-form prompt), its Project, its creator
  and the id of its session;
- a sheaf repository in GCS, which holds the working document, the agent's scripts and intermediate files, and every
  earlier revision of each, because the repository's history is append-only ([`sheaf.md`](sheaf.md));
- a Managed Agents session at Anthropic, whose event stream holds the opening instruction, every tool call with its
  input and result, and everything the model wrote. The conversation a curator sees is read from this stream
  ([`conversation-view.md`](conversation-view.md)).

The Project is the only link to a dataset, and nothing links an Analysis to a participant. One Analysis can concern
several people: a trio has three, and a free-form question may concern a whole cohort.

## Design

### What is stored where

The table lists every store that can hold case content, in every environment. "Erasure" says whether an operator erasing
an Analysis removes that store's copy.

| Store                                                        | What it holds                                                                                       | Where                  | Kept                                                   | Erasure                                                                        |
| :----------------------------------------------------------- | :-------------------------------------------------------------------------------------------------- | :--------------------- | :----------------------------------------------------- | :----------------------------------------------------------------------------- |
| Analysis rows ([Cloud SQL](../../infra/themis_infra/sql.py)) | Inputs, including clinical context or a prompt; Project; creator                                    | `australia-southeast1` | Indefinitely                                           | Removes them                                                                   |
| Sheaf repository ([GCS](../../infra/themis_infra/sheaf.py))  | Working document, scripts, intermediate files, every revision                                       | `australia-southeast1` | Indefinitely                                           | Removes it, every version                                                      |
| Managed Agents session                                       | Opening instruction, every tool call and result, model output                                       | Anthropic, US          | Indefinitely                                           | Removes it; Anthropic's back end keeps it up to 30 days (two years if flagged) |
| Database backups                                             | Copies of every row                                                                                 | `australia-southeast2` | 30 daily backups, and 7 days of point-in-time recovery | Ages out within 30 days                                                        |
| Curation rows ([`curation-surface.md`](curation-surface.md)) | Variant, clinical context, rationales                                                               | `australia-southeast1` | Indefinitely                                           | Out of scope                                                                   |
| Logs                                                         | Upstream request URLs naming the variant; the sandbox's labels for its commands; each SQL statement | `australia-southeast1` | 400 days                                               | Ages out after 400 days                                                        |
| A curator's browser                                          | A full copy of each Analysis they opened                                                            | The curator's machine  | Until evicted for space or cleared                     | Not reached                                                                    |

Cloud Trace spans and request logs carry ids but no case content, so they are not listed: the sandbox's spans record a
tool's name and outcome, never its input or result.

### Analyses are kept indefinitely

The obvious retention rule for personal research data is a fixed period: delete what has not been touched for a year, or
delete an Analysis when its dataset stops being served. Either rule bounds how much case content Themis holds, and
either can be enforced by a scheduled job with no operator in the loop.

Both rules destroy something Themis exists to keep. An Analysis is the record of how a variant was assessed: the
evidence the agent found, the reasoning in the working document, the curator's edits. When a later case brings up the
same variant, that record is what a curator reads first. A rule that deletes an Analysis because a year has passed, or
because the site has stopped serving its dataset, deletes the record at the point it becomes useful for the next case.

So every Analysis is kept until it is erased, in every store that holds it. The cost is that the volume of case content
in Themis only grows, and an erasure request is the only event that reduces it. Storage cost grows with it, slowly: GCS
moves a repository's objects that nobody reads to colder storage classes on its own
([`sheaf.md`](sheaf.md#compaction-and-append-only-storage)).

### Erasure works on whole Analyses

The site reports a withdrawal to Themis's operators, with the participant's sample and family identifiers as Metamist
records them. An operator then finds every Analysis that holds that participant's content and erases each one whole.

Finding them is a text search, because the participant appears only in text. The operator searches the Analyses' inputs
(their clinical context and prompts) and their working documents for those identifiers. Session transcripts are not
searched: the transcript of an Analysis the search finds is deleted along with it. The consequence is that content which
reached only a transcript goes unfound. If the agent read a sample's files and nothing it printed from them was quoted
into the working document, the Analysis's inputs and document never name the participant, the search does not find it,
and its transcript stays at Anthropic.

Erasing one Analysis removes it from all three of its stores: its rows in Cloud SQL, every object and every version
under its repository's key prefix in GCS, and its Anthropic session. Repositories share no objects
([`sheaf.md`](sheaf.md#non-goals)), so deleting one leaves nothing behind that another Analysis needs. Deleting a
session removes "the session's record, events, and associated sandbox"
([session operations](https://platform.claude.com/docs/en/managed-agents/session-operations)).

Take a withdrawal for the proband of family F17. The search finds two Analyses. One is a single-variant classification
whose clinical context reads "F17 proband, early-onset cardiomyopathy". The other is a trio Analysis covering the
proband and both parents. The operator erases both. For the first, that removes exactly the withdrawn content. For the
second, it also removes the parents' content. The proband's content sits in every earlier revision of the trio's working
document, and the session's events cannot be deleted one at a time, so there is no way to take one person out of an
Analysis and keep the rest. The trio Analysis is gone, and a curator who still needs it for the parents opens a new one.

The cost of erasing whole Analyses is that erasing one participant also erases the content of anyone who shared an
Analysis with them. A search can also miss an Analysis whose text names the participant only indirectly, so a person
checks the result of a search before anything is deleted.

### What erasure does not reach

Database backups are snapshots, and a snapshot cannot be edited. Every environment keeps 30 daily backups and seven days
of point-in-time recovery, so a deleted row is gone from every copy 30 days after the erasure.

Anthropic keeps deleted data on its back end "within 30 days" of deletion, and content that its usage-policy review
flagged for up to two years
([data retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data)). Themis
has no control over either period.

Logs cannot be deleted one entry at a time, and three kinds of entry carry case content. The evidence service logs each
upstream request URL, which names the gene and variant a lookup was for
([`security.md`](security.md#what-counts-as-an-exfiltration-channel)). The sandbox logs a short label the model writes
for each command it runs, which can name a variant or a sample. And the database audit log records every statement with
the values bound into it ([`infrastructure.md`](infrastructure.md#audit-logging)), so every clinical context, prompt and
rationale a curator enters is also copied into the logs. Logs are kept for 400 days, so each of these stays readable for
up to 400 days after an erasure. The window matches the 400 days for which Google keeps the admin-activity audit logs,
so that a question asked months later about what a service sent upstream, or who read what, can still be answered from
the record. A shorter window would shorten the tail of an erasure, but 400 days is not a long operational history, and
there is no reason to go shorter.

Curation submissions and assessments hold clinical context and the curators' rationales. They are the lab's record of
how a variant was assessed, and erasure leaves them in place. Unlike every other copy in this section, they have no end
date: a registration's clinical context is the referral's free text, which can name the participant, and it stays after
the withdrawal for as long as the record does.

A curator's browser keeps a full copy of every Analysis they open, including its history, and the copy survives sign-out
and removal from a Project. [`workbench-workspace.md`](workbench-workspace.md#where-things-are-stored) accepts that for
revoked access "under the curator trust model … and the assumption that curators use personal workstations". The same
holds for erasure, and curators' workstations are managed by their institutions, whose controls govern what stays on
them.

### Session transcripts are stored in the US

Every store Themis runs itself is in Australia. The session transcript is not. Anthropic stores Managed Agents data in
the workspace's geography, and "`us` is the only available workspace geo"
([data residency](https://platform.claude.com/docs/en/manage-claude/data-residency)). Zero data retention does not help
either: "In Claude Managed Agents, no feature is eligible for ZDR or HIPAA readiness"
([API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)).

Inference has a setting of its own. Prod's agent pins no inference geography, so it follows the workspace default, which
is left at Anthropic's default of `global`: "Inference may run in any available geography". The model therefore reads
the case content wherever Anthropic serves the request from.

So in prod, every transcript is a US-hosted copy of the case content its Analysis touched: the clinical context, every
tool result, and whatever the agent's commands print from the dataset's files. Whether CaRDinal's consent and ethics
approval allow storage in the US and inference in any region is a data-governance question rather than a design one, so
the design makes it a precondition, and prod carries no CaRDinal data until CPG's data governance confirms it.

### Agent configuration and uploaded files

An erasure deletes the session, so anything Anthropic keeps independently of a session would escape it. An agent's
configuration can be archived but not deleted, and a file uploaded through the Files API is "not affected" when a
session that used it is deleted
([session operations](https://platform.claude.com/docs/en/managed-agents/session-operations)). So case content never
goes into an agent's configuration, and Themis uploads no files. Files the session writes are scoped to it and are
deleted with it.

## Alternatives considered

- **Delete an Analysis when its dataset leaves Themis**, after a grace period. This bounds Themis's holdings by what the
  site still serves, and needs no erasure request when a dataset is retired. It lost because the Analyses on a retired
  dataset are still the record of how their variants were assessed, and later cases on other datasets read them.
- **A fixed lifetime per Analysis**, such as a year after its last activity. A scheduled job enforces it without anyone
  in the loop, and the volume of case content stays bounded. It lost for the same reason: age is no measure of whether a
  record of curation work will be needed again.
- **Delete each Anthropic session early, and keep the rows and the repository.** That removes the only copy outside
  Australia and shortens Anthropic's tail, while the working document stays as the record. It lost because the
  conversation a curator reads is the session's event stream, so deleting the session deletes the conversation from the
  record.
- **Record which samples each Analysis read.** When the agent mounts a sample's files, Themis would record the sample on
  the Analysis, and erasure would find a participant's Analyses by a query rather than a search. That link fits a
  single-case Analysis, which reads a few known samples. It fails for anything wider, such as a cohort analysis, where
  an Analysis reads many samples without being about any one participant, and it misses content a curator typed in.
  Search is needed either way, so a link would add a second mechanism without removing the first.
- **Give logs that can carry case content their own bucket, kept for 30 days.** That would bound the log tail of an
  erasure at 30 days, matching the backups. It lost to the reason the window is 400 days, given above.
- **Reconcile browser copies when the app loads**, deleting the copies of Analyses the user may no longer read or that
  were erased, and expiring copies left unopened. That would bring browser copies inside erasure. It lost for the
  reasons given above: the curator trust model, and workstations that their institutions manage.
