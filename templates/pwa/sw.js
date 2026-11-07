{% autoescape off %}/* Muzadi School System - service worker (served at /sw.js by mainApp/views/pwa_views.py)
 *
 * Phase 1 (installable app):
 *   - keeps the app shell (icons, main CSS, offline page) on the phone
 *   - pages always come fresh from the server; if there is no internet the
 *     "You are offline" page is shown instead of the browser's error
 *   - private pages and files (/media/, reports, marks) are NEVER stored
 *
 * Phase 2 (offline mark entry):
 *   - keeps a copy of the Offline Marks page so it opens with no internet
 *     (only when the server marked it with X-Muzadi-Shell, never a login page)
 *   - "background sync": lists the teacher tapped Send on are sent when the
 *     phone gets a connection, even if the app is closed (Chrome / Android)
 */
const VERSION = {{ version_json }};
const SHELL_CACHE = "muzadi-shell-" + VERSION;
const STATIC_CACHE = "muzadi-static-" + VERSION;
const OFFLINE_URL = {{ offline_url_json }};
const STATIC_PREFIX = {{ static_prefix_json }};
const PRECACHE = {{ precache_json }};
const MARKS_APP_URL = {{ marks_app_url_json }};
const APP_CACHE = "muzadi-app";   // not versioned: an update must never remove the offline copy
const SYNC_TAG = "muzadi-marks-sync";

// storage + sending code shared with the Offline Marks page
try { importScripts({{ marks_store_url_json }}); } catch (e) { /* offline at first install: page still works */ }

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      // one missing file must not stop the app installing
      Promise.all(PRECACHE.map((url) =>
        cache.add(new Request(url, { cache: "reload", credentials: "same-origin" })).catch(() => null)
      ))
    ).then(refreshMarksApp)
  );
  self.skipWaiting();
});

// Keep the newest Offline Marks page (only if the teacher is signed in).
async function refreshMarksApp() {
  try {
    const res = await fetch(MARKS_APP_URL, { credentials: "same-origin", cache: "reload" });
    if (res.ok && !res.redirected && res.headers.get("X-Muzadi-Shell") === "offline-marks") {
      await (await caches.open(APP_CACHE)).put(MARKS_APP_URL, res);
    }
  } catch (e) { /* offline or signed out: keep the old copy */ }
}

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = [SHELL_CACHE, STATIC_CACHE, APP_CACHE];
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n.startsWith("muzadi-") && !keep.includes(n))
                           .map((n) => caches.delete(n)));
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) { /* not supported */ }
    }
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                       // forms / saves go straight to the server
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // other websites: leave alone

  // Offline Marks page: fresh from the network when possible, else the copy on the phone.
  if (req.mode === "navigate" && url.pathname === MARKS_APP_URL) {
    event.respondWith((async () => {
      const cache = await caches.open(APP_CACHE);
      try {
        const res = await fetch(req);
        if (res.ok && !res.redirected && res.headers.get("X-Muzadi-Shell") === "offline-marks") {
          await cache.put(MARKS_APP_URL, res.clone());
        }
        return res;
      } catch (err) {
        const copy = await cache.match(MARKS_APP_URL, { ignoreSearch: true });
        return copy || (await caches.match(OFFLINE_URL)) ||
               new Response("You are offline.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
      }
    })());
    return;
  }

  // Pages: always from the network; offline page when there is no connection.
  if (req.mode === "navigate") {
    event.respondWith((async () => {
      try {
        const preloaded = await event.preloadResponse;
        if (preloaded) return preloaded;
        return await fetch(req);
      } catch (err) {
        const cached = await caches.match(OFFLINE_URL);
        return cached || new Response("You are offline.", {
          status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      }
    })());
    return;
  }

  // CSS / JS / images / fonts: use the phone's copy, refresh it in the background.
  if (url.pathname.startsWith(STATIC_PREFIX)) {
    event.respondWith((async () => {
      const cache = await caches.open(STATIC_CACHE);
      const cached = (await cache.match(req)) || (await caches.match(req));
      const network = fetch(req).then((res) => {
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      }).catch(() => null);
      if (cached) { event.waitUntil(network); return cached; }
      const res = await network;
      return res || new Response("", { status: 504 });
    })());
  }
  // Everything else (media, JSON, downloads) is private: not cached.
});
// ---------------------------------------------------------------- background sending
async function sendWaitingMarks() {
  if (!self.MuzadiMarks) return;
  const user = await self.MuzadiMarks.getMeta("lastUser");
  if (!user) return;
  const results = await self.MuzadiMarks.syncReady(user.id);
  if (results.some((r) => r.offline)) throw new Error("still offline");   // the browser retries later
  if (results.length) {
    const clients = await self.clients.matchAll({ includeUncontrolled: true });
    clients.forEach((c) => c.postMessage({ type: "marks-synced", count: results.length }));
  }
}
self.addEventListener("sync", (event) => {
  if (event.tag === SYNC_TAG) event.waitUntil(sendWaitingMarks());
});
{% endautoescape %}
