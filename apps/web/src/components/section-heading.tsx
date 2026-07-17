import type { ElementType, ReactNode } from "react";
import { cn } from "@/lib/utils";

// The recurring bold-uppercase section heading — distinct from
// the mono `Eyebrow`. Only `font-bold uppercase` is common; size, tracking, colour,
// and margin come from the caller via `className`. Polymorphic via `as` so each
// site keeps its element (an `h3` document section, a `span` inside a callout row,
// a `div` label) without losing semantics.
export function SectionHeading({
  as: Tag = "h3",
  children,
  className,
}: {
  as?: ElementType;
  children: ReactNode;
  className?: string;
}) {
  return <Tag className={cn("font-bold uppercase", className)}>{children}</Tag>;
}
