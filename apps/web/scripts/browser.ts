import { spawn, spawnSync } from "node:child_process";
import {
  accessSync,
  closeSync,
  constants,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import {
  type Browser,
  chromium,
  type Page,
  type Request,
} from "playwright-core";

// A visible Chrome window a person works in and Claude can attach to: `open` starts the installed
// Chrome, headed unless `--headless` asks otherwise, with a DevTools port on 127.0.0.1 and a
// profile made for this window, and leaves it running; every other command attaches to that window
// over the port, acts on the tab last active in it and detaches.
//
//   bun run browser open <url> [--headless]          open the window, or point its tab at <url>
//   bun run browser shot <out.png> [--selector <selector>] [--full]
//   bun run browser js '<expression>'               print what the expression gives, as JSON
//   bun run browser click <selector> [--timeout ms]  click, then wait for the page's network to settle
//   bun run browser wait <selector> [--timeout ms]   wait until the element is visible
//   bun run browser close                           quit the window and delete its profile
//
// The port hands control of the browser to any local process, so the profile is a new directory
// that should never hold a login (docs/runbooks/widget-browser.md). What `open` started is recorded
// per port in a state file only the user can read: the checkout, the profile, the process and the
// browser's DevTools id. A command drives only the browser the record names, from the checkout that
// opened it, and a record whose process is gone or is not that Chrome is stale and is dropped.

const PORT = portFrom(process.env.THEMIS_BROWSER_PORT ?? "9333");
const ENDPOINT = `http://127.0.0.1:${PORT}`;
const ROOT = realpathSync(path.resolve(import.meta.dir, "..", "..", ".."));
const STATE_DIR = path.join(
  process.env.XDG_STATE_HOME ?? path.join(homedir(), ".local", "state"),
  "themis",
  "widget-browser",
);
const STATE_FILE = path.join(STATE_DIR, `port-${PORT}.json`);
const PROFILE_PREFIX = "themis-widget-browser-";
const STARTUP_MS = 20_000;
const SHUTDOWN_MS = 10_000;
const WAIT_MS = 60_000;
/** How long the network must be quiet for a click's effects to count as settled. */
const QUIET_MS = 500;

/** What `open` records, field by field as it starts the browser: the checkout and profile first,
 *  the process once spawned, and the browser's DevTools id once its port answers. */
interface BrowserRecord {
  root: string;
  profile: string;
  pid?: number;
  browser?: string;
}

/** The record's browser, known to be running as the record says. */
type Ours = Required<BrowserRecord>;

function portFrom(raw: string): number {
  const port = /^\d+$/.test(raw) ? Number(raw) : Number.NaN;
  if (!(port >= 1024 && port <= 65535)) {
    throw new Error(
      `THEMIS_BROWSER_PORT must be a port from 1024 to 65535 (got ${JSON.stringify(raw)})`,
    );
  }
  return port;
}

/** The installed Chrome or Chromium: `THEMIS_CHROME`, the macOS apps, then the names on PATH. */
function chromePath(): string {
  const tried: string[] = [];
  const candidates = [
    process.env.THEMIS_CHROME,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ...[
      "google-chrome",
      "google-chrome-stable",
      "chromium",
      "chromium-browser",
    ].flatMap((name) =>
      (process.env.PATH ?? "")
        .split(path.delimiter)
        .map((dir) => path.join(dir, name)),
    ),
  ];
  for (const candidate of candidates) {
    if (candidate === undefined || candidate === "") continue;
    tried.push(candidate);
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Not there, or not executable: try the next.
    }
  }
  throw new Error(
    `no Chrome or Chromium found; set THEMIS_CHROME to its executable. Looked for:\n  ${tried.join("\n  ")}`,
  );
}

/** The DevTools id (its WebSocket URL) of the browser answering on the port, or undefined for none. */
async function liveBrowser(): Promise<string | undefined> {
  let response: Response;
  try {
    response = await fetch(`${ENDPOINT}/json/version`);
  } catch (error) {
    if (error instanceof TypeError) return undefined;
    throw error;
  }
  if (!response.ok) return undefined;
  const { webSocketDebuggerUrl } = (await response.json()) as {
    webSocketDebuggerUrl?: string;
  };
  if (webSocketDebuggerUrl === undefined) {
    throw new Error(
      `the browser on port ${PORT} names no webSocketDebuggerUrl`,
    );
  }
  return webSocketDebuggerUrl;
}

