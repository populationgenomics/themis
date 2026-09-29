import type { CopyLocks, LockMode } from "./locks";

// The Web Lock semantics the copy relies on, in memory: shared holders together, an exclusive one
// alone, waiters granted in order. Two services given one instance stand for two SharedWorkers.

interface Waiter {
  mode: LockMode;
  grant: () => void;
}

class ReadWriteLock {
  private shared = 0;
  private exclusive = false;
  private readonly queue: Waiter[] = [];

  free(): boolean {
    return !this.exclusive && this.shared === 0 && this.queue.length === 0;
  }

  acquire(mode: LockMode): Promise<void> {
    return new Promise((grant) => {
      this.queue.push({ mode, grant });
      this.pump();
    });
  }

  release(mode: LockMode): void {
    if (mode === "exclusive") this.exclusive = false;
    else this.shared -= 1;
    this.pump();
  }

  private pump(): void {
    while (this.queue.length > 0) {
      const head = this.queue[0];
      if (head.mode === "exclusive") {
        if (this.exclusive || this.shared > 0) return;
        this.exclusive = true;
      } else {
        if (this.exclusive) return;
        this.shared += 1;
      }
      this.queue.shift();
      head.grant();
    }
  }
}

export function memoryLocks(): CopyLocks {
  const locks = new Map<string, ReadWriteLock>();
  const lockFor = (analysisId: string) => {
    let lock = locks.get(analysisId);
    if (lock === undefined) {
      lock = new ReadWriteLock();
      locks.set(analysisId, lock);
    }
    return lock;
  };
  const hold = async <T>(
    analysisId: string,
    mode: LockMode,
    task: () => Promise<T>,
  ) => {
    const lock = lockFor(analysisId);
    await lock.acquire(mode);
    try {
      return await task();
    } finally {
      lock.release(mode);
    }
  };
  return {
    hold,
    holdIfFree: async (analysisId, task) => {
      if (!lockFor(analysisId).free()) return false;
      await hold(analysisId, "exclusive", task);
      return true;
    },
  };
}
