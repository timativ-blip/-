const CACHE = 'exit-poll-v8-offline';
const FILES = [
  '/',
  '/static/app.js',
  '/static/style.css',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/survey-illustration.svg',
  '/static/manifest.webmanifest'
];

self.addEventListener('install', event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll(FILES)).then(() => self.skipWaiting())
));

self.addEventListener('activate', event => event.waitUntil(
  caches.keys()
    .then(keys => Promise.all(keys.filter(key => key.startsWith('exit-poll-') && key !== CACHE).map(key => caches.delete(key))))
    .then(() => self.clients.claim())
));

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  // The coordinator dashboard contains live confidential data and is never
  // cached or used as the offline fallback for the interviewer application.
  if (event.request.mode === 'navigate' && url.pathname === '/dashboard') {
    event.respondWith(fetch(event.request));
    return;
  }

  if (event.request.mode === 'navigate') {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 4000);
      try {
        const response = await fetch(event.request, {signal:controller.signal});
        if (response.ok) await cache.put('/', response.clone());
        return response;
      } catch {
        return (await cache.match('/')) || Response.error();
      } finally {
        clearTimeout(timer);
      }
    })());
    return;
  }

  if (FILES.includes(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(url.pathname);
      const fresh = fetch(event.request).then(response => {
        if (response.ok) void cache.put(url.pathname, response.clone());
        return response;
      }).catch(() => null);
      return cached || fresh || Response.error();
    })());
  }
});

self.addEventListener('message', event => {
  if (event.data?.type !== 'CHECK_OFFLINE_READY') return;
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    const results = await Promise.all(FILES.map(path => cache.match(path)));
    const missing = FILES.filter((_, index) => !results[index]);
    event.ports[0]?.postMessage({type:'OFFLINE_READY', ready:missing.length === 0, version:CACHE, missing});
  })());
});
