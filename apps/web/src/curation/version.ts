/** The transcription a worksheet is answered against, pinned per worksheet at assignment.
 *
 *  Bumped by hand when a change to the workflow components alters what an answer *means* — a
 *  reworded option, a changed option set, a workflow added or removed. Not derived from the build:
 *  most builds change nothing a stored answer depends on, and a version that moves on every deploy
 *  pins nothing.
 *
 *  Bumping it does not migrate worksheets in flight. They keep the version they were assigned, and
 *  a comparison across versions is the reader's problem to notice, which is why the value is stored
 *  rather than assumed. */
export const WORKFLOWS_VERSION = "svcv4-pilot-2026-07.5";
