"use client";

import { Check } from "lucide-react";
import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { useEffect, useId, useRef, useState } from "react";
import { cn } from "@/lib/utils";

// A dependency-free dropdown: a caller-styled trigger plus a popover menu that
// closes on outside-click, Escape and Tab. The `menu` role promises the
// menu-button keyboard pattern, so arrows, Home and End move focus between
// items and closing returns focus to the trigger.

export interface MenuItem {
  key: string;
  label: ReactNode;
  onSelect: () => void;
  /** Present makes the item a single-select radio; the check marks the active one. */
  selected?: boolean;
}

export function DropdownMenu({
  children,
  items,
  emptyLabel,
  triggerClassName,
  menuClassName,
  itemClassName,
  align = "start",
  ariaLabel,
}: {
  /** Trigger button contents. */
  children: ReactNode;
  items: MenuItem[];
  /** Shown in place of the items when there are none. */
  emptyLabel?: ReactNode;
  /** Classes for the trigger button (the caller owns its look). */
  triggerClassName?: string;
  /** Geometry for the popover panel — width, radius, scroll cap. */
  menuClassName?: string;
  /** Geometry for every item row. */
  itemClassName?: string;
  align?: "start" | "end";
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [focusOnOpen, setFocusOnOpen] = useState<"first" | "last" | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  function close({ restoreFocus }: { restoreFocus: boolean }) {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!open || focusOnOpen === null) return;
    focusItem(menuRef.current, focusOnOpen === "first" ? 0 : -1);
    setFocusOnOpen(null);
  }, [open, focusOnOpen]);

  function onTriggerClick(e: MouseEvent<HTMLButtonElement>) {
    const next = !open;
    setOpen(next);
    // A keyboard-activated click reports no pointer detail; that entry point
    // moves into the menu, a mouse click leaves focus on the trigger.
    if (next && e.detail === 0) setFocusOnOpen("first");
  }

  function onTriggerKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    setOpen(true);
    setFocusOnOpen(e.key === "ArrowDown" ? "first" : "last");
  }

  function onMenuKeyDown(e: KeyboardEvent<HTMLDivElement>) {
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
        // Tab leaves the menu; focus returns to the trigger rather than to the
        // item being unmounted.
        e.preventDefault();
        close({ restoreFocus: true });
        break;
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        className={triggerClassName}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={ariaLabel}
        onClick={onTriggerClick}
        onKeyDown={onTriggerKeyDown}
      >
        {children}
      </button>
      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          onKeyDown={onMenuKeyDown}
          className={cn(
            "absolute z-40 mt-1 min-w-full overflow-hidden rounded-button border border-line-primary bg-white py-1 shadow-[0_8px_24px_rgba(0,0,0,0.10)]",
            align === "end" ? "right-0" : "left-0",
            menuClassName,
          )}
        >
          {items.length === 0
            ? emptyLabel
            : items.map((item) => (
                <MenuItemButton
                  key={item.key}
                  item={item}
                  className={itemClassName}
                  onSelect={() => {
                    item.onSelect();
                    close({ restoreFocus: true });
                  }}
                />
              ))}
        </div>
      )}
    </div>
  );
}

function MenuItemButton({
  item,
  className,
  onSelect,
}: {
  item: MenuItem;
  className?: string;
  onSelect: () => void;
}) {
  const buttonClassName = cn(
    "flex w-full items-center justify-between gap-3 whitespace-nowrap px-3 py-1.5 text-left text-[13px] text-ink-label hover:bg-surface-warm-panel",
    className,
  );
  const content = (
    <>
      <span className="min-w-0 flex-1">{item.label}</span>
      {item.selected && (
        <Check className="size-3.5 shrink-0 text-teal-fg" aria-hidden />
      )}
    </>
  );
  // An item carrying `selected` is one option of a single-select group; without
  // it the item is a plain action. The roles are spelled out rather than
  // computed so `aria-checked` stays statically checkable against the role.
  return item.selected === undefined ? (
    <button
      type="button"
      role="menuitem"
      tabIndex={-1}
      onClick={onSelect}
      className={buttonClassName}
    >
      {content}
    </button>
  ) : (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={item.selected}
      tabIndex={-1}
      onClick={onSelect}
      className={buttonClassName}
    >
      {content}
    </button>
  );
}

function menuItems(menu: HTMLElement | null): HTMLElement[] {
  if (!menu) return [];
  return Array.from(menu.querySelectorAll<HTMLElement>('[role^="menuitem"]'));
}

function focusItem(menu: HTMLElement | null, index: number): void {
  const focusable = menuItems(menu);
  if (focusable.length === 0) return;
  const wrapped = (index + focusable.length) % focusable.length;
  focusable[wrapped].focus();
}
