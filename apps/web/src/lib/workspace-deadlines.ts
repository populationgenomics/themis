// How long each call on the workspace repository may take at the BFF, which relays it to the sheaf
// service. The table sits outside `server/` so code on both sides of the relay can import it.

/** How long each call to the sheaf service may take before the BFF gives up on it, in milliseconds.
 *  Without one a stalled call holds its request for the service's own 30-minute timeout. */
export interface SheafDeadlines {
  readonly readRefDoc: number;
  readonly signPackUrls: number;
  readonly publish: number;
}

/** The deployment's deadlines. A read fetches one small document, so 10 s: past a cold start of
 *  the service, which scales to zero. Signing is one storage listing and then one signing request
 *  per pack, eight at a time, for up to 256 packs, so 60 s. A publish is at most one 16 MiB request
 *  body sent across Google's network and stored once, so 60 s. */
export const SHEAF_DEADLINES_MS: SheafDeadlines = {
  readRefDoc: 10_000,
  signPackUrls: 60_000,
  publish: 60_000,
};

/** How long a Poll tick waits on its read of the ref document before it answers the workspace as
 *  unavailable, in milliseconds. Under the browser's 2.5 s Poll cadence, so a stalled read never
 *  holds the conversation's events past the tick after it. */
export const POLL_TIP_BUDGET_MS = 1_500;
