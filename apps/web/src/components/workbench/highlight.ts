// Markdown quote highlighting via the CSS Custom Highlight API: DOM Ranges are painted by the
// `::highlight(themis-citation)` rule (globals.css), so nothing in the rendered markdown is mutated —
// clearing is just dropping a range. Placement recomputes the range from the durable quote text against
// the live rendering (the quote is the anchor; offsets are a rendering detail — docs/design/
// document-pane.md), so it survives a representation re-render. Feature-detected: absent the API (older
// browsers), highlighting is a no-op and the caller falls back to the warning chip.
//
// Highlights are **per tab**, keyed by a caller-supplied tab key, because two paper tabs can be visible
// at once (a split tab area, or two windows). The registry holds one named highlight built from the
// union of every tab's range, re-registered on each add/clear, so one tab's highlight never clobbers
// another's — matching the design's per-tab highlight lifecycle.

const HIGHLIGHT_NAME = "themis-citation";
const ranges = new Map<string, Range>();

interface HighlightRegistryLike {
  set(name: string, highlight: object): void;
  delete(name: string): void;
}

function registry(): HighlightRegistryLike | null {
  const highlights = (CSS as unknown as { highlights?: HighlightRegistryLike })
    .highlights;
  return highlights ?? null;
}

function highlightCtor(): (new (...ranges: Range[]) => object) | null {
  return (
    (globalThis as { Highlight?: new (...ranges: Range[]) => object })
      .Highlight ?? null
  );
}

/** Rebuild the single named highlight from the union of every tab's range (or drop it when none). */
function reregister(): void {
  const highlights = registry();
  const Ctor = highlightCtor();
  if (!highlights || !Ctor) return;
  if (ranges.size === 0) {
    highlights.delete(HIGHLIGHT_NAME);
    return;
  }
  highlights.set(HIGHLIGHT_NAME, new Ctor(...ranges.values()));
}

/** Highlight the first occurrence of `quote` within `container` for the tab `key`. Returns false when
 *  the API is unavailable or the quote is not found in the rendered text (the caller shows the warning
 *  chip); other tabs' highlights are untouched. */
export function applyQuoteHighlight(
  container: HTMLElement,
  quote: string,
  key: string,
): boolean {
  const highlights = registry();
  const Ctor = highlightCtor();
  if (!highlights || !Ctor) return false;
  // An empty quote is "nothing to show" — route it through the same clear path as a not-found quote
  // (`findQuoteRange` returns null for it) so this tab's prior highlight is dropped, not left painted.
  const range = findQuoteRange(container, quote);
  if (!range) {
    clearQuoteHighlight(key);
    return false;
  }
  ranges.set(key, range);
  reregister();
  range.startContainer.parentElement?.scrollIntoView({
    block: "center",
    behavior: "smooth",
  });
  return true;
}

export function clearQuoteHighlight(key: string): void {
  if (ranges.delete(key)) reregister();
}

function findQuoteRange(container: HTMLElement, quote: string): Range | null {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode())
    nodes.push(node as Text);
  const span = locateQuoteInRuns(
    nodes.map((n) => n.data),
    quote,
  );
  if (!span) return null;
  const range = document.createRange();
  range.setStart(nodes[span.start.run], span.start.offset);
  range.setEnd(nodes[span.end.run], span.end.offset);
  return range;
}

interface RunOffset {
  run: number;
  offset: number;
}

/** Resolve the first occurrence of `quote` across a sequence of adjacent text runs to `(run, offset)`
 *  start/end positions, or null when absent. Pure over the run texts — the DOM-free half of the
 *  boundary walk that carries the risk: a quote spanning nodes, a position on a run boundary, empty
 *  runs from the renderer. The `start` resolves to the run that *contains* the character, because the
 *  scroll target is `startContainer.parentElement`; the `end` resolves to the earlier run's end, an
 *  equivalent DOM Range endpoint to the start of the next. */
export function locateQuoteInRuns(
  runTexts: string[],
  quote: string,
): { start: RunOffset; end: RunOffset } | null {
  if (!quote) return null;
  const index = runTexts.join("").indexOf(quote);
  if (index < 0) return null;
  const start = runOffset(runTexts, index, true);
  const end = runOffset(runTexts, index + quote.length);
  if (!start || !end) return null;
  return { start, end };
}

function runOffset(
  runTexts: string[],
  position: number,
  preferLater = false,
): RunOffset | null {
  let start = 0;
  for (let run = 0; run < runTexts.length; run++) {
    const end = start + runTexts[run].length;
    if (preferLater ? position < end : position <= end)
      return { run, offset: position - start };
    start = end;
  }
  return null;
}
