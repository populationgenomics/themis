// Which runtime drove a run, read off the session id its analysis carries. A run the platform holds
// has a conversation the workbench can read and steer; one driven elsewhere has a working document
// and nothing else here.

/** The prefix a session id carries when the run was driven outside the workbench. */
export const UNMANAGED_SESSION_PREFIX = "pi_";

/** Whether the platform holds this run's session. Unrecognised ids read as the platform's, so a
 *  surprise surfaces as a failed read rather than as a run with nothing to say. */
export function isManagedSession(sessionId: string): boolean {
  return !sessionId.startsWith(UNMANAGED_SESSION_PREFIX);
}
