/* Phantom Companion — service worker (PWA).
 * Offline shell caching: the app UI loads instantly even when the phone can't
 * reach the PC (you see the last briefing/status cached in the UI). API calls
 * are network-only (they need the live PC); failures surface honestly.
 *
 * Note: service workers only run in a secure context (https or localhost).
 * On a LAN (http://192.168.x.x) the app still works fully as a web app; to
 * install it as a PWA on iPhone you need https (e.g. a Cloudflare Tunnel).
 */
"use strict";

const CACHE = "phantom-companion-v1";
const SHELL = [
  "/mobile",
  "/mobile.css",
  "/mobile.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (ev) => {
  ev.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  // API / ws / auth: network only
  if (url.pathname.startsWith("/api/") || url.pathname === "/ws") return;
  // shell: cache-first, fall back to network
  if (ev.request.method === "GET") {
    ev.respondWith(
      caches.match(ev.request).then((hit) => hit || fetch(ev.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(ev.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match("/mobile")))
    );
  }
});
