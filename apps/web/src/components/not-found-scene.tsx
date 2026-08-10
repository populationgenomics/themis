"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/**
 * Host for the not-found animation. Three.js is imported dynamically so it stays out of
 * every other route's bundle, and the scene reads its backdrop off this element's
 * computed background — `bg-surface-inset` here is the single source of that colour.
 */
export function NotFoundScene({ className }: { className?: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let dispose: (() => void) | null = null;
    let cancelled = false;
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;

    import("@/lib/not-found-scene")
      .then(({ mountNotFoundScene }) => {
        if (cancelled) return;
        dispose = mountNotFoundScene(host, { reducedMotion });
      })
      .catch((error: unknown) => {
        // A 404 must still render its message if WebGL or the chunk is unavailable.
        console.error("not-found scene failed to mount", error);
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
      dispose?.();
    };
  }, []);

  if (failed) return null;

  return (
    <div
      ref={hostRef}
      aria-hidden
      className={cn(
        "relative overflow-hidden rounded-panel bg-surface-inset",
        className,
      )}
    />
  );
}
