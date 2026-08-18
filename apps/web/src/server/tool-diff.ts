import { diffLines } from "diff";
import { DiffLineKind } from "@/models/workbench";

// The line-level diff of an `edit` tool call's replacement
// (docs/design/conversation-view.md).

/** Beyond this many lines on either side the alignment is skipped: it is quadratic in the
 *  line counts. */
const ALIGN_LINE_CAP = 2000;

export interface DiffLineInit {
  kind: DiffLineKind;
  text: string;
}

/** The lines of `text`, with a trailing newline meaning "the last line ends", not "there
 *  is an empty line after it". */
function lines(text: string): string[] {
  if (text === "") return [];
  const split = text.split("\n");
  if (split[split.length - 1] === "") split.pop();
  return split;
}

function marked(text: string, kind: DiffLineKind): DiffLineInit[] {
  return lines(text).map((line) => ({ kind, text: line }));
}

/** The line-level diff of the two sides of a replacement. */
export function replacementDiff(before: string, after: string): DiffLineInit[] {
  if (
    lines(before).length > ALIGN_LINE_CAP ||
    lines(after).length > ALIGN_LINE_CAP
  ) {
    return [
      ...marked(before, DiffLineKind.REMOVED),
      ...marked(after, DiffLineKind.ADDED),
    ];
  }
  // A change coalesces a run of lines into one `value`, so each is split back out: the
  // client draws a sign per line, not per run. `ignoreNewlineAtEof` matches the line rule
  // above — without it a newline added at the end reports the last line as changed to
  // itself, which draws as a line removed and re-added with identical text.
  return diffLines(before, after, { ignoreNewlineAtEof: true }).flatMap(
    (change) =>
      marked(
        change.value,
        change.added
          ? DiffLineKind.ADDED
          : change.removed
            ? DiffLineKind.REMOVED
            : DiffLineKind.CONTEXT,
      ),
  );
}
