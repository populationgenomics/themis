import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { isResourceNotFoundError } from "@/server/errors";
import type { CurationAccess } from "./access";
import { curationContext } from "./http";

/** The caller's curation access in `projectId`, for a page: a Project the caller holds no role on is
 *  the page's 404, as the workbench answers it. The boundary is handed a GET carrying the
 *  navigation's own headers, so its resource-isolation check sees what the browser sent. */
export async function curationPageAccess(
  projectId: string,
): Promise<CurationAccess> {
  try {
    return await curationContext(
      new Request("http://internal/curation", { headers: await headers() }),
      projectId,
    );
  } catch (error) {
    if (isResourceNotFoundError(error)) notFound();
    throw error;
  }
}
