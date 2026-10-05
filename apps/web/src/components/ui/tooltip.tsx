"use client";

import type { ReactNode, SyntheticEvent } from "react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

// A dependency-free tooltip, the one Themis uses for hover text in place of the browser's `title`:
// it opens the moment the pointer or keyboard focus reaches its target, not after the browser's
// delay, and is drawn in the app's own style. It stays open while the pointer crosses onto it. Escape
// or pressing the target dismisses it (WCAG 1.4.13), and it then stays shut until the pointer leaves
// its target or focus leaves it; a scroll or a resize only hides it. One tooltip is open at a time. The panel is
// portalled to the body, or into the open modal dialog holding its target (a modal sits in the top
// layer, above anything portalled to the body), placed on the side it prefers or the other where
// that has no room, and clamped to the viewport.

/** A box in viewport coordinates, as `getBoundingClientRect` gives one. */
export interface Anchor {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/** The side of its anchor a panel prefers. */
export type Placement = "above" | "below";

// The gap between an anchor and its panel, the viewport margin a panel keeps, and how long a panel
// lingers after the pointer leaves its target so the pointer can cross onto it.
const GAP_PX = 6;
const MARGIN_PX = 8;
const LINGER_MS = 120;

// Announced on the document when a tooltip opens, so any other open one closes.
const OPENED = "themis:tooltip-opened";

// Marks a `Tooltip`'s wrapper and a panel, so nested tooltips and a panel's own scroll can tell
// themselves apart.
const WRAPPER_ATTRIBUTE = "data-tooltip";
const PANEL_ATTRIBUTE = "data-tooltip-panel";
const HIDDEN_TEXT_ATTRIBUTE = "data-tooltip-text";

/** Where a panel of `size` goes against `anchor` in a viewport of `viewport`: centred on the anchor
 *  on the side it prefers, on the other side where the preferred one has no room, and moved in to
 *  keep `MARGIN_PX` from every edge the viewport has room for. */
export function place(
  anchor: Anchor,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  placement: Placement,
): { left: number; top: number } {
  const centre = (anchor.left + anchor.right) / 2;
  const left = Math.max(
    MARGIN_PX,
    Math.min(centre - size.width / 2, viewport.width - size.width - MARGIN_PX),
  );
  const above = anchor.top - GAP_PX - size.height;
  const below = anchor.bottom + GAP_PX;
  const fitsAbove = above >= MARGIN_PX;
  const fitsBelow = below + size.height <= viewport.height - MARGIN_PX;
  const preferred = placement === "above" ? fitsAbove : fitsBelow;
  const other = placement === "above" ? fitsBelow : fitsAbove;
  const side =
    preferred || !other ? placement : placement === "above" ? "below" : "above";
  const top = Math.max(
    MARGIN_PX,
    Math.min(
      side === "above" ? above : below,
      viewport.height - size.height - MARGIN_PX,
    ),
  );
  return { left, top };
}

// Kept inside the panel: portalled, it would otherwise bubble up the React tree to its target's
// ancestors, where a link would navigate or a card toggle on a click meant for the panel.
const stop = (event: SyntheticEvent): void => event.stopPropagation();

/** The floating panel alone, placed against `anchor`: for a caller that computes its own anchor,
 *  such as a chart snapping the pointer to its nearest mark, and drives it with `useTooltip`. */
export function TooltipPanel({
  id,
  anchor,
  placement = "above",
  container,
  children,
  onPointerEnter,
  onPointerLeave,
}: {
  id: string;
  anchor: Anchor;
  placement?: Placement;
  /** Where the panel is portalled: the body unless the target sits in an open modal dialog. */
  container?: Element;
  children: ReactNode;
  onPointerEnter?: () => void;
  onPointerLeave?: () => void;
}): React.ReactPortal {
  const panel = useRef<HTMLDivElement>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: new content has a new size, so the panel is measured and placed again
  useLayoutEffect(() => {
    const element = panel.current;
    if (element === null) return;
    // The layout viewport, which leaves out a scrollbar the window's inner size counts. The panel
    // is capped by it before it is measured, so `place` sees the size it will be drawn at.
    const { clientWidth, clientHeight } = document.documentElement;
    element.style.maxWidth = `${Math.min(360, clientWidth - 2 * MARGIN_PX)}px`;
    element.style.maxHeight = `${clientHeight - 2 * MARGIN_PX}px`;
    const { width, height } = element.getBoundingClientRect();
    const { left, top } = place(
      anchor,
      { width, height },
      { width: clientWidth, height: clientHeight },
      placement,
    );
    element.style.left = `${left}px`;
    element.style.top = `${top}px`;
    element.style.visibility = "visible";
  }, [anchor, placement, children]);
  return createPortal(
    <div
      ref={panel}
      id={id}
      role="tooltip"
      {...{ [PANEL_ATTRIBUTE]: "" }}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onClick={stop}
      onContextMenu={stop}
      onPointerDown={stop}
      onMouseDown={stop}
      onKeyDown={stop}
      // `max-content` so the panel is measured at its own width, not squeezed by where it last sat.
      style={{
        position: "fixed",
        left: 0,
        top: 0,
        width: "max-content",
        visibility: "hidden",
      }}
      className="z-50 overflow-auto rounded-button border border-line-primary bg-white px-[9px] py-[6px] text-left text-[12.5px] leading-[1.45] text-ink-body shadow-[0_8px_24px_rgba(0,0,0,0.10)]"
    >
      {children}
    </div>,
    container ?? document.body,
  );
}

/** One tooltip's state: where it is anchored and what it shows, or null while shut. */
export interface Open<T> {
  anchor: Anchor;
  value: T;
}

/** A tooltip's state and the handlers that drive it, shared by `Tooltip` and by a caller drawing
 *  `TooltipPanel` against an anchor of its own. `show` is ignored after a dismissal until `release`,
 *  which the caller calls when the pointer or focus leaves its target; `same` tells an unchanged
 *  value, so showing it again does not re-render; `dismiss` hides it and holds it shut until
 *  `release`. Escape on an open tooltip dismisses it and cancels the browser's own handling (a
 *  modal dialog's close), while a menu or editor listening for Escape still hears it. */
export function useTooltip<T>(same: (a: T, b: T) => boolean): {
  open: Open<T> | null;
  show: (anchor: Anchor, value: T) => void;
  hide: () => void;
  hideNow: () => void;
  dismiss: () => void;
  hold: () => void;
  release: () => void;
} {
  const token = useId();
  const [open, setOpen] = useState<Open<T> | null>(null);
  const linger = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const dismissed = useRef(false);
  const sameValue = useRef(same);
  sameValue.current = same;
  const hold = useCallback(() => clearTimeout(linger.current), []);
  const hideNow = useCallback(() => {
    clearTimeout(linger.current);
    setOpen(null);
  }, []);
  const hide = useCallback(() => {
    clearTimeout(linger.current);
    linger.current = setTimeout(() => setOpen(null), LINGER_MS);
  }, []);
  const release = useCallback(() => {
    dismissed.current = false;
  }, []);
  const dismiss = useCallback(() => {
    dismissed.current = true;
    hideNow();
  }, [hideNow]);
  const show = useCallback((anchor: Anchor, value: T) => {
    if (dismissed.current) return;
    clearTimeout(linger.current);
    setOpen((held) =>
      held !== null &&
      sameValue.current(held.value, value) &&
      sameAnchor(held.anchor, anchor)
        ? held
        : { anchor, value },
    );
  }, []);
  const isOpen = open !== null;
  useEffect(() => {
    if (!isOpen) return;
    document.dispatchEvent(new CustomEvent(OPENED, { detail: token }));
    const onOpened = (event: Event): void => {
      if ((event as CustomEvent<string>).detail !== token) hideNow();
    };
    // Captured on the window and cancelled, so a modal dialog does not also close on it; it still
    // propagates, so a menu open over the target closes on the same Escape.
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      dismiss();
    };
    // A scroll inside a panel taller than the viewport is the reader reading it, not a dismissal.
    const onScroll = (event: Event): void => {
      const target = event.target;
      if (target instanceof Element && target.closest(`[${PANEL_ATTRIBUTE}]`)) {
        return;
      }
      hideNow();
    };
    document.addEventListener(OPENED, onOpened);
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", hideNow);
    return () => {
      document.removeEventListener(OPENED, onOpened);
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", hideNow);
    };
  }, [isOpen, hideNow, dismiss, token]);
  useEffect(() => () => clearTimeout(linger.current), []);
  return { open, show, hide, hideNow, dismiss, hold, release };
}

