/* MailArchiver — уведомления о новой почте (Web Push, 1.22.0).
 * Браузер будит этот обработчик, когда архив присылает уведомление, даже если «Почта» закрыта.
 * Содержимое (отправитель, тема, ссылка на письмо) зашифровано ключом этого браузера. */
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function safeUrl(u){
  // только адрес этого же сайта: «/#/mail…»
  return (typeof u === 'string' && u.startsWith('/') && !u.startsWith('//')) ? u : '/#/mail';
}

self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = {}; }
  const title = String(d.title || 'Новое письмо').slice(0, 200);
  const options = {
    body: String(d.body || '').slice(0, 500),
    tag: String(d.tag || 'ma-mail').slice(0, 100),
    renotify: true,
    icon: '/static/img/icon-192.png',
    badge: '/static/img/icon-192.png',
    data: { url: safeUrl(d.url) },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const path = safeUrl(event.notification.data && event.notification.data.url);
  const url = new URL(path, self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin !== self.location.origin) continue;
      try { await w.focus(); } catch (e) { /* окно уже в фокусе или браузер не дал */ }
      w.postMessage({ type: 'ma-open', url: path });
      return;
    }
    await self.clients.openWindow(url);
  })());
});

// подписку сменил сам браузер (истекла) — интерфейс при следующем открытии подпишется заново
self.addEventListener('pushsubscriptionchange', () => {});
