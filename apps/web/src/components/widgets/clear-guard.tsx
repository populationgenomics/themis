"use client";

import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type ClearCopyGuard,
  ClearCopyGuardContext,
} from "@/components/workbench/clear-copy";
import { useWidgetStates, type WidgetStates } from "./widget-state";

// Clearing an Analysis's copy discards every change in its widgets still waiting on its publish, so
// the clear asks the curator first while there is one, and not otherwise.

/** The guard a clear of `analysisId`'s copy asks: with no change unsaved in its widgets it goes
 *  ahead; otherwise it goes ahead only if `ask`, given how many there are, resolves true. */
export function unsavedChangesGuard(
  states: Pick<WidgetStates, "unsaved">,
  ask: (unsaved: number) => Promise<boolean>,
): ClearCopyGuard {
  return async (analysisId) => {
    const unsaved = states.unsaved(analysisId);
    return unsaved === 0 ? true : ask(unsaved);
  };
}

/** What the curator is asked before a clear discards `unsaved` changes. */
export function unsavedChangesQuestion(unsaved: number): string {
  if (!Number.isInteger(unsaved) || unsaved < 1) {
    throw new Error(`no question to ask about ${unsaved} unsaved changes`);
  }
  const changes =
    unsaved === 1 ? "1 unsaved change" : `${unsaved} unsaved changes`;
  const them = unsaved === 1 ? "it" : "them";
  return `You have ${changes}. Clearing the cache discards ${them}. Everything already saved stays as it is.`;
}

interface Question {
  unsaved: number;
  answer(discard: boolean): void;
}

/** Provides the guard every clear beneath it asks, from the widget states of this window, and
 *  draws the question it asks. */
export function UnsavedChangesGuard({
  children,
}: {
  children: ReactNode;
}): React.ReactElement {
  const states = useWidgetStates();
  const [question, setQuestion] = useState<Question | null>(null);
  const open = useRef<Question | null>(null);
  const guard = useMemo(
    () =>
      unsavedChangesGuard(
        states,
        (unsaved) =>
          new Promise<boolean>((resolve) => {
            // A second clear asked while one question stands answers the first as declined.
            open.current?.answer(false);
            const asked: Question = {
              unsaved,
              answer: (discard) => {
                if (open.current === asked) {
                  open.current = null;
                  setQuestion(null);
                }
                resolve(discard);
              },
            };
            open.current = asked;
            setQuestion(asked);
          }),
      ),
    [states],
  );
  return (
    <ClearCopyGuardContext.Provider value={guard}>
      {children}
      {question !== null && <UnsavedChangesDialog question={question} />}
    </ClearCopyGuardContext.Provider>
  );
}

/** The question, as a modal dialog: Esc and the backdrop are the platform's, and either answers
 *  Cancel. */
function UnsavedChangesDialog({
  question,
}: {
  question: Question;
}): React.ReactElement {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog !== null && !dialog.open) dialog.showModal();
  }, []);
  const cancel = useCallback(() => question.answer(false), [question]);
  return (
    <dialog
      ref={ref}
      onClose={cancel}
      aria-label="Unsaved changes"
      className="m-auto w-[min(26rem,92vw)] rounded-md border border-line-primary bg-white p-0 text-ink-body shadow-lg backdrop:bg-ink-primary/40"
    >
      <UnsavedChangesBody question={question} />
    </dialog>
  );
}

/** The dialog's text and its two answers. */
export function UnsavedChangesBody({
  question,
}: {
  question: Question;
}): React.ReactElement {
  return (
    <div className="flex flex-col gap-[16px] px-[20px] py-[18px]">
      <p className="text-[13.5px] text-ink-primary">
        {unsavedChangesQuestion(question.unsaved)}
      </p>
      <div className="flex justify-end gap-[8px]">
        <button
          type="button"
          onClick={() => question.answer(false)}
          className="rounded-field border border-line-soft px-[12px] py-[5px] text-[12.5px] font-medium text-ink-faint hover:text-ink-primary"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => question.answer(true)}
          className="rounded-field bg-primary px-[12px] py-[5px] text-[12.5px] font-medium text-primary-foreground"
        >
          Discard and reload
        </button>
      </div>
    </div>
  );
}
