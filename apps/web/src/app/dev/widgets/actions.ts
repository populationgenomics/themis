"use server";

import { headers } from "next/headers";
import { notFound, redirect } from "next/navigation";
import { selectedBackend } from "@/server/backend";
import { userContext } from "@/server/context";
import { exampleDocument, listExamples } from "@/server/widget-examples";

/** Open the examples the form names, read from disk now, in a new Analysis, and go to it: the
 *  form's `group`, and its `name` for one example, or every example in the group without one. */
export async function openExamples(form: FormData): Promise<void> {
  if (selectedBackend() !== "fixture") notFound();
  const group = form.get("group");
  const name = form.get("name");
  if (typeof group !== "string" || group === "") {
    throw new Error("the form names no example group");
  }
  const chosen = listExamples().filter(
    (example) =>
      example.group === group &&
      (typeof name !== "string" || example.name === name),
  );
  if (chosen.length === 0) {
    throw new Error(
      `no example ${group}${typeof name === "string" ? `/${name}` : ""}`,
    );
  }
  const title =
    typeof name === "string"
      ? `Widget example: ${group}/${name}`
      : `Widget examples: ${group}`;
  const { backend } = await userContext(await headers());
  const analysis = await backend.seedDocument(
    title,
    exampleDocument(title, chosen),
  );
  redirect(`/analysis/${analysis.id}`);
}