function sameAnchor(a: Anchor, b: Anchor): boolean {
  return (
    a.left === b.left &&
    a.right === b.right &&
    a.top === b.top &&
    a.bottom === b.bottom
  );
}

/** The element children of `node`, read through `childNodes` so any DOM exposing that serves. */
function elementChildren(node: Node): Element[] {
  return Array.from(node.childNodes).filter(
    (child): child is Element => child.nodeType === 1,
  );
}

/** What a wrapper holds for anchoring and describing: its first element, looking through the
 *  wrappers of any tooltips nested inside it. */
function heldElement(wrapper: Element): Element | undefined {
  const first = (node: Element) =>
    elementChildren(node).find(
      (child) => !child.hasAttribute(HIDDEN_TEXT_ATTRIBUTE),
    );
  let at = first(wrapper);
  while (at?.hasAttribute(WRAPPER_ATTRIBUTE)) at = first(at);
  return at;
}

const FOCUSABLE_TAGS = new Set(["BUTTON", "INPUT", "SELECT", "TEXTAREA"]);

/** Whether `element` takes keyboard focus by itself: a tab stop, not a disabled control. */
function takesFocus(element: Element): boolean {
  const tabindex = element.getAttribute("tabindex");
  if (tabindex !== null) return Number(tabindex) >= 0;
  if (element.tagName === "A") return element.hasAttribute("href");
  if (element.tagName === "SUMMARY") return true;
  return (
    FOCUSABLE_TAGS.has(element.tagName) && !element.hasAttribute("disabled")
  );
}

