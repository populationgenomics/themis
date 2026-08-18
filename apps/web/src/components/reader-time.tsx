"use client";

import { useEffect, useState } from "react";
import { absoluteTime, timeAgo } from "@/lib/format";

// How often the elapsed form re-renders. The coarsest step it shows is minutes, so a slower tick
// would leave "4 min ago" standing while the clock reads five.
const TICK_MS = 20_000;

/** An instant on the reader's own clock: elapsed time, the full instant on hover, refreshed as it
 *  ages. The server cannot format either — it has no reader zone to resolve and no clock that keeps
 *  running — so it renders the pinned form (`format.ts`), and this replaces it once mounted. */
export function ReaderTime({
  iso,
  pinnedLabel,
  pinnedTitle,
  className,
}: {
  iso: string;
  /** The server's render of the same instant. It is this component's first render too, so the
   *  markup matches and hydration has nothing to reconcile. */
  pinnedLabel: string;
  pinnedTitle: string;
  className?: string;
}) {
  const [shown, setShown] = useState({
    label: pinnedLabel,
    title: pinnedTitle,
  });

  useEffect(() => {
    // Bail out when neither string moved: the label changes at most once a minute and, past a week,
    // never again — while a listing holds one of these per card, each on its own interval. Returning
    // the previous object lets React skip the re-render instead of committing an identical `<time>`.
    const render = (): void =>
      setShown((prev) => {
        const label = timeAgo(iso, Date.now(), "reader");
        const title = absoluteTime(iso, "reader");
        return prev.label === label && prev.title === title
          ? prev
          : { label, title };
      });
    render();
    const tick = setInterval(render, TICK_MS);
    return () => clearInterval(tick);
  }, [iso]);

  return (
    <time className={className} dateTime={iso} title={shown.title}>
      {shown.label}
    </time>
  );
}