/** Whether `pid` is the Chrome running on `profile`: its command line names the profile. */
function runsProfile(pid: number, profile: string): boolean {
  const ps = spawnSync("ps", ["-o", "command=", "-p", String(pid)], {
    encoding: "utf8",
  });
  if (ps.error !== undefined) throw ps.error;
  const flag = `--user-data-dir=${profile}`;
  const command = ps.stdout.trim();
  // The flag whole, so a profile whose path extends this one's does not match.
  return (
    ps.status === 0 && (command.endsWith(flag) || command.includes(`${flag} `))
  );
}

function readRecord(): BrowserRecord | undefined {
  let text: string;
  try {
    text = readFileSync(STATE_FILE, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
  return JSON.parse(text) as BrowserRecord;
}

function writeRecord(record: BrowserRecord): void {
  writeFileSync(STATE_FILE, JSON.stringify(record), { mode: 0o600 });
}

/** Delete a profile this tool made: refused for a path not named with the tool's prefix, or one that
 *  is not a real directory, so a damaged record cannot delete anything else. */
function removeProfile(profile: string): void {
  let stat: ReturnType<typeof lstatSync>;
  try {
    stat = lstatSync(profile);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
  if (
    !path.basename(profile).startsWith(PROFILE_PREFIX) ||
    !stat.isDirectory()
  ) {
    throw new Error(
      `the recorded profile ${profile} is not a directory this tool made; not deleting it`,
    );
  }
  rmSync(profile, { recursive: true });
}

function forget(record: BrowserRecord): void {
  removeProfile(record.profile);
  rmSync(STATE_FILE, { force: true });
}

/** Wait until `done` holds, polling; raise `why` past `ms`. */
async function until(
  done: () => Promise<boolean> | boolean,
  ms: number,
  why: string,
): Promise<void> {
  const deadline = Date.now() + ms;
  while (!(await done())) {
    if (Date.now() > deadline) throw new Error(why);
    await Bun.sleep(250);
  }
}

/** The record's browser, when its process is still the Chrome it started; a record whose process is
 *  gone or is something else is stale, and is dropped with its profile. Raises for a record another
 *  `open` is still writing, and for one another checkout made. */
function reconcile(): Ours | undefined {
  const record = readRecord();
  if (record === undefined) return undefined;
  if (record.pid === undefined || record.browser === undefined) {
    const age = Date.now() - statSync(STATE_FILE).mtimeMs;
    if (age < STARTUP_MS + SHUTDOWN_MS) {
      throw new Error(`another \`open\` is starting a browser on port ${PORT}`);
    }
    if (record.pid !== undefined && runsProfile(record.pid, record.profile)) {
      throw new Error(
        `Chrome (process ${record.pid}) was started on port ${PORT} but never recorded as open; quit it, then open again`,
      );
    }
    forget(record);
    return undefined;
  }
  if (!runsProfile(record.pid, record.profile)) {
    forget(record);
    return undefined;
  }
  if (record.root !== ROOT) {
    throw new Error(
      `the browser on port ${PORT} was opened from another checkout, ${record.root}; set THEMIS_BROWSER_PORT to another port`,
    );
  }
  return record as Ours;
}

/** Start the window on the port with a new profile, detached so it outlives this command. The record
 *  is created exclusively before anything starts, so two `open`s cannot both launch. */
async function launch(url: string, headless: boolean): Promise<Ours> {
  const executable = chromePath();
  mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  let lock: number;
  try {
    lock = openSync(STATE_FILE, "wx", 0o600);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      throw new Error(`another \`open\` is starting a browser on port ${PORT}`);
    }
    throw error;
  }
  closeSync(lock);
  const record: BrowserRecord = {
    root: ROOT,
    profile: mkdtempSync(path.join(tmpdir(), PROFILE_PREFIX)),
  };
  writeRecord(record);
  try {
    const child = spawn(
      executable,
      [
        `--remote-debugging-port=${PORT}`,
        `--user-data-dir=${record.profile}`,
        "--no-first-run",
        "--no-default-browser-check",
        ...(headless ? ["--headless=new", "--window-size=1440,900"] : []),
        url,
      ],
      { detached: true, stdio: "ignore" },
    );
    child.unref();
    if (child.pid === undefined) throw new Error(`${executable} did not start`);
    record.pid = child.pid;
    writeRecord(record);
    await until(
      async () => {
        record.browser = await liveBrowser();
        return record.browser !== undefined;
      },
      STARTUP_MS,
      `Chrome did not open its DevTools port ${PORT} within ${STARTUP_MS / 1000} s`,
    );
    if (!runsProfile(child.pid, record.profile)) {
      throw new Error(
        `the browser answering on port ${PORT} is not the Chrome this command started`,
      );
    }
    writeRecord(record);
    return record as Ours;
  } catch (error) {
    if (record.pid !== undefined && runsProfile(record.pid, record.profile)) {
      process.kill(record.pid);
      await until(
        () => !runsProfile(record.pid as number, record.profile),
        SHUTDOWN_MS,
        `Chrome (process ${record.pid}) did not quit; its profile ${record.profile} is kept`,
      );
    }
    forget(record);
    throw error;
  }
}

/** Raise unless the browser answering on the port is the record's. */
async function verify(ours: Ours): Promise<void> {
  const live = await liveBrowser();
  if (live === undefined) {
    throw new Error(
      `Chrome (process ${ours.pid}) is running without its DevTools port ${PORT}; run \`bun run browser close\``,
    );
  }
  if (live !== ours.browser) {
    throw new Error(
      `the browser on port ${PORT} is not the one this tool opened; set THEMIS_BROWSER_PORT to another port`,
    );
  }
}

/** The ids of the verified browser's tabs, most recently active first, as DevTools lists them. */
async function tabs(): Promise<string[]> {
  const listed = (await (await fetch(`${ENDPOINT}/json/list`)).json()) as {
    id: string;
    type: string;
  }[];
  return listed
    .filter((target) => target.type === "page")
    .map((target) => target.id);
}

/** The record's browser, raising when this tool has none open on the port. */
async function required(): Promise<Ours> {
  const ours = reconcile();
  if (ours !== undefined) return ours;
  throw new Error(
    (await liveBrowser()) !== undefined
      ? `a browser this tool did not open answers on port ${PORT}; set THEMIS_BROWSER_PORT to another port`
      : `no browser on port ${PORT}: run \`bun run browser open <url>\` first`,
  );
}

/** The page of the tab with DevTools id `id`. */
async function pageOf(browser: Browser, id: string): Promise<Page> {
  for (const page of browser.contexts().flatMap((each) => each.pages())) {
    const session = await page.context().newCDPSession(page);
    try {
      const { targetInfo } = await session.send("Target.getTargetInfo");
      if (targetInfo.targetId === id) return page;
    } finally {
      await session.detach();
    }
  }
  throw new Error(`the last active tab ${id} is not one Playwright sees`);
}

/** Attach to the record's browser, act on the tab the person was last on, and detach, whatever
 *  `act` does. Raises for a browser with no tab, as a macOS Chrome whose last window was closed
 *  has none: Playwright cannot attach to one. */
async function withPage(
  ours: Ours,
  act: (page: Page, browser: Browser) => Promise<void>,
): Promise<void> {
  await verify(ours);
  const [last] = await tabs();
  if (last === undefined) {
    throw new Error(
      "the browser has no open tab: run `bun run browser open <url>`",
    );
  }
  const browser = await chromium.connectOverCDP(ours.browser);
  try {
    await act(await pageOf(browser, last), browser);
  } finally {
    // Disconnects, leaving the window open, and deletes Playwright's scratch directory.
    await browser.close();
  }
}

/** Quit the record's browser, wait until it has exited, and delete its profile. */
async function close(ours: Ours): Promise<void> {
  // The browser drops the connection as it quits, which can fail the reply; its exit below is what
  // shows the command worked, and the reply's failure is reported only if it did not.
  let reply = "";
  const live = await liveBrowser();
  if (live === undefined || (await tabs()).length === 0) {
    // No port to ask, or no tab for Playwright to attach through: the process is the record's.
    process.kill(ours.pid, "SIGTERM");
  } else {
    await verify(ours);
    const browser = await chromium.connectOverCDP(ours.browser);
    try {
      const session = await browser.newBrowserCDPSession();
      await session.send("Browser.close").catch((error: unknown) => {
        reply = ` (Browser.close: ${String(error)})`;
      });
    } finally {
      await browser.close();
    }
  }
  await until(
    async () =>
      (await liveBrowser()) === undefined &&
      !runsProfile(ours.pid, ours.profile),
    SHUTDOWN_MS,
    `Chrome (process ${ours.pid}) is still running ${SHUTDOWN_MS / 1000} s after it was asked to quit${reply}; its profile ${ours.profile} is kept`,
  );
  forget(ours);
}

/** Click `selector`, then wait until no request has been in flight for `QUIET_MS`: a card's form
 *  posts, redirects and fetches the next page without a page load to wait for. A shared worker's
 *  script is left out: the page sees its request start, and its end reaches only the worker. */
async function clickAndSettle(
  browser: Browser,
  page: Page,
  selector: string,
  timeoutMs: number,
): Promise<void> {
  const workers = new Set<string>();
  const targets = await browser.newBrowserCDPSession();
  targets.on("Target.targetCreated", ({ targetInfo }) => {
    if (targetInfo.type === "shared_worker") workers.add(targetInfo.url);
  });
  await targets.send("Target.setDiscoverTargets", { discover: true });
  const inFlight = new Set<Request>();
  let changed = Date.now();
  const started = (request: Request) => {
    inFlight.add(request);
    changed = Date.now();
  };
  const ended = (request: Request) => {
    inFlight.delete(request);
    changed = Date.now();
  };
  const pending = () =>
    [...inFlight].filter((request) => !workers.has(request.url()));
  page.on("request", started);
  page.on("requestfinished", ended);
  page.on("requestfailed", ended);
  try {
    await page.locator(selector).first().click({ timeout: timeoutMs });
    changed = Date.now();
    const deadline = Date.now() + timeoutMs;
    while (pending().length > 0 || Date.now() - changed < QUIET_MS) {
      if (Date.now() > deadline) {
        const open = pending()
          .map((each) => `${each.resourceType()} ${each.url().slice(0, 120)}`)
          .join(", ");
        throw new Error(
          `the page's network did not settle within ${timeoutMs / 1000} s of clicking ${selector}; still in flight: ${open || "none, but requests kept starting"}`,
        );
      }
      await Bun.sleep(100);
    }
  } finally {
    page.off("request", started);
    page.off("requestfinished", ended);
    page.off("requestfailed", ended);
    await targets.detach();
  }
}

/** Where an element sits, and how far it runs past what its scrolling ancestors and the viewport
 *  show once scrolled into view. */
interface Placement {
  x: number;
  y: number;
  width: number;
  height: number;
  /** Pixels of the element below the lowest edge that clips it. */
  below: number;
  /** Pixels of the element above the highest edge that clips it. */
  above: number;
  viewportWidth: number;
  viewportHeight: number;
}

/** Screenshot the whole of the first element `selector` names, even one taller than the pane it
 *  scrolls in: the viewport is grown until the pane shows all of it, then restored. Raises when
 *  the element still does not fit. */
async function shootElement(
  page: Page,
  selector: string,
  out: string,
): Promise<void> {
  const element = page.locator(selector).first();
  await element.waitFor({ state: "visible", timeout: WAIT_MS });
  const place = () =>
    element.evaluate((node): Placement => {
      node.scrollIntoView({ block: "start" });
      const rect = node.getBoundingClientRect();
      let below = rect.bottom - window.innerHeight;
      let above = -rect.top;
      for (let at = node.parentElement; at !== null; at = at.parentElement) {
        if (at.scrollHeight <= at.clientHeight) continue;
        if (!/auto|scroll|hidden/.test(getComputedStyle(at).overflowY))
          continue;
        const pane = at.getBoundingClientRect();
        below = Math.max(below, rect.bottom - pane.bottom);
        above = Math.max(above, pane.top - rect.top);
      }
      return {
        x: rect.left + window.scrollX,
        y: rect.top + window.scrollY,
        width: rect.width,
        height: rect.height,
        // Whole pixels only: layout places edges at fractions a capture does not lose.
        below: Math.floor(below),
        above: Math.floor(above),
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
      };
    });
  const session = await page.context().newCDPSession(page);
  let grown = false;
  try {
    let placement = await place();
    for (let tries = 0; placement.below > 0 && tries < 3; tries += 1) {
      await session.send("Emulation.setDeviceMetricsOverride", {
        width: placement.viewportWidth,
        height: placement.viewportHeight + placement.below + 8,
        deviceScaleFactor: 0,
        mobile: false,
      });
      grown = true;
      placement = await place();
    }
    if (placement.below > 0 || placement.above > 0) {
      throw new Error(
        `cannot capture the whole of ${selector}: ${placement.above} px stay hidden above and ${placement.below} px below what its pane shows`,
      );
    }
    const { data } = await session.send("Page.captureScreenshot", {
      format: "png",
      clip: {
        x: placement.x,
        y: placement.y,
        width: placement.width,
        height: placement.height,
        scale: 1,
      },
    });
    writeFileSync(out, Buffer.from(data, "base64"));
  } finally {
    if (grown) await session.send("Emulation.clearDeviceMetricsOverride");
    await session.detach();
  }
}

function timeoutFrom(args: string[]): number {
  const raw = option(args, "--timeout");
  if (raw === undefined) return WAIT_MS;
  const ms = /^\d+$/.test(raw) ? Number(raw) : Number.NaN;
  if (!(ms > 0)) {
    throw new Error(
      `--timeout takes milliseconds (got ${JSON.stringify(raw)})`,
    );
  }
  return ms;
}

function option(args: string[], name: string): string | undefined {
  const at = args.indexOf(name);
  if (at === -1) return undefined;
  const value = args[at + 1];
  if (value === undefined) throw new Error(`${name} needs a value`);
  return value;
}

async function main(args: string[]): Promise<void> {
  const [command, ...rest] = args;
  switch (command) {
    case "open": {
      const url = rest.find((arg) => !arg.startsWith("--"));
      if (url === undefined) throw new Error("open needs a URL");
      const ours = reconcile();
      if (ours !== undefined) {
        await verify(ours);
        if ((await tabs()).length === 0) {
          const opened = await fetch(`${ENDPOINT}/json/new?${url}`, {
            method: "PUT",
          });
          if (!opened.ok) {
            throw new Error(`the browser opened no tab: ${opened.status}`);
          }
          console.log(`opened a tab at ${url}`);
          return;
        }
        await withPage(ours, async (page) => {
          await page.goto(url);
        });
        console.log(`navigated to ${url}`);
        return;
      }
      if ((await liveBrowser()) !== undefined) {
        throw new Error(
          `a browser this tool did not open answers on port ${PORT}; set THEMIS_BROWSER_PORT to another port`,
        );
      }
      const opened = await launch(url, rest.includes("--headless"));
      console.log(
        `opened ${url} (DevTools port ${PORT}, profile ${opened.profile})`,
      );
      return;
    }
    case "shot": {
      const out = rest[0];
      if (out === undefined || out.startsWith("--"))
        throw new Error("shot needs an output path");
      const selector = option(rest, "--selector");
      await withPage(await required(), async (page) => {
        if (selector === undefined) {
          await page.screenshot({
            path: out,
            fullPage: rest.includes("--full"),
          });
        } else {
          await shootElement(page, selector, out);
        }
      });
      console.log(`wrote ${out}`);
      return;
    }
    case "js": {
      const expression = rest[0];
      if (expression === undefined)
        throw new Error("js needs a JavaScript expression");
      await withPage(await required(), async (page) => {
        console.log(JSON.stringify(await page.evaluate(expression), null, 2));
      });
      return;
    }
    case "click": {
      const selector = rest[0];
      if (selector === undefined || selector.startsWith("--"))
        throw new Error("click needs a selector");
      const timeout = timeoutFrom(rest);
      await withPage(await required(), async (page, browser) => {
        await clickAndSettle(browser, page, selector, timeout);
      });
      console.log(`clicked ${selector}`);
      return;
    }
    case "wait": {
      const selector = rest[0];
      if (selector === undefined || selector.startsWith("--"))
        throw new Error("wait needs a selector");
      const timeout = timeoutFrom(rest);
      await withPage(await required(), async (page) => {
        await page
          .locator(selector)
          .first()
          .waitFor({ state: "visible", timeout });
      });
      console.log(`found ${selector}`);
      return;
    }
    case "close": {
      const hadRecord = readRecord() !== undefined;
      const ours = reconcile();
      if (
        ours === undefined &&
        hadRecord &&
        (await liveBrowser()) === undefined
      ) {
        console.log(
          "the recorded browser had already quit; deleted its profile",
        );
        return;
      }
      await close(ours ?? (await required()));
      console.log("closed the browser and deleted its profile");
      return;
    }
    default:
      throw new Error(
        `unknown command ${JSON.stringify(command)}: open, shot, js, click, wait or close`,
      );
  }
}

// Exit when done, so nothing Playwright leaves scheduled holds the command; the window keeps running
// for the person using it.
main(process.argv.slice(2)).then(
  () => process.exit(0),
  (error: unknown) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  },
);
