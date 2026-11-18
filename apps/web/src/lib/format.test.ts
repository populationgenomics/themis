import { describe, expect, test } from "bun:test";
import { absoluteTime, timeAgo } from "./format";

// `timeAgo` takes `now` so it is a pure function of its inputs; these pin the boundaries a card
// crosses as an Analysis ages, and that a bad instant fails rather than rendering "NaN min ago".

const NOW = Date.parse("2026-08-06T12:00:00Z");

function ago(ms: number): string {
  return timeAgo(new Date(NOW - ms).toISOString(), NOW);
}

const zoneCache = new Map<
  string,
  {
    pinnedTime: string;
    readerTime: string;
    pinnedDate?: string;
    readerDate?: string;
  }
>();

/** Run `format.ts` in a fresh child process initialised with `TZ = zone` — JSC resolves its
 *  default zone at process start, so a fresh process tests `clock = "reader"` without mutating
 *  this worker's environment. */
function formatInTimeZone(
  zone: string,
  input: { iso: string; now?: number },
): {
  pinnedTime: string;
  readerTime: string;
  pinnedDate?: string;
  readerDate?: string;
} {
  const key = `${zone}:${JSON.stringify(input)}`;
  const cached = zoneCache.get(key);
  if (cached !== undefined) return cached;
  const proc = Bun.spawnSync(
    [
      process.execPath,
      "-e",
      `import { absoluteTime, timeAgo } from "./src/lib/format";
       const { iso, now } = JSON.parse(process.argv[1]);
       console.log(JSON.stringify({
         pinnedTime: absoluteTime(iso),
         readerTime: absoluteTime(iso, "reader"),
         ...(now === undefined
           ? {}
           : {
               pinnedDate: timeAgo(iso, now, "pinned"),
               readerDate: timeAgo(iso, now, "reader"),
             }),
       }));`,
      JSON.stringify(input),
    ],
    {
      cwd: `${import.meta.dir}/../..`,
      env: {
        ...process.env,
        TZ: zone,
        LC_ALL: "en_US.UTF-8",
        LANG: "en_US.UTF-8",
      },
    },
  );
  if (proc.exitCode !== 0) {
    throw new Error(proc.stderr.toString());
  }
  const parsed = JSON.parse(proc.stdout.toString()) as {
    pinnedTime: string;
    readerTime: string;
    pinnedDate?: string;
    readerDate?: string;
  };
  zoneCache.set(key, parsed);
  return parsed;
}

describe("how a card dates an Analysis", () => {
  test.each([
    ["under a minute", 30 * 1000, "just now"],
    ["minutes", 5 * 60 * 1000, "5 min ago"],
    ["hours", 3 * 60 * 60 * 1000, "3 h ago"],
    ["days", 2 * 24 * 60 * 60 * 1000, "2 d ago"],
  ])("%s", (_label, elapsed, expected) => {
    expect(ago(elapsed as number)).toBe(expected as string);
  });

  test("past a week the elapsed form stops carrying the distinction, so the date is shown", () => {
    expect(ago(30 * 24 * 60 * 60 * 1000)).toBe("7 Jul 2026");
  });

  test("past a week the reader clock follows the reader's zone while pinned stays on UTC", () => {
    // 2026-08-05T23:30:00Z is already 6 August in UTC+14: `pinned` stays on the UTC date, while
    // `reader` formats in the host's zone.
    const input = {
      iso: "2026-08-05T23:30:00Z",
      now: Date.parse("2026-09-05T00:00:00Z"),
    };
    const utc = formatInTimeZone("UTC", input);
    const kiritimati = formatInTimeZone("Pacific/Kiritimati", input);
    expect(utc.pinnedDate).toBe("5 Aug 2026");
    expect(kiritimati.pinnedDate).toBe("5 Aug 2026");
    expect(utc.readerDate).toMatch(/(?<!\d)0?5(?!\d)/);
    expect(utc.readerDate).toContain("2026");
    expect(kiritimati.readerDate).toMatch(/(?<!\d)0?6(?!\d)/);
    expect(kiritimati.readerDate).not.toMatch(/(?<!\d)0?5(?!\d)/);
    expect(kiritimati.readerDate).toContain("2026");
  });

  test("an unparseable instant raises rather than rendering nonsense", () => {
    expect(() => timeAgo("not-a-date", NOW)).toThrow("not an instant");
  });
});

describe("the absolute form a curator hovers", () => {
  test("is the same UTC string across host timezones", () => {
    const input = { iso: "2026-08-06T04:49:00Z" };
    const utc = formatInTimeZone("UTC", input).pinnedTime;
    const kiritimati = formatInTimeZone("Pacific/Kiritimati", input).pinnedTime;
    // Apple ICU joins `en-GB` date and time with " at "; Linux ICU joins with ",".
    expect(utc).toMatch(/^6 Aug 2026(?:,| at) 04:49 UTC$/);
    expect(kiritimati).toBe(utc);
  });

  test("on the reader's clock it drops the zone label", () => {
    const input = { iso: "2026-08-06T04:49:00Z" };
    expect(formatInTimeZone("UTC", input).readerTime).not.toContain("UTC");
    expect(
      formatInTimeZone("Pacific/Kiritimati", input).readerTime,
    ).not.toMatch(/UTC|GMT/);
  });

  test("on the reader's clock it shifts with the host zone", () => {
    const input = { iso: "2026-08-06T04:49:00Z" };
    const utc = formatInTimeZone("UTC", input);
    const kiritimati = formatInTimeZone("Pacific/Kiritimati", input);
    expect(utc.readerTime).toMatch(/0?4[:.]49/);
    expect(kiritimati.readerTime).toMatch(/(?:18|0?6)[:.]49/);
    expect(kiritimati.readerTime).not.toBe(utc.readerTime);
  });

  test("a bad instant raises rather than rendering", () => {
    expect(() => absoluteTime("not-a-date")).toThrow("not an instant");
    expect(() => absoluteTime("not-a-date", "reader")).toThrow(
      "not an instant",
    );
  });
});
