import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

// Short machine identifiers — a tool name, a variant's protein change, an ACMG
// code — render as mono chips on the teal tint (design-spec §1.2 Teal accent).
// Only that identity is shared; each call site sets its own geometry — radius,
// padding, size, weight, border — via `className`.
export function IdentifierTag({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={cn("bg-teal-tint font-mono text-teal-fg", className)}>
      {children}
    </span>
  );
}
