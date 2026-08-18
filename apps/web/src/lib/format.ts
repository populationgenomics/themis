/** Strip a leading http(s):// scheme from a URL for compact display — a tool
 *  intent that falls back to a `url` target reads cleaner without it. */
export function stripScheme(url: string): string {
  return url.replace(/^https?:\/\//, "");
}

const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** Whose clock a formatted instant is on. A server render has no reader to resolve one from, so it
 *  pins UTC and a fixed locale — deterministic, and the same string the client's first render
 *  produces. `reader` resolves the host's own, which is what a mounted client formats in. */
export type Clock = "pinned" | "reader";

/** How long ago an ISO instant was, as a card labels it: elapsed time while that is
 *  what distinguishes two runs, the calendar date once it is not. `now` is a
 *  parameter so the result is a pure function of its inputs. */
export function timeAgo(
  iso: string,
  now: number = Date.now(),
  clock: Clock = "pinned",
): string {
  const elapsed = now - Date.parse(iso);
  if (!Number.isFinite(elapsed)) throw new Error(`not an instant: ${iso}`);
  if (elapsed < MINUTE_MS) return "just now";
  if (elapsed < HOUR_MS) return `${Math.floor(elapsed / MINUTE_MS)} min ago`;
  if (elapsed < DAY_MS) return `${Math.floor(elapsed / HOUR_MS)} h ago`;
  if (elapsed < 7 * DAY_MS) return `${Math.floor(elapsed / DAY_MS)} d ago`;
  return absoluteDate(iso, clock);
}

const ABSOLUTE: Intl.DateTimeFormatOptions = {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
};

/** The full instant, for a `title` a curator hovers when the elapsed form is too coarse. On the
 *  reader's clock it needs no zone label; pinned, it carries one, because an unmarked UTC string
 *  would read as the curator's own time. */
export function absoluteTime(iso: string, clock: Clock = "pinned"): string {
  const at = new Date(iso);
  // Raise rather than render "Invalid Date" into a title attribute, matching `timeAgo`: a bad
  // instant is a broken row, not a string to show a curator.
  if (Number.isNaN(at.getTime())) throw new Error(`not an instant: ${iso}`);
  if (clock === "reader") return at.toLocaleString(undefined, ABSOLUTE);
  // Explicit components, not dateStyle/timeStyle: those cannot be combined with `timeZoneName`.
  return at.toLocaleString("en-GB", {
    ...ABSOLUTE,
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

function absoluteDate(iso: string, clock: Clock): string {
  const date: Intl.DateTimeFormatOptions = {
    day: "numeric",
    month: "short",
    year: "numeric",
  };
  if (clock === "reader")
    return new Date(iso).toLocaleDateString(undefined, date);
  return new Date(iso).toLocaleDateString("en-GB", {
    ...date,
    timeZone: "UTC",
  });
}
