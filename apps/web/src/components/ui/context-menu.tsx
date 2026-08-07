"use client";

import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// useLayoutEffect warns under SSR; this component renders (closed) in the server-rendered workbench
// tree, so fall back to useEffect on the server where the effect is a no-op anyway.
const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

// A dependency-free context menu: wraps a target, opens a `role="menu"` popover at the pointer on
// right-click / long-press, and — because the browser dispatches `contextmenu` for `Shift+F10` and
// the Menu key on the focused target too — is fully keyboard-operable. The `menu` role carries the
// menu-button keyboard pattern (arrows, Home, End); Escape and selecting an item return focus to the
// element that was focused when it opened. The panel is portalled to the body and clamped to the
// viewport so a target at a screen edge (the right group's strip) does not open off-screen.

export interface ContextMenuItem {
  key: string;
  label: ReactNode;
  onSelect: () => void;
}

interface Position {
  x: number;
  y: number;
}

export function ContextMenu({
  children,
  items,
  ariaLabel,
}: {
  /** The target the menu opens over; rendered inline (the wrapper adds no box). */
  children: ReactNode;
  items: ContextMenuItem[];
  ariaLabel?: string;
}): React.ReactElement {
  const [pos, setPos] = useState<Position | null>(null);
  const open = pos !== null;
  const menuRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  function close({ restoreFocus }: { restoreFocus: boolean }): void {
    setPos(null);
    if (restoreFocus) restoreFocusRef.current?.focus();
  }

  function onContextMenu(e: MouseEvent<HTMLSpanElement>): void {
    if (items.length === 0) return;
    e.preventDefault();
    const focused = document.activeElement as HTMLElement | null;
    restoreFocusRef.current = focused;
    // Keyboard invocation (Shift+F10 / Menu key) can report a 0,0 point; anchor to the focused control
    // then — not `currentTarget`, the `display:contents` wrapper, whose rect is empty (it has no box).
    if (e.clientX <= 0 && e.clientY <= 0 && focused) {
      const rect = focused.getBoundingClientRect();
      setPos({ x: rect.left, y: rect.bottom });
    } else {
      setPos({ x: e.clientX, y: e.clientY });
    }
  }

  // Focus the first item on open (whether invoked by mouse or keyboard), so the menu is arrow-ready.
  useEffect(() => {
    if (open) focusItem(menuRef.current, 0);
  }, [open]);

  // Clamp into the viewport after the panel has a measured size, before paint.
  useIsomorphicLayoutEffect(() => {
    if (!open || !menuRef.current || pos === null) return;
    const menu = menuRef.current;
    const rect = menu.getBoundingClientRect();
    const x = Math.max(8, Math.min(pos.x, window.innerWidth - rect.width - 8));
    const y = Math.max(
      8,
      Math.min(pos.y, window.innerHeight - rect.height - 8),
    );
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
  }, [open, pos]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent): void => {
      if (!menuRef.current?.contains(e.target as Node)) setPos(null);
    };
    const onKeyDown = (e: globalThis.KeyboardEvent): void => {
      if (e.key === "Escape") {
        setPos(null);
        restoreFocusRef.current?.focus();
      }
    };
    // A floating menu detaches from its anchor on scroll/resize; dismiss it, restoring focus — a
    // keyboard user may have scrolled off it (unlike an outside pointer-down, which leaves focus where
    // the user clicked).
    const onDismiss = (): void => {
      setPos(null);
      restoreFocusRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onDismiss);
    window.addEventListener("scroll", onDismiss, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onDismiss);
      window.removeEventListener("scroll", onDismiss, true);
    };
  }, [open]);

  function onMenuKeyDown(e: KeyboardEvent<HTMLDivElement>): void {
    const focusable = menuItems(menuRef.current);
    const current = focusable.indexOf(document.activeElement as HTMLElement);
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        focusItem(menuRef.current, current + 1);
        break;
      case "ArrowUp":
        e.preventDefault();
        focusItem(menuRef.current, current - 1);
        break;
      case "Home":
        e.preventDefault();
        focusItem(menuRef.current, 0);
        break;
      case "End":
        e.preventDefault();
        focusItem(menuRef.current, focusable.length - 1);
        break;
      case "Tab":
        e.preventDefault();
        close({ restoreFocus: true });
        break;
    }
  }

  return (
    <>
      {/* `display: contents` so the wrapper carries the contextmenu handler without altering layout. */}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: the wrapper only delegates the contextmenu
          event (fired by right-click, long-press, and Shift+F10/Menu on the focusable child); the child is
          the real control and carries the keyboard affordance, so the wrapper needs no role. */}
      <span className="contents" onContextMenu={onContextMenu}>
        {children}
      </span>
      {open &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label={ariaLabel}
            onKeyDown={onMenuKeyDown}
            style={{ position: "fixed", left: pos.x, top: pos.y }}
            className="z-50 min-w-[168px] overflow-hidden rounded-button border border-line-primary bg-white py-1 shadow-[0_8px_24px_rgba(0,0,0,0.10)]"
          >
            {items.map((item) => (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={() => {
                  item.onSelect();
                  close({ restoreFocus: true });
                }}
                className="flex w-full items-center whitespace-nowrap px-3 py-1.5 text-left text-[13px] text-ink-label hover:bg-surface-warm-panel"
              >
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}

function menuItems(menu: HTMLElement | null): HTMLElement[] {
  if (!menu) return [];
  return Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]'));
}

function focusItem(menu: HTMLElement | null, index: number): void {
  const focusable = menuItems(menu);
  if (focusable.length === 0) return;
  const wrapped = (index + focusable.length) % focusable.length;
  focusable[wrapped].focus();
}