/** The first element at or under `element` that takes focus, if any. */
function focusTarget(element: Element): Element | undefined {
  if (takesFocus(element)) return element;
  for (const child of elementChildren(element)) {
    const found = focusTarget(child);
    if (found !== undefined) return found;
  }
  return undefined;
}

/** Whether any element at or under `element` has text cut off by its box. */
function overflows(element: Element): boolean {
  if (element.scrollWidth > element.clientWidth) return true;
  return elementChildren(element).some(overflows);
}

/** The anchor for what a wrapper holds: its element's box, or its text's where it holds only text
 *  (a `display: contents` wrapper has no box of its own). */
function heldBox(wrapper: HTMLElement): Anchor {
  const held = heldElement(wrapper);
  if (held !== undefined) {
    const { left, right, top, bottom } = held.getBoundingClientRect();
    return { left, right, top, bottom };
  }
  const range = document.createRange();
  range.selectNodeContents(wrapper);
  const { left, right, top, bottom } = range.getBoundingClientRect();
  return { left, right, top, bottom };
}

const always = () => true;

/** Hover text for `children`, shown at once on pointer or keyboard focus (`:focus-visible`, not a
 *  click's or a script's focus); empty `content` shows none. The wrapper adds no box, so it cannot
 *  sit where only a `tr`, `td` or `li` may.
 *
 *  How the text reaches a screen reader depends on what the wrapper holds, and neither form waits
 *  for the panel to open, since a reading cursor moves without focusing or hovering. A target that
 *  takes focus is described by a hidden copy of the text, beside any description it already has;
 *  one that does not carries the text as visually hidden content, where a reading cursor meets it.
 *  `describes={false}` does neither, for text that repeats the target's own name.
 *  `truncatedOnly` shows the panel only while the target's text is cut off by its box. */
