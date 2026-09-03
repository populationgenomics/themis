"use client";

import { type ReactNode, useEffect, useRef } from "react";

// A framework reference the calculator prints behind a button on a workflow.
//
// Native `<dialog>`: Esc, the backdrop, the focus trap and the inert background are the platform's,
// so the surface adds no dependency to get them. The shape mirrors the calculator's own modals — a
// title bar with a close cross, the body, and an OK that dismisses.

export function ReferenceDialog({
  title,
  open,
  onClose,
  children,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  // `showModal()` is the only route to the top layer and the backdrop; the `open` attribute alone
  // renders a non-modal dialog in flow.
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-label={title}
      className="m-auto max-h-[85vh] w-[min(56rem,92vw)] rounded-md border border-line-primary bg-white p-0 text-ink-body shadow-lg backdrop:bg-ink-primary/40"
    >
      <header className="sticky top-0 flex items-start gap-4 border-line-row border-b bg-white px-5 py-3">
        <h2 className="framework-voice flex-1 font-medium text-[15px] text-ink-primary">
          {title}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="framework-voice text-[15px] text-ink-faint hover:text-ink-body"
        >
          ×
        </button>
      </header>
      <div className="max-h-[calc(85vh-7rem)] overflow-y-auto px-5 py-4">
        {children}
      </div>
      <footer className="flex justify-end border-line-row border-t px-5 py-3">
        <button
          type="button"
          onClick={onClose}
          className="framework-voice rounded-sm bg-primary px-3.5 py-1.5 text-[13.5px] text-primary-foreground"
        >
          OK
        </button>
      </footer>
    </dialog>
  );
}

/** The button the calculator prints beside the threshold field to open one of these. */
export function ReferenceButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="framework-voice rounded-sm border border-line-input bg-white px-2.5 py-1 text-[12.5px] text-ink-muted hover:border-ink-ghost hover:text-ink-body"
    >
      {label}
    </button>
  );
}
