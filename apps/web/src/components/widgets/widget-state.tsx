"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useState,
  useSyncExternalStore,
} from "react";

// What a widget holds across its component's mounts: a tick being saved, why one failed. The
// component is thrown away whenever its place in the tree changes, an embed inserted above it, a
// tab switched away and back, a failed read, while what it holds is about its asset, so it lives
// here, one entry per Analysis, asset path and kind, for as long as the window does. Whatever the
// widget, each entry says whether a change made in it is still being saved, which the document's
// header shows for all of them at once.

/** What any widget's state tells the window about it, whatever the widget: how many changes made in
 *  it are still being saved, and a subscription to hear when that may have changed. */
export interface WidgetState {
  /** How many changes made in the widget still wait on their publish: neither accepted nor
   *  refused, nor found to have an outcome nobody knows. */
  unsaved(): number;
  subscribe(listener: () => void): () => void;
}

/** Each widget's state in one window, made on first use and kept for the window's life. */
export class WidgetStates {
  private readonly entries = new Map<
    string,
    { analysisId: string; state: WidgetState }
  >();
  private readonly listeners = new Set<() => void>();

  /** The `kind` state of the asset at `path` in `analysisId`, made with `make` the first time. */
  entry<T extends WidgetState>(
    analysisId: string,
    path: string,
    kind: string,
    make: () => T,
  ): T {
    const key = JSON.stringify([analysisId, path, kind]);
    let found = this.entries.get(key);
    if (found === undefined) {
      const state = make();
      state.subscribe(this.notify);
      found = { analysisId, state };
      this.entries.set(key, found);
    }
    return found.state as T;
  }

  /** How many changes made in `analysisId`'s widgets are still being saved. */
  unsaved(analysisId: string): number {
    let count = 0;
    for (const entry of this.entries.values()) {
      if (entry.analysisId === analysisId) count += entry.state.unsaved();
    }
    return count;
  }

  /** Hear of any change to any widget's state; for React's external-store hook. */
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private readonly notify = (): void => {
    for (const listener of this.listeners) listener();
  };
}

const WidgetStatesContext = createContext<WidgetStates | null>(null);

/** Keeps each widget's state for every surface beneath it. */
export function WidgetStatesProvider({
  children,
}: {
  children: ReactNode;
}): React.ReactElement {
  const [states] = useState(() => new WidgetStates());
  return (
    <WidgetStatesContext.Provider value={states}>
      {children}
    </WidgetStatesContext.Provider>
  );
}

/** The widget states of the window this component draws in. */
export function useWidgetStates(): WidgetStates {
  const states = useContext(WidgetStatesContext);
  if (states === null) {
    throw new Error("a widget is drawn outside a WidgetStatesProvider");
  }
  return states;
}

/** Whether any widget of `analysisId` in this window has a change still being saved. */
export function useWidgetsSaving(analysisId: string): boolean {
  const states = useWidgetStates();
  const saving = () => states.unsaved(analysisId) > 0;
  return useSyncExternalStore(states.subscribe, saving, saving);
}
