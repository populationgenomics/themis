import type { ReactNode } from "react";

/** A registration's clinical context as every curation surface shows it: the manager's prose with its
 *  line breaks kept. */
export function ClinicalContextText({ value }: { value: string }) {
  return (
    <p className="framework-voice whitespace-pre-wrap break-words text-[14px] text-ink-body">
      {value}
    </p>
  );
}

/** The registration's clinical context as a panel of its own, with a note saying where it comes
 *  from. Part of the question, so read-only wherever it appears. */
export function ClinicalContextPanel({
  value,
  heading,
  note,
}: {
  value: string;
  /** The panel's title, set in the type of the page around it. */
  heading: ReactNode;
  note: string;
}) {
  return (
    <section className="rounded-md border border-line-primary bg-surface-warm-panel p-5">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        {heading}
        <span className="framework-voice text-[12.5px] text-ink-faint">
          {note}
        </span>
      </div>
      <ClinicalContextText value={value} />
    </section>
  );
}
