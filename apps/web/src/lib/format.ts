/** Strip a leading http(s):// scheme from a URL for compact display — a tool
 *  intent that falls back to a `url` target reads cleaner without it. */
export function stripScheme(url: string): string {
  return url.replace(/^https?:\/\//, "");
}
