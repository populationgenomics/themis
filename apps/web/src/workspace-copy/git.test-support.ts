import { execFileSync } from "node:child_process";
import { promises as fsp, mkdtempSync, rmSync } from "node:fs";
import { devNull, tmpdir } from "node:os";
import path from "node:path";
import type { CopyFsPromises, CopyStorage } from "./copy";

// Test support for the browser's copy: the real `git` as an oracle, and a copy's storage on a
// directory of the host's filesystem.

/** Run `git` against `gitDir` with no caller repository or user configuration leaking in. */
export function runGit(
  gitDir: string,
  args: readonly string[],
  input: string | Uint8Array = "",
  env: Record<string, string> = {},
): Buffer {
  const base = { ...process.env };
  for (const key of Object.keys(base)) {
    if (key.startsWith("GIT_")) delete base[key];
  }
  return execFileSync("git", args, {
    input,
    env: {
      ...base,
      GIT_CONFIG_GLOBAL: devNull,
      GIT_CONFIG_NOSYSTEM: "1",
      LC_ALL: "C",
      GIT_DIR: gitDir,
      ...env,
    },
    stdio: ["pipe", "pipe", "pipe"],
    maxBuffer: 256 * 1024 * 1024,
  });
}

export function gitText(
  gitDir: string,
  args: readonly string[],
  input: string | Uint8Array = "",
  env: Record<string, string> = {},
): string {
  return runGit(gitDir, args, input, env).toString("utf8").trim();
}

/** A scratch directory removed by the returned cleanup. */
export function scratchDir(prefix: string): {
  dir: string;
  remove: () => void;
} {
  const dir = mkdtempSync(path.join(tmpdir(), `themis-copy-${prefix}-`));
  return { dir, remove: () => rmSync(dir, { recursive: true, force: true }) };
}

/** A bare repository built by `git`, the oracle a copy's objects are compared against. */
export function oracleRepo(dir: string): string {
  const gitDir = path.join(dir, "oracle.git");
  runGit(gitDir, ["init", "--bare", "--quiet", "--object-format=sha1", gitDir]);
  return gitDir;
}

/** What a storage did, in order: each write to a path, each deletion, and each flush. */
export type StorageEvent =
  | { kind: "write" | "unlink"; path: string }
  | { kind: "flush" };

/** A copy's storage on a host directory, recording its writes and flushes so a test can check that
 *  no ref is written before the objects it names are flushed. */
export function directoryStorage(
  gitdir: string,
): CopyStorage & { events: StorageEvent[] } {
  const events: StorageEvent[] = [];
  const promises: CopyFsPromises = {
    readFile: (p, options) =>
      fsp.readFile(p, options as never).then(asBytesOrText),
    writeFile: async (p, data, options) => {
      events.push({ kind: "write", path: path.relative(gitdir, p) });
      await fsp.writeFile(p, data, options as never);
    },
    unlink: async (p) => {
      events.push({ kind: "unlink", path: path.relative(gitdir, p) });
      await fsp.unlink(p);
    },
    readdir: (p) => fsp.readdir(p),
    mkdir: (p) => fsp.mkdir(p),
    rmdir: (p) => fsp.rmdir(p),
    stat: (p) => fsp.stat(p),
    lstat: (p) => fsp.lstat(p),
    readlink: (p) => fsp.readlink(p),
    symlink: (target, p) => fsp.symlink(target, p),
  };
  return {
    promises,
    gitdir,
    events,
    flush: async () => {
      events.push({ kind: "flush" });
    },
    size: () => directorySize(gitdir),
  };
}

function asBytesOrText(data: Buffer | string): Uint8Array | string {
  return typeof data === "string" ? data : new Uint8Array(data);
}

async function directorySize(dir: string): Promise<number> {
  let total = 0;
  for (const entry of await fsp.readdir(dir, { withFileTypes: true })) {
    const child = path.join(dir, entry.name);
    total += entry.isDirectory()
      ? await directorySize(child)
      : (await fsp.stat(child)).size;
  }
  return total;
}
