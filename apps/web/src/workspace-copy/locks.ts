// The per-Analysis lock every use of a copy holds: a Web Lock, so the SharedWorkers of two builds
// open in one browser see each other's. Writes hold it exclusively, reads shared, and eviction takes
// it only if nobody holds it.

export type LockMode = "exclusive" | "shared";

export interface CopyLocks {
  /** Run `task` holding `analysisId`'s lock in `mode`, waiting for it. */
  hold<T>(
    analysisId: string,
    mode: LockMode,
    task: () => Promise<T>,
  ): Promise<T>;
  /** Run `task` holding `analysisId`'s lock exclusively if nobody holds it, in any worker;
   *  resolves whether it ran. */
  holdIfFree(analysisId: string, task: () => Promise<void>): Promise<boolean>;
}

/** Named apart from LightningFS's own lock, which it takes as `<database>_lock`. */
function lockName(analysisId: string): string {
  return `themis-workspace-copy-lock:${analysisId}`;
}

export function webLocks(locks: LockManager): CopyLocks {
  return {
    hold: async (analysisId, mode, task) =>
      await locks.request(lockName(analysisId), { mode }, task),
    holdIfFree: async (analysisId, task) =>
      await locks.request(
        lockName(analysisId),
        { mode: "exclusive", ifAvailable: true },
        async (lock) => {
          if (lock === null) return false;
          await task();
          return true;
        },
      ),
  };
}
