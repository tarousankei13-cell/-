/**
 * Mount prefix of the app.
 *
 * The server injects `<base href="...">` into the SPA shell, so the same build
 * works whether the site owns its origin (`/`) or is served under a sub-path
 * (`/s/kazino/`) by shared hosting or a proxy that does not strip its prefix.
 * Everything that builds a URL by hand -- fetch paths, the WebSocket URL, the
 * router basename, plain links -- goes through here.
 */
function detect(): string {
  if (typeof document === "undefined") return "";
  const href = document.querySelector("base")?.getAttribute("href");
  if (!href) return "";
  try {
    return new URL(href, location.origin).pathname.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

export const BASE = detect();

/** Absolute in-app path, including the mount prefix. */
export function withBase(path: string): string {
  return path.startsWith("/") ? `${BASE}${path}` : path;
}
