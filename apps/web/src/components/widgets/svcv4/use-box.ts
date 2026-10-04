"use client";

import { useLayoutEffect, useState } from "react";

/** An element's laid-out size in pixels. */
export interface Box {
  width: number;
  height: number;
}

/** The element's size, measured before the first paint and again whenever it changes, a font's
 *  late arrival included; null until the element has been laid out with a size. */
export function useBox(
  element: React.RefObject<HTMLElement | null>,
): Box | null {
  const [box, setBox] = useState<Box | null>(null);
  useLayoutEffect(() => {
    const target = element.current;
    if (target === null) return;
    const measure = () => {
      const { width, height } = target.getBoundingClientRect();
      if (width === 0 && height === 0) return;
      setBox((held) =>
        held?.width === width && held.height === height
          ? held
          : { width, height },
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(target);
    return () => observer.disconnect();
  }, [element]);
  return box;
}
