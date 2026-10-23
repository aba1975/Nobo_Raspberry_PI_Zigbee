/*
 * Cabin's service worker. It does one thing: show the alerts the Pi sends.
 *
 * There is deliberately no fetch handler. A worker that caches pages is how a
 * deploy ends up serving new HTML with old JavaScript, and this app has been
 * bitten by exactly that before. Every request still goes to the network, as
 * if there were no worker at all.
 *
 * Every push must show a notification. Safari withdraws the subscription of a
 * site that receives pushes and shows nothing, so even a message that cannot
 * be read becomes a notification rather than silence.
 *
 * docs/PUSH.md is the design.
 */
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function readMessage(event) {
  try {
    const data = event.data ? event.data.json() : null;
    if (data && typeof data.title === 'string') return data;
  } catch (_) { /* not JSON: fall through to the generic alert */ }
  return { title: 'Alert', body: 'Open Cabin to see what happened.', url: '/' };
}

function sameOriginPath(url) {
  try {
    const target = new URL(url || '/', self.location.origin);
    if (target.origin === self.location.origin) return target.pathname + target.search + target.hash;
  } catch (_) { /* fall back to the start page */ }
  return '/';
}

self.addEventListener('push', (event) => {
  const msg = readMessage(event);
  const options = {
    body: msg.body || '',
    icon: '/static/ui/cabin/icon-192.png',
    badge: '/static/ui/cabin/icon-192.png',
    data: { url: sameOriginPath(msg.url) },
    requireInteraction: msg.severity === 'critical',
  };
  // The same tag replaces the earlier alert about the same thing, so "closed
  // again" takes the place of "left open" instead of stacking beneath it.
  if (msg.tag) options.tag = msg.tag;
  event.waitUntil(self.registration.showNotification(msg.title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const path = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin === self.location.origin && 'focus' in client) {
        await client.focus();
        if ('navigate' in client && new URL(client.url).pathname !== path) {
          try { await client.navigate(path); } catch (_) { /* focused is enough */ }
        }
        return;
      }
    }
    await self.clients.openWindow(path);
  })());
});
