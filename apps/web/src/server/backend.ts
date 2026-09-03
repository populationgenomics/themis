// Which backend the process runs against, named explicitly by `THEMIS_BACKEND`. Infrastructure
// rather than any one surface's concern: the workbench adapters, the request identity and the
// curation module all select on it.

type EnvLike = Record<string, string | undefined>;

export type Backend = "fixture" | "live";
/** Which backend to build, named explicitly by `THEMIS_BACKEND`. There is no
 *  default, in either direction: the fixture's identity resolver attributes every
 *  request to the seed dev user without verifying an assertion, so a deploy that
 *  lost the variable would authenticate everyone rather than fail. Selecting a
 *  backend is a deliberate act; an absent or unrecognised value is a
 *  misconfiguration. */
export function selectedBackend(env: EnvLike = process.env): Backend {
  const raw = env.THEMIS_BACKEND;
  if (raw === "live") return "live";
  if (raw === "fixture") return "fixture";
  throw new Error(
    `THEMIS_BACKEND must be "fixture" or "live" (got ${JSON.stringify(raw)})`,
  );
}
