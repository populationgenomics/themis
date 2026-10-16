# Coordinate a round

For the coordinating session, working through a collected and rendered round with the operator.

## Before reading

1. Run `status`. If it reports mixed agent versions, run the rerun it names (`run --rerun <variant>` for each
   straggler), then `render`, and start again from here. Don't compare runs across two agent versions; trying a fix,
   below, is the one exception.
1. A variant whose run was not collected or did not render cannot be read. Tell the operator which, with the cause
   `status` gives. A collected run that failed or was interrupted is read like any other.

## Reading

Start one fresh-context subagent per rendered run with `read-run.md`, as its header says. Give a reader nothing but that
brief: no other review, and no view of your own.

## Discussing the findings

Summarise the findings for the operator, pointing into the reviews where each one is argued. Say which look like defects
of the agent or a service, and which are about the reference: curators disagreeing, a worksheet's data, how the
registration was written. Some are questions of interpretation, where the instructions and the curators' practice part
ways; name those as the operator's to decide. Then decide with the operator what to fix.

When the reference rests on a fact the run never had, the fix is the route to that fact (a service, an rpc, a field, an
instruction). Never put the fact in a prompt.

These help when they fit, and neither is required:

- Before writing an instruction change that might be fitted to the variants that prompted it, run the generality check
  in a fresh-context subagent with `generality.md`, its placeholders filled as its header says.
- To try a fix on dev before it is merged, tell the operator first what would count as fixed in the new runs. Ask them
  to deploy the fix's branch to dev, since you never deploy yourself, then run `run --rerun <variant>` for each variant
  that showed the finding, `render`, and read every new run with a fresh reader. Then tell the operator whether the new
  runs meet what you said; the earlier runs and their reviews are where `run` logged that it moved them.

## What goes into tracked files

Every tracked file, commit message and pull request is mirrored to a public repository. None of them names a variant, a
gene or disease entity under curation, a curator or a case fact, or carries any text a curator wrote, even paraphrased;
a fix is described by the prompts, instructions, interfaces, services and framework clauses it changes. Everything the
round produced stays under `tools/run_review/rounds/`, which is not tracked.
