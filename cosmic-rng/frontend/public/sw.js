/**
 * The smallest service worker that earns its keep.
 *
 * It exists so the game can be installed to a home screen, and so the static
 * bundle loads instantly on a second visit. It never caches an API response:
 * this game is server-authoritative, and a stale answer served from a cache
 * would be a lie about the player's own state.
 */
const CACHE = "cosmic-static-v2";

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Hashed build assets only. Everything else — the HTML shell, every /api
  // path, the WebSocket — goes to the network, always.
  if (!url.pathname.includes("/assets/")) return;

  e.respondWith(
    caches.match(req).then((hit) => {
      if (hit) return hit;
      return fetch(req).then((res) => {
        if (res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => undefined);
        }
        return res;
      });
    }),
  );
});
