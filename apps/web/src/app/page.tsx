import { Suspense } from "react";
import { currentUserEmail } from "@/server/context";
import { Workbench } from "./workbench";

// `Workbench` reads the selected analysis from the URL via `useSearchParams`, which
// requires a Suspense boundary above it (App Router prerendering contract).

// The page resolves the caller before it renders, so there is nothing to prerender:
// without this the build attempts one, and the identity seam it reaches has no
// request to answer from.
export const dynamic = "force-dynamic";

export default async function Home() {
  const userEmail = await currentUserEmail();
  return (
    <Suspense fallback={null}>
      <Workbench userEmail={userEmail} />
    </Suspense>
  );
}
