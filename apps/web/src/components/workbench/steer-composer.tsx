"use client";

import { ArrowUp, CircleStop } from "lucide-react";
import { useLayoutEffect, useRef } from "react";
import { errorMessage, isAgentBusy } from "@/lib/rpc";
import type { Steering } from "./use-steering";

// The curator's turn, pinned under the conversation stream. Enter sends and clears the
// field; Shift+Enter inserts a newline; ⌘↵ sends too, so the create composer's gesture
// holds here as well. The draft lives in `useSteering`, not here, so a layout change
// that remounts this component does not discard it.

export function SteerComposer({ steering }: { steering: Steering }) {
  const field = useRef<HTMLTextAreaElement>(null);
  const canSend = steering.ready && steering.draft.trim().length > 0;

  // Driven off the value, not the input event: prose handed back after a failed send
  // arrives without one, and would otherwise sit in a one-row box. `max-height` clamps
  // the growth, so the field scrolls past that rather than filling the region.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the draft is a re-run trigger — the body measures the field, which carries it by the time a layout effect runs.
  useLayoutEffect(() => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [steering.draft]);

  const send = () => {
    if (!canSend) return;
    steering.send();
    field.current?.focus();
  };

  return (
    <div className="shrink-0 border-t border-line-primary bg-white px-[26px] py-[16px]">
      <div className="flex items-end gap-[10px] rounded-card border border-line-input bg-white px-[14px] py-[10px] focus-within:shadow-focus-ring">
        <textarea
          ref={field}
          rows={1}
          value={steering.draft}
          onChange={(e) => steering.setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.shiftKey) return;
            // An IME consumes Enter to accept a candidate, so sending on it would post
            // a half-composed turn and drop the rest.
            if (e.nativeEvent.isComposing) return;
            e.preventDefault();
            send();
          }}
          placeholder={steering.ready ? "Write a message…" : "Loading the run…"}
          aria-label="Steer this analysis"
          // 6.5px vertical padding makes a single 21px line 34px tall — centered against
          // the send button, which sets the row height; further lines grow from the top.
          className="tscroll max-h-[145px] min-w-0 flex-1 resize-none bg-transparent py-[6.5px] text-[13.5px] leading-[1.55] text-ink-body outline-none placeholder:text-ink-faintest"
        />
        <button
          type="button"
          onClick={steering.stop}
          disabled={!steering.ready}
          aria-label="Stop the agent's current step"
          className="flex size-[34px] shrink-0 items-center justify-center rounded-full text-ink-faint hover:text-ink-body disabled:opacity-50"
        >
          <CircleStop className="size-[19px]" strokeWidth={2} aria-hidden />
        </button>
        <button
          type="button"
          onClick={send}
          disabled={!canSend}
          aria-label="Send this turn"
          className="flex size-[34px] shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-50"
        >
          <ArrowUp className="size-[16px]" strokeWidth={2.4} aria-hidden />
        </button>
      </div>
      <p className="mt-[7px] text-[11.5px] text-ink-faintest">
        ↵ to send · ⇧↵ for a new line
      </p>
      {steering.error !== null && (
        <p role="alert" className="mt-[4px] text-[12.5px] text-error-text">
          {isAgentBusy(steering.error.cause)
            ? "The agent is still working on its current step — wait for it to finish, or stop it first."
            : steering.error.act === "send"
              ? `Could not send that turn: ${errorMessage(steering.error.cause)}`
              : `Could not stop the agent: ${errorMessage(steering.error.cause)}`}
        </p>
      )}
    </div>
  );
}
