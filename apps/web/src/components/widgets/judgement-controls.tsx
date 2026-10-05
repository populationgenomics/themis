"use client";

import { Pencil } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Tooltip } from "@/components/ui/tooltip";
import type { Judgement } from "./judgements";
import type { GuardState } from "./use-judgements";

// The controls a curator records a judgement with, for every widget: a tick saying they reviewed
// what it sits on, and a note in their own words. Each draws the guard's state as `useJudgements`
// reads it, with what a judgement being published shows over it, and why one did not save. A note
// is drawn in the workbench's colours for the curator's own voice, so it is never read as the
// agent's text, and a note being written is kept with the guard, so it survives the control moving
// or the widget being drawn again.

const MARKER: Record<Judgement["state"], { text: string; className: string }> =
  {
    saving: { text: "saving…", className: "text-ink-faint" },
    saved: { text: "saved", className: "text-status-done-fg" },
  };

/** Whether a judgement is being saved, or has been, beside the control that made it. */
export function JudgementMarker({
  state,
}: {
  state: Judgement["state"];
}): React.ReactElement {
  const { text, className } = MARKER[state];
  return <span className={`shrink-0 text-[11.5px] ${className}`}>{text}</span>;
}

/** Why a guard's last judgement did not save, or may not have. */
export function JudgementFailure({
  failure,
}: {
  failure: string | undefined;
}): React.ReactElement | null {
  if (failure === undefined) return null;
  return (
    <div role="alert" className="text-[12.5px] text-error-text">
      {failure}
    </div>
  );
}

/** A curator's tick: "I have reviewed this". `label` names what it is on, for a screen reader;
 *  `compact` draws the box alone, for a row that repeats it. */
export function ReviewedTick({
  guard,
  label,
  compact = false,
}: {
  guard: GuardState;
  label: string;
  compact?: boolean;
}): React.ReactElement {
  const { view, editable } = guard;
  return (
    <label className="inline-flex shrink-0 cursor-pointer items-center gap-[6px] text-[12.5px] text-ink-label has-disabled:cursor-default">
      <input
        type="checkbox"
        checked={view.value === true}
        disabled={!editable}
        onChange={(event) => guard.set(event.target.checked)}
        aria-label={`Reviewed: ${label}`}
        className="size-[14px] accent-primary"
      />
      <span className={compact ? "sr-only" : undefined}>Reviewed</span>
      {view.marker !== undefined && <JudgementMarker state={view.marker} />}
    </label>
  );
}

/** The maximum length of a note, as the payloads' rules bound it. */
export const NOTE_MAX_LENGTH = 4000;

/** Why a note was kept rather than saved: what it is on changed while it was being written. */
export const NOTE_ON_CHANGED =
  "What this note is on changed while you wrote it. Read it again, then save.";

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, index) => byte === b[index]);
}

/** A curator's note: the text they left, and a way to write, change or remove it. The editor takes
 *  its container's whole line, so in a wrapping row it opens beneath the row's controls. `compact`
 *  draws the button as its icon alone. */
