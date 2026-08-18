import { ChevronDown } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { Logo } from "@/components/logo";

// The shared chrome (design-spec §2.0): the logo + wordmark, which lead back to the Projects page,
// the verified caller, and a `left`/`right` slot each page fills with what only it carries — the
// Projects page nothing, a Project page its name, an Analysis page its identity and the
// conversation-dock control.
export function AppBar({
  userEmail,
  left,
  right,
}: {
  userEmail: string;
  left?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <header className="flex h-[54px] shrink-0 items-center justify-between gap-[16px] border-b border-line-primary bg-white px-[22px]">
      <div className="flex min-w-0 items-center gap-[16px]">
        <Link
          href="/"
          aria-label="Themis — all Projects"
          className="flex shrink-0 items-center gap-[10px] rounded-field"
        >
          <Logo className="h-[30px] w-auto" />
          <span className="text-[16px] font-semibold tracking-[-0.01em] text-ink-primary">
            Themis
          </span>
        </Link>
        {left !== undefined && (
          <>
            <span
              className="h-[24px] w-px shrink-0 bg-line-primary"
              aria-hidden
            />
            <div className="flex min-w-0 items-center gap-[10px]">{left}</div>
          </>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-[9px] pl-[4px]">
        {right !== undefined && (
          <>
            {right}
            <span className="h-[24px] w-px bg-line-primary" aria-hidden />
          </>
        )}
        <span className="max-w-[280px] truncate font-mono text-[12px] text-ink-muted">
          {userEmail}
        </span>
        <ChevronDown className="size-[11px] text-ink-faintest" aria-hidden />
      </div>
    </header>
  );
}