export function Tooltip({
  content,
  placement = "above",
  describes = true,
  truncatedOnly = false,
  children,
}: {
  content: ReactNode;
  placement?: Placement;
  describes?: boolean;
  truncatedOnly?: boolean;
  children: ReactNode;
}): React.ReactElement {
  const id = useId();
  const wrapper = useRef<HTMLSpanElement>(null);
  const container = useRef<Element | undefined>(undefined);
  const { open, show, hide, hideNow, dismiss, hold, release } =
    useTooltip<null>(always);
  const shown = content !== "" && content !== null && content !== undefined;
  useEffect(() => {
    if (!shown) hideNow();
  }, [shown, hideNow]);
  const textId = `${id}-text`;
  // What in the wrapper takes focus (`undefined` for nothing), known once it is laid out; until
  // then (and on the server) the text is in neither of its screen-reader forms.
  const [target, setTarget] = useState<Element | undefined | null>(null);
  useLayoutEffect(() => {
    const held =
      wrapper.current === null ? undefined : heldElement(wrapper.current);
    setTarget(held === undefined ? undefined : focusTarget(held));
  });
  useLayoutEffect(() => {
    if (!shown || !describes || target === null || target === undefined) {
      return;
    }
    const tokens = (target.getAttribute("aria-describedby") ?? "")
      .split(/\s+/)
      .filter((token) => token !== "");
    target.setAttribute("aria-describedby", [...tokens, textId].join(" "));
    return () => {
      const left = (target.getAttribute("aria-describedby") ?? "")
        .split(/\s+/)
        .filter((token) => token !== "" && token !== textId);
      if (left.length === 0) target.removeAttribute("aria-describedby");
      else target.setAttribute("aria-describedby", left.join(" "));
    };
  }, [target, shown, describes, textId]);
  /** Open at what the wrapper holds, unless the event belongs to a tooltip nested inside it. */
  const showFor = (target: EventTarget) => {
    const own = wrapper.current;
    if (!shown || own === null) return;
    if (
      target instanceof Element &&
      target.closest(`[${WRAPPER_ATTRIBUTE}]`) !== own
    ) {
      return;
    }
    if (truncatedOnly) {
      const held = heldElement(own);
      if (held === undefined || !overflows(held)) return;
    }
    container.current = own.closest("dialog[open]") ?? undefined;
    show(heldBox(own), null);
  };
  return (
    <>
      {/* biome-ignore lint/a11y/noStaticElementInteractions: the wrapper only observes pointer and focus on the target it holds; the target carries its own role */}
      <span
        ref={wrapper}
        className="contents"
        {...{ [WRAPPER_ATTRIBUTE]: "" }}
        onPointerEnter={(event) => {
          if (event.pointerType !== "touch") showFor(event.target);
        }}
        onPointerLeave={() => {
          release();
          hide();
        }}
        // Pressing the target acts on it, so its hover text makes way until the pointer leaves.
        onPointerDown={dismiss}
        onContextMenu={dismiss}
        onFocus={(event) => {
          if (event.target.matches(":focus-visible")) showFor(event.target);
        }}
        onBlur={() => {
          release();
          hideNow();
        }}
      >
        {children}
        {shown && describes && target === undefined && (
          <span className="sr-only" {...{ [HIDDEN_TEXT_ATTRIBUTE]: "" }}>
            {" "}
            ({content})
          </span>
        )}
        {shown && describes && target !== null && target !== undefined && (
          <span id={textId} hidden {...{ [HIDDEN_TEXT_ATTRIBUTE]: "" }}>
            {content}
          </span>
        )}
      </span>
      {open !== null && shown && (
        <TooltipPanel
          id={id}
          anchor={open.anchor}
          placement={placement}
          container={container.current}
          onPointerEnter={hold}
          onPointerLeave={hide}
        >
          {content}
        </TooltipPanel>
      )}
    </>
  );
}