export function NoteControl({
  guard,
  label,
  compact = false,
}: {
  guard: GuardState;
  label: string;
  compact?: boolean;
}): React.ReactElement {
  const { view, editable, draft } = guard;
  const text = typeof view.value === "string" ? view.value : "";
  const [notice, setNotice] = useState<string | null>(null);
  const field = useId();
  const area = useRef<HTMLTextAreaElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const editing = draft !== undefined;
  const wasEditing = useRef(editing);
  // Focus follows the editor: into it when it opens, back to what opened it when it closes.
  useEffect(() => {
    if (editing && !wasEditing.current) area.current?.focus();
    if (!editing && wasEditing.current) trigger.current?.focus();
    wasEditing.current = editing;
  }, [editing]);
  // Saving or removing a note moves the control between the row and the block beneath it, so the
  // one that mounts while the change saves takes focus: the change was just made in this window.
  // biome-ignore lint/correctness/useExhaustiveDependencies: on mount alone
  useEffect(() => {
    if (!editing && view.marker === "saving") trigger.current?.focus();
  }, []);
  const open = (from: string) => {
    setNotice(null);
    guard.setDraft({ text: from, judged: guard.judged });
  };
  if (draft !== undefined) {
    const save = () => {
      if (!sameBytes(draft.judged, guard.judged)) {
        setNotice(NOTE_ON_CHANGED);
        guard.setDraft({ ...draft, judged: guard.judged });
        return;
      }
      if (guard.set(draft.text.trim())) guard.setDraft(undefined);
    };
    return (
      <div className="order-last flex w-full basis-full flex-col gap-[6px] rounded-[9px] border border-user-bubble-border bg-user-bubble-bg p-[9px]">
        <label htmlFor={field} className="text-[12px] text-ink-muted">
          Your note on {label}. The agent reads it at the start of its next
          turn.
        </label>
        <textarea
          ref={area}
          id={field}
          value={draft.text}
          maxLength={NOTE_MAX_LENGTH}
          rows={3}
          onChange={(event) =>
            guard.setDraft({ ...draft, text: event.target.value })
          }
          className="min-h-[64px] w-full resize-y rounded-[7px] border border-line-input bg-white px-[9px] py-[6px] text-[13.5px] leading-[1.5] text-ink-body shadow-focus-ring outline-none focus-visible:border-ring"
        />
        {notice !== null && (
          <div
            role="alert"
            className="text-[12.5px] text-amber-uncertainty-heading"
          >
            {notice}
          </div>
        )}
        {!editable && (
          <div className="text-[12.5px] text-ink-muted">
            This version cannot take a note now; your text is kept until it can.
          </div>
        )}
        <div className="flex justify-end gap-[8px]">
          <button
            type="button"
            onClick={() => guard.setDraft(undefined)}
            className="rounded-button px-[10px] py-[4px] text-[12.5px] text-ink-muted hover:text-ink-primary"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={!editable || draft.text.trim() === text}
            className="rounded-button bg-primary px-[11px] py-[4px] text-[12.5px] font-medium text-primary-foreground disabled:opacity-50"
          >
            Save note
          </button>
        </div>
      </div>
    );
  }
  if (text === "") {
    return (
      <Tooltip
        content={compact ? `Add a note on ${label}` : ""}
        describes={false}
      >
        {/* A disabled button takes no pointer events in every browser, so the box around it does. */}
        <span className="inline-flex shrink-0">
          <button
            ref={trigger}
            type="button"
            disabled={!editable}
            onClick={() => open("")}
            aria-label={`Add a note on ${label}`}
            className="inline-flex shrink-0 items-center gap-[5px] rounded-button px-[6px] py-[2px] text-[12.5px] text-ink-muted hover:bg-surface-idle hover:text-ink-primary disabled:pointer-events-none disabled:opacity-60"
          >
            <Pencil className="size-[12px]" strokeWidth={2} aria-hidden />
            {!compact && <span>Note</span>}
            {view.marker !== undefined && (
              <JudgementMarker state={view.marker} />
            )}
          </button>
        </span>
      </Tooltip>
    );
  }
  return (
    <div className="order-last w-full basis-full rounded-[9px] border border-user-bubble-border bg-user-bubble-bg px-[10px] py-[7px]">
      <div className="mb-[2px] flex items-center gap-[8px] text-[11.5px] text-ink-muted">
        <span>Your note</span>
        {view.marker !== undefined && <JudgementMarker state={view.marker} />}
        <span className="flex-1" />
        <button
          ref={trigger}
          type="button"
          disabled={!editable}
          onClick={() => open(text)}
          className="hover:text-ink-primary disabled:opacity-60 disabled:hover:text-ink-muted"
        >
          Edit
        </button>
        <button
          type="button"
          disabled={!editable}
          onClick={() => guard.set("")}
          className="hover:text-ink-primary disabled:opacity-60 disabled:hover:text-ink-muted"
        >
          Remove
        </button>
      </div>
      <p className="m-0 whitespace-pre-wrap text-[13.5px] leading-[1.5] text-ink-body">
        {text}
      </p>
    </div>
  );
}
