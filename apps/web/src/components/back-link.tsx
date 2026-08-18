import { ChevronLeft } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

// The one level-up affordance, in the app bar's left slot. A link to a known route, never
// `history.back()`: a shared or bookmarked URL arrives with no history to go back through.
export function BackLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      className="flex h-[32px] shrink-0 items-center gap-[6px] rounded-field px-[9px] text-[12.5px] font-medium text-ink-muted hover:bg-surface-idle hover:text-ink-primary"
    >
      <ChevronLeft className="size-[14px] text-ink-faint" aria-hidden />
      {children}
    </Link>
  );
}
