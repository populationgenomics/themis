"use client";

import { Component, type ReactNode } from "react";

// A subtree that fails to render draws `fallback` in its place, and the rest of the page renders. A
// component that throws takes the whole page down otherwise: React unmounts the root when no
// boundary catches the error.

interface BoundaryProps {
  fallback: (error: Error) => ReactNode;
  children: ReactNode;
  /** What the subtree draws, such as the commit a document is read at: a new one clears a failure
   *  and draws the subtree again, without remounting it, so what it holds outlives the change. */
  resetKey?: string;
}

interface BoundaryState {
  error: Error | null;
  resetKey: string | undefined;
}

export class RenderBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = {
    error: null,
    resetKey: this.props.resetKey,
  };

  static getDerivedStateFromError(error: unknown): Partial<BoundaryState> {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  static getDerivedStateFromProps(
    props: BoundaryProps,
    state: BoundaryState,
  ): Partial<BoundaryState> | null {
    return props.resetKey === state.resetKey
      ? null
      : { error: null, resetKey: props.resetKey };
  }

  override componentDidCatch(error: unknown): void {
    console.error("a part of the page failed to render", error);
  }

  override render(): ReactNode {
    return this.state.error === null
      ? this.props.children
      : this.props.fallback(this.state.error);
  }
}
