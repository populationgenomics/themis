import { toJson } from "@bufbuild/protobuf";
import {
  type Assessment,
  AssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";

// The worksheet's one path to the server. A section — a workflow, the routing, the case, the
// verdict — is written after a short idle or on leaving a field, skipped when its payload matches
// what the server last accepted, and retried on a timer when the write fails. The indicator reads
// "saved" only when no write is out, in flight or failed: the rarity gate issues several writes at
// once, and one landing must not read as all landed.
//
// Per section, writes go one after another and a timer always writes the newest assessment, so
// two writes cannot land in the wrong order and a retry cannot put an older value over a newer one.

export type SaveState = "idle" | "saving" | "saved" | "failed";

/** Thrown by `settle` where a section's write is still failing after every pending write has been
 *  tried: what the server holds is not what is on screen. */
export class UnsavedSectionsError extends Error {
  constructor(readonly sectionIds: string[]) {
    super(`not saved: ${sectionIds.join(", ")}`);
    this.name = "UnsavedSectionsError";
  }
}

export interface AutosaveOptions {
  /** The proto-JSON the server already holds, by section id. */
  stored: Record<string, string>;
  /** Put one section's proto-JSON payload; rejects where the server did not accept it. */
  write: (sectionId: string, payload: string) => Promise<void>;
  onState: (state: SaveState) => void;
  idleMs: number;
  retryMs: number;
}

export class Autosave {
  private readonly stored: Record<string, string>;
  private readonly latest = new Map<string, Assessment>();
  private readonly timers = new Map<string, ReturnType<typeof setTimeout>>();
  /** The section's writes so far, so the next one starts after the last has settled. */
  private readonly queue = new Map<string, Promise<void>>();
  private readonly failing = new Set<string>();
  private inFlight = 0;

  constructor(private readonly options: AutosaveOptions) {
    this.stored = { ...options.stored };
  }

  /** Write one section's assessment now. Skipped where the server already holds it. */
  async commit(sectionId: string, assessment: Assessment): Promise<void> {
    this.latest.set(sectionId, assessment);
    this.clearTimer(sectionId);
    const previous = this.queue.get(sectionId) ?? Promise.resolve();
    const run = previous.then(() => this.write(sectionId, assessment));
    this.queue.set(sectionId, run);
    await run;
  }

  /** Write after the idle window. A later call for the same section supersedes an earlier one. */
  schedule(sectionId: string, assessment: Assessment): void {
    this.latest.set(sectionId, assessment);
    this.setTimer(sectionId, this.options.idleMs);
  }

  /** Write the section's newest assessment now — bound to a control losing focus. */
  flush(sectionId: string): void {
    const assessment = this.latest.get(sectionId);
    if (assessment !== undefined) void this.commit(sectionId, assessment);
  }

  /** Write every section's newest assessment now and wait for every write to land, so what the
   *  server holds is what is on screen — for a caller about to have the server read the drafts.
   *  Rejects with `UnsavedSectionsError` where a section is still failing afterwards; its retry stays
   *  armed. */
  async settle(): Promise<void> {
    for (const sectionId of this.latest.keys()) this.flush(sectionId);
    await Promise.all(this.queue.values());
    if (this.failing.size > 0) {
      throw new UnsavedSectionsError([...this.failing]);
    }
  }

  /** Stop every timer: nothing writes once the worksheet has left the screen. */
  dispose(): void {
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
  }

  /** Never rejects: a failure is recorded and retried, and the section's queue carries on. */
  private async write(
    sectionId: string,
    assessment: Assessment,
  ): Promise<void> {
    const payload = JSON.stringify(toJson(AssessmentSchema, assessment));
    if (this.stored[sectionId] === payload) {
      // Reverted to what the server holds: a section whose write failed is saved again.
      if (this.failing.delete(sectionId)) this.publish();
      return;
    }
    this.inFlight += 1;
    this.publish();
    try {
      await this.options.write(sectionId, payload);
      this.stored[sectionId] = payload;
      this.failing.delete(sectionId);
    } catch {
      // The value stays on screen and stays unsaved; a curator who walks away mid-outage should
      // come back to saved work.
      this.failing.add(sectionId);
      this.setTimer(sectionId, this.options.retryMs);
    } finally {
      this.inFlight -= 1;
      this.publish();
    }
  }

  private setTimer(sectionId: string, ms: number): void {
    this.clearTimer(sectionId);
    this.timers.set(
      sectionId,
      setTimeout(() => {
        this.timers.delete(sectionId);
        this.flush(sectionId);
      }, ms),
    );
  }

  private clearTimer(sectionId: string): void {
    const timer = this.timers.get(sectionId);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.timers.delete(sectionId);
    }
  }

  private publish(): void {
    this.options.onState(
      this.failing.size > 0 ? "failed" : this.inFlight > 0 ? "saving" : "saved",
    );
  }
}
