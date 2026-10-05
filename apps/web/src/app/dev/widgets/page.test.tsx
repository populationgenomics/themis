import { afterEach, beforeEach, expect, test } from "bun:test";
import {
  getAccessFallbackHTTPStatus,
  isHTTPAccessFallbackError,
} from "next/dist/client/components/http-access-fallback/http-access-fallback";
import { openExamples } from "./actions";
import WidgetBrowserPage from "./page";

// The widget browser exists on the fixture backend alone: on the live one its page and its action
// answer not-found before reading a request, an example or a Project.

let saved: string | undefined;
beforeEach(() => {
  saved = process.env.THEMIS_BACKEND;
  process.env.THEMIS_BACKEND = "live";
});
afterEach(() => {
  if (saved === undefined) delete process.env.THEMIS_BACKEND;
  else process.env.THEMIS_BACKEND = saved;
});

async function statusOf(run: () => Promise<unknown>): Promise<number> {
  try {
    await run();
  } catch (error) {
    if (isHTTPAccessFallbackError(error))
      return getAccessFallbackHTTPStatus(error);
    throw error;
  }
  throw new Error("it answered");
}

test("the page is not found on the live backend", async () => {
  expect(await statusOf(() => WidgetBrowserPage())).toBe(404);
});

test("opening an example is not found on the live backend", async () => {
  const form = new FormData();
  form.set("group", "checklist");
  expect(await statusOf(() => openExamples(form))).toBe(404);
});
