// How long each call on the workspace repository may take, on both legs: the BFF's call to the
// sheaf service, and the browser's call to the BFF. Both sides import this one table, so the browser
// always waits longer than the BFF does and a slow service fails at the BFF, as the code the relay
// passes through, rather than as a browser timeout over a call still running behind it.

/** How long each call to the sheaf service may take before the BFF gives up on it, in milliseconds.
 *  Without one a stalled call holds its request for the service's own 30-minute timeout. */
export interface SheafDeadlines {
  readonly readRefDoc: number;
  readonly signPackUrls: number;
  readonly publish: number;
}

export type WorkspaceCall = keyof SheafDeadlines;

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

/** What the browser allows beyond the BFF's deadline for the hop between them. */
const RELAY_MARGIN_MS = 10_000;

/** The browser's deadline on each Workbench call that relays a sheaf call. */
export const RELAY_DEADLINES_MS: Readonly<Record<WorkspaceCall, number>> = {
  readRefDoc: SHEAF_DEADLINES_MS.readRefDoc + RELAY_MARGIN_MS,
  signPackUrls: SHEAF_DEADLINES_MS.signPackUrls + RELAY_MARGIN_MS,
  publish: SHEAF_DEADLINES_MS.publish + RELAY_MARGIN_MS,
};
