import { ChevronDown } from "lucide-react";
import { Eyebrow } from "@/components/eyebrow";
import { Logo } from "@/components/logo";

// The shared chrome (design-spec §2.0): logo + wordmark, a divider, the Project
// identity, and the verified caller.
export function AppBar({
  userEmail,
  projectName,
}: {
  userEmail: string;
  projectName: string;
}) {
  return (
    <header className="flex h-[54px] shrink-0 items-center justify-between border-b border-line-primary bg-white px-[22px]">
      <div className="flex items-center gap-[16px]">
        <div className="flex items-center gap-[10px]">
          <Logo className="h-[30px] w-auto" />
          <span className="text-[16px] font-semibold tracking-[-0.01em] text-ink-primary">
            Themis
          </span>
        </div>
        <span className="h-[24px] w-px bg-line-primary" aria-hidden />
        <div className="flex h-[38px] items-center gap-[11px] rounded-field border border-line-primary bg-white px-[13px]">
          <span
            className="size-[8px] rounded-[2px] bg-teal-project-dot"
            aria-hidden
          />
          <span className="flex flex-col items-start leading-[1.1]">
            <Eyebrow className="text-[9.5px] tracking-[0.14em]">
              PROJECT
            </Eyebrow>
            <span className="text-[13px] font-semibold text-ink-primary">
              {projectName}
            </span>
          </span>
          <ChevronDown
            className="ml-[2px] size-[11px] text-ink-faintest"
            aria-hidden
          />
        </div>
      </div>

      <div className="flex items-center gap-[9px] pl-[4px]">
        <span className="max-w-[280px] truncate font-mono text-[12px] text-ink-muted">
          {userEmail}
        </span>
        <ChevronDown className="size-[11px] text-ink-faintest" aria-hidden />
      </div>
    </header>
  );
}
