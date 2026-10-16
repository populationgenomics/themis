# Read one variant-classification run

For the coordinating session: start one fresh-context subagent per collected and rendered run, and hand it everything
below the rule with `{VARIANT_DIR}` replaced by the variant's directory in the round. Give it nothing else: no other
variant's review, and no impression of your own.

______________________________________________________________________

An agent classified one variant under SVCv4, the ClinGen pilot of the points-based ACMG/AMP framework, on the dev
deployment. Curators classified the same variant on the curation worksheet beforehand, each on their own. The variant's
registration states the question: the variant against one entity, a disease under one mode of inheritance, and the
clinical context. Each curator saw it read-only and answered it, and the run was told it in its kickoff. Read the run
against each curator's worksheet and write a review that says whether the run reached a sound classification and where
it went wrong.

## What you have

Everything about this run is under `{VARIANT_DIR}`. Read nothing else of the round: never open another variant's
directory, another review, or anything else under `tools/run_review/rounds/` outside `{VARIANT_DIR}`. Outside the round
you may read the repository's tracked sources the run depended on: the framework's transcription under `themis/svcv4/`
and its rules in `docs/design/svcv4-worksheet-transcription.md` and `docs/design/svcv4-interpretations.md`, and the
agent's instructions.

Read the instructions the run read, not the checkout's. Each skill in `run/run.json`'s `provenance.skills` with a
`commit` was published from `agents/skills/<directory>` at that commit: read it there, with
`git show <commit>:agents/skills/<directory>/SKILL.md` and `git ls-tree -r <commit> -- agents/skills/<directory>` for
the rest. If the commit is missing, `git fetch` first; if it is still missing, say so and do not read the checkout's
copy in its place. `provenance.model` holds the model and its settings. Nothing records the build of the classification
library, the evidence services or the sandbox the run called, so the checkout's `themis/` may be newer than what the run
ran: where the run's tool output disagrees with the checkout's code, say that a version difference may explain it before
calling it a defect.

- `kickoff.json`: what the run was created with. `inputs` holds the transcript, the coding change and the clinical
  context; the run received nothing else about the variant. The clinical context opens with the registered entity,
  stated as the one to classify against, and then carries the registration's clinical context, which every curator was
  also shown, read-only. All of it comes from the registration; nothing a curator wrote reached the run. `date_to_pin`
  is the date of the latest submission among the curators in the reference, and `date_applied` is false because no
  service limits the evidence a run sees to a date yet.
- `run/run.json`: how the run ended (`ending.kind`, and the event that decided it), each interrupt the round sent at the
  deadline (`interrupts`), every error event the session reported (`errors`), and the agent version, model and skill
  versions it ran, with the commit each of our skills was published from (`provenance`).
- `run/events.jsonl`: the session's raw event log, oldest first: the kickoff as the first `user.message`, every tool
  call and result, status changes, errors, model requests and the messages exchanged with spawned threads.
  `run/threads/<id>.jsonl` holds each thread's own log; the reviewer the coordinator briefed is one of them. A thinking
  event says that the model thought and when, not what it thought.
- `run/repository/`: a clone of the run's workspace. The working document and `assets/svcv4.binpb` are at the tip of
  `origin/main`, every revision is a commit, and `git log --first-parent refs/sheaf/reflog` times each push.
- `rendered/record.yaml`: the run's final classification record decoded into text: the routing, every code with its
  status, cell, points, evidence, reasoning and confidence, the tally and the class. The classification library computed
  every number in it, so read the facts and the assessment, not the sums.
- `rendered/registration.yaml`: the variant as registered, the entity with its mode under the member name the record's
  entity uses, the clinical context, the curators whose submissions are the reference, any curator left out because
  their submission left the consequence class or the class unset, and any assigned curator who had not submitted when
  the round was prepared. A curator left out or outstanding is absent from the reference: there is nothing of theirs to
  compare.
- `rendered/curator-<letter>.yaml`: the latest submission of each curator in the reference, decoded into text, with
  every date that curator submitted on and the note left with each submission. Each workflow carries its status, cell,
  evidence, rationale, nearest alternative and confidence. The worksheet-level sections carry the routing, which is the
  consequence class and the curator's reasoning about it, and the class with the calls that decided it.

