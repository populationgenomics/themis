import { headers } from "next/headers";
import Link from "next/link";
import { getUserIdentity } from "@/server/identity";
import { curationStore } from "..";
import type { Role } from "../model";

// The curation module's half of the landing page. Self-contained: it resolves its own caller and
// its own role, and renders nothing for someone who holds neither — so the landing page composes it
// without knowing what a curation role is, and deleting the module is deleting this import.
//
// It renders beside the Projects grid, never inside it: a curation holds no dataset, no Analysis and
// no membership, so a card in that grid would falsify the Projects section's own description.

export async function CurationPanel() {
  const email = await getUserIdentity().assertedEmail(await headers());
  // Contained, and loudly logged. This panel renders on the landing page for everyone, so an
  // unapplied migration or a missing grant would otherwise 500 the Projects list for people who
  // have nothing to do with curation.
  let role: Role | undefined;
  try {
    role = await curationStore().roleOf(email);
  } catch (error) {
    console.error("curation panel: could not resolve the caller's role", error);
    return null;
  }
  if (role === undefined) return null;

  return (
    <section className="flex flex-col gap-[6px] border-t border-line-soft pt-[24px]">
      <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-ink-primary">
        Curation
      </h2>
      <p className="text-[13px] text-ink-muted">
        SVCv4 worksheets assigned to you. Separate from Projects: a curation
        records one curator's judgement on one variant, unaided.
      </p>
      <Link
        href="/curation"
        className="mt-[6px] w-fit rounded-card border border-line-primary bg-white px-[18px] py-[12px] text-[13px] text-ink-body hover:border-line-input"
      >
        Open curation
        {role === "manager" ? " — you can assign curators" : ""}
      </Link>
    </section>
  );
}
