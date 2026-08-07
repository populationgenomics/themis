import { type RefObject, useEffect } from "react";

// Adobe-style panning for a scroll container: hold Space to arm (hand/grab cursor), then left-button
// drag to pan. Space is armed only while the pointer is over this element, so in a multi-pane
// workbench the pane under the cursor is the one that pans; while armed, Space's default page-scroll
// is suppressed. Drag maps 1:1 to scroll offset (drag right → content moves right → scrollLeft down),
// via pointer capture so a drag that leaves the element keeps panning until release.
export function useSpacePan(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    let hovering = false;
    let spaceHeld = false;
    let panning = false;
    let pointerId = -1;
    let startX = 0;
    let startY = 0;
    let startLeft = 0;
    let startTop = 0;

    const cursor = () => (panning ? "grabbing" : spaceHeld ? "grab" : "");
    const applyCursor = () => {
      el.style.cursor = cursor();
    };

    const onPointerEnter = () => {
      hovering = true;
    };
    const onPointerLeave = () => {
      hovering = false;
      if (!panning) applyCursor();
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== "Space" || !hovering) return;
      const t = e.target as HTMLElement | null;
      // Never hijack Space from a typing target (search box, editable content) or a focused control
      // that Space activates (a button); those keep their default behaviour.
      if (
        t?.isContentEditable ||
        t?.getAttribute("role") === "button" ||
        (t && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName))
      )
        return;
      // Suppress the page scroll on every keydown, including auto-repeat — the arming below is the
      // one-shot; preventing only the first keydown lets repeats scroll the page mid-pan.
      e.preventDefault();
      if (spaceHeld) return;
      spaceHeld = true;
      applyCursor();
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      spaceHeld = false;
      if (!panning) applyCursor();
    };
    // A keyup delivered to another window (focus left mid-hold: ⌘Tab, clicking into a mirror window)
    // never reaches this listener, so disarm on blur or Space stays stuck armed.
    const onBlur = () => {
      spaceHeld = false;
      if (!panning) applyCursor();
    };

    const onPointerDown = (e: PointerEvent) => {
      if (!spaceHeld || e.button !== 0) return;
      panning = true;
      pointerId = e.pointerId;
      startX = e.clientX;
      startY = e.clientY;
      startLeft = el.scrollLeft;
      startTop = el.scrollTop;
      applyCursor();
      el.setPointerCapture?.(e.pointerId);
      e.preventDefault();
    };
    const onPointerMove = (e: PointerEvent) => {
      // Arm on movement too, not only `pointerenter`: a pane that mounts *under* a stationary cursor
      // (the PDF viewer's lazy `ssr:false` chunk, a representation toggle, a tab switch at the same
      // position) gets no enter event, so Space would arm nothing until the pointer left and re-entered.
      hovering = true;
      if (!panning) return;
      el.scrollLeft = startLeft - (e.clientX - startX);
      el.scrollTop = startTop - (e.clientY - startY);
    };
    const endPan = (e: PointerEvent) => {
      if (!panning || e.pointerId !== pointerId) return;
      panning = false;
      el.releasePointerCapture?.(pointerId);
      applyCursor();
    };

    el.addEventListener("pointerenter", onPointerEnter);
    el.addEventListener("pointerleave", onPointerLeave);
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointermove", onPointerMove);
    el.addEventListener("pointerup", endPan);
    el.addEventListener("pointercancel", endPan);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      el.removeEventListener("pointerenter", onPointerEnter);
      el.removeEventListener("pointerleave", onPointerLeave);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointermove", onPointerMove);
      el.removeEventListener("pointerup", endPan);
      el.removeEventListener("pointercancel", endPan);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
      el.style.cursor = "";
    };
  }, [ref]);
}