Treat the run's own account of what it did, in the document or its messages, as a pointer to where to look. The event
log is the evidence.

## How to compare

Compare the run with each curator separately, workflow by workflow, after the routing, in two passes.

First, list every difference between the run and each curator: the routing, the gene-disease record, each workflow's
status and cell, and the class, each with what the run's call rested on and what the curator's did. Leave nothing out
because it seems explained.

Then explain each difference, with one of the explanations below or with "none of these". An explanation is a tag on a
difference: it never takes the difference out of the review. These explanations were written before any round had run,
so a difference that none of them fits, or one that a tag fits badly, is worth saying so.

1. Routing is compared first, as routing. Start with the entity: did the run classify against the disease and the mode
   it was given? Read the entity in the record and the run's reasoning about it. A run that classified against another
   entity has diverged from its instructions, which tell it to classify the entity the clinical context states. That is
   a finding, and its cells answer a different question, so do not grade them against the curators'. Then compare the
   consequence class with each curator's: a run that routed on a different class has taken a different path, so do not
   grade the cells that class selects against that curator's. Say what the run routed on and why that differs.
1. Read which curated gene-disease record the run took its validity level from. `DescribeGene` returns every record
   curated on the disease term, under every mode and from every source, each with its source's own mode term, and the
   run has to pick the one that describes the given entity. The record's source, its classification and the mode the run
   classified under are in the record's entity; the response it chose from is in the event log. A level taken from a
   record curated under another mode, or a mode term read wrongly, is a fact-recovery finding, whatever the class.
1. Inside the curator's openness. A call marked leaning or open admits its neighbouring cells, so a run that lands on
   one of them has not made a mistake. A call marked settled admits only its own cell.
1. The curators disagree. Where they differ from each other, on a cell, the consequence class or the class, record that
   as a finding about the reference and grade the run against neither of them on that point. A resubmission after
   discussion can hide such a disagreement, which is why the rendered submissions carry their dates. With one curator in
   the reference, or a curator left out or outstanding, a disagreement may simply not be visible; weigh a lone curator's
   call by the openness they recorded, as for any other. A curator who thinks another entity is the question to ask says
   so in their routing reasoning; name that as a finding about the reference.
1. What each side could see. Evidence the run cites that was published after a curator's submission date is evidence
   that curator could not have seen; grade it on neither side.
1. A missing route. When the reference rests on a fact the run never had, name the route that should exist (the service,
   the rpc, the field, the instruction) and what it would have returned. Never propose putting the fact in the prompt,
   since that teaches to the test.

A path code (a variant-type code scored on a path through a decision tree) has no cell id in the record. The record
carries the agent's own statement of the cell it read instead. Compare that statement with the curator's cell; the
missing id is how the record is built, and not a finding.

The reasoning matters as much as the answer. A run that reached a curator's cell by a route the curator would reject has
not matched them, and a run that reached the right class from wrong facts is the failure this review exists to find.

## The in-run reviewer

The coordinator briefed a copy of itself to read the draft against the evidence. The brief and the report are in the
event logs, and the document records which findings the coordinator overruled. Say what the reviewer caught, what it
missed that you found, and whether each overruling was right.

## Runs that ended early

If `interrupts` is not empty, the round stopped the run at its deadline, and the document may be half written. If
`ending.kind` is `failed`, the session failed on the event in `ending.event`, after the errors in `errors`. Either way,
read what the run committed as a partial run: compare what it reached and do not count what it never got to as a
mistake. The ending is a finding in itself; say what the log shows the run was doing when it stopped and, for a failure,
what failed. A run whose turn ended on a question is also an end, since nobody answered it, and its instructions say
never to block on a question, so that is a finding too. If `rendered/record.yaml` says the run left no record, the run
produced nothing to compare: find out from the log why.

## The review

Write the review to `{VARIANT_DIR}/review.md`, in prose. Its first line is `Sound`, `Not sound` or
`Could not be checked`, a colon, and the reason in one sentence. Sound needs sound reasoning as well as an answer within
the curators' range. Every finding follows, each one stating what happened, where in the log or the repository you saw
it, and what it says about the system: a tool, an interface, an instruction, the reviewer. Do not rank the findings or
say which matters most; you have read one variant and cannot know how many others a finding touches.
