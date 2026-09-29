import { Buffer } from "buffer";

// isomorphic-git calls a global `Buffer`, which a browser does not define. Imported first by the
// SharedWorker's entry, so it is in place before isomorphic-git runs.

const scope = globalThis as typeof globalThis & { Buffer?: typeof Buffer };
if (scope.Buffer === undefined) scope.Buffer = Buffer;
