// Cache name is bumped whenever the cache strategy or asset list changes so
// previously-installed workers drop their stale entries on activate.
const CACHE = 'telfiles-v3';
const STATIC = ['/manifest.json', '/icon-192.png', '/icon-512.png'];
// Frequently-changing assets — always go to network first, only fall back to
// cache when offline. Prevents the "I refreshed and nothing updated" trap.
const NETWORK_FIRST = new Set(['/', '/index.html', '/app.js', '/i18n.js', '/sw.js']);

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Only intercept GET requests for same-origin static assets
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (NETWORK_FIRST.has(url.pathname)) {
    // Network-first: prefer the live copy, fall back to cache only on failure.
    // cache:'no-cache' forces conditional revalidation against the server so
    // the browser HTTP cache can never hand us a stale copy "successfully".
    e.respondWith(
      fetch(e.request, { cache: 'no-cache' }).then(res => {
        if (res.ok) {
          caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        }
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache-first for the truly-static stuff (icons, manifest).
  e.respondWith(
    caches.match(e.request).then(cached => {
      const network = fetch(e.request).then(res => {
        if (res.ok && STATIC.includes(url.pathname)) {
          caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        }
        return res;
      });
      return cached || network;
    })
  );
});
