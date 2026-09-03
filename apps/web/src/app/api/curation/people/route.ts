import { NextResponse } from "next/server";
import {
  curationContext,
  jsonBody,
  requiredString,
  runCuration,
} from "@/curation/http";

/** POST /api/curation/people — grant someone the curator role. Manager only. */
export async function POST(request: Request): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const body = await jsonBody(request);
    const person = await access.addCurator(requiredString(body, "email"));
    return NextResponse.json({ email: person.email, role: person.role });
  });
}
