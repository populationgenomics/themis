import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

// The recurring uppercase mono eyebrow label. Size, tracking,
// and any contextual colour override come from the caller via `className`; the
// default is the faintest ink at the common tracking.
export function Eyebrow({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "font-mono uppercase text-ink-faintest tracking-[0.1em]",
        className,
      )}
    >
      {children}
    </span>
  );
}
