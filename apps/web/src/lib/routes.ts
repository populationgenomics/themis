/** The path of a Project's page. The id comes from the site's ACL, so it is encoded as one
 *  path segment rather than trusted to hold no reserved character. */
export function projectPath(projectId: string): string {
  return `/project/${encodeURIComponent(projectId)}`;
}

/** The path of a page of a Project's curation: its home with no `segments`, else e.g. `"manage"`,
 *  a worksheet id, or `"compare", variantId`. Each segment is encoded as one path segment. */
export function curationPath(
  projectId: string,
  ...segments: readonly string[]
): string {
  return [
    `${projectPath(projectId)}/curation`,
    ...segments.map(encodeURIComponent),
  ].join("/");
}

/** The path of a Project's curation API route, its segments encoded as `curationPath`'s are. */
export function curationApiPath(
  projectId: string,
  ...segments: readonly string[]
): string {
  return [
    `/api/projects/${encodeURIComponent(projectId)}/curation`,
    ...segments.map(encodeURIComponent),
  ].join("/");
}
