import { headers } from "next/headers";
import { userContext } from "@/server/context";
import { MirrorWindowPage } from "./mirror-window";

// A mirror window of the workbench. The page is rendered per request so it carries the email the
// BFF verifies for the caller, which a widget edit made in this window is committed as.

export const dynamic = "force-dynamic";

export default async function PanePage() {
  const { userEmail } = await userContext(await headers());
  return <MirrorWindowPage userEmail={userEmail} />;
}
