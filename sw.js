/* Service worker du CCQF · Cyclo-Club Quint-Fonsegrives, version 1.5.2 */
const CACHE = "ccqf-1.5.2";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./data/sorties.json",
  "./icons/icon-192.png", "./icons/icon-512.png", "./icons/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;
  // Toujours revalider auprès du serveur (GitHub Pages autorise 10 minutes de cache) :
  // une nouvelle version du site s'affiche dès sa publication
  const reseau = request.mode === "navigate" ? fetch(request.url, { cache: "no-cache" }) : fetch(request, { cache: "no-cache" });
  event.respondWith(
    reseau
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("./index.html")))
  );
});
