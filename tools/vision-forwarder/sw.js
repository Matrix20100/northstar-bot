/* Northstar Vision Forwarder: service worker. Receives posts from the Vision tab and forwards them. */
importScripts("lib.js", "sender.js");

const store = {
  get: (keys) => chrome.storage.local.get(keys),
  set: (obj) => chrome.storage.local.set(obj),
};
const sender = new NSSender({ store, fetchFn: (...a) => fetch(...a), lib: self.__NSVisionLib });
let page = { at: 0, panels: [], visible: null };

async function badge() {
  try {
    const s = await sender.status();
    const stale = Date.now() - page.at > 60000;
    let text = "", color = "#16a34a";
    if (!s.webhookSet) { text = "SET"; color = "#ca8a04"; }
    else if (s.lastError && /webhook|refused/.test(s.lastError)) { text = "ERR"; color = "#dc2626"; }
    else if (s.queued) { text = String(Math.min(s.queued, 999)); color = "#2563eb"; }
    else if (stale) { text = "OFF"; color = "#6b7280"; }
    await chrome.action.setBadgeText({ text });
    await chrome.action.setBadgeBackgroundColor({ color });
  } catch (_) { /* badge is cosmetic */ }
}

chrome.runtime.onMessage.addListener((msg, sender_, reply) => {
  (async () => {
    if (msg.kind === "posts") {
      const added = await sender.enqueue(msg.posts || []);
      if (added) sender.drain().then(badge);
      reply({ ok: true, added });
    } else if (msg.kind === "heartbeat") {
      page = { ...msg.info, at: Date.now() };
      await chrome.storage.local.set({ page });
      reply({ ok: true });
    } else if (msg.kind === "status") {
      const stored = (await chrome.storage.local.get(["page"])).page || page;
      reply({ ok: true, status: await sender.status(), page: stored });
    } else if (msg.kind === "configure") {
      try {
        await sender.configure(msg.cfg || {});
        sender.drain().then(badge);
        reply({ ok: true });
      } catch (e) {
        reply({ ok: false, error: e.message });
      }
    } else if (msg.kind === "retryFailed") {
      reply({ ok: true, n: await sender.retryFailed() });
      sender.drain().then(badge);
    } else if (msg.kind === "test") {
      const post = { key: `test:${Date.now()}`, feed: "Test", type: "TWEET", id: null, source: "test",
        created_at: Date.now(), fromHistory: false, author: { handle: "northstar", name: "Northstar", avatar: null,
        followers: null, verified: false }, text: "Test post from the Northstar Vision Forwarder. If you can read this, forwarding works.",
        urls: [], images: [], link_preview: null, quoted: null, retweeted: null, reply: null, subtweet: null,
        deleted: null, following: null, action: null, coin: null, side: null, venue: null, solAmount: null,
        platform: null, generic: false, mints: [] };
      await sender.enqueue([post]);
      await sender.drain();
      reply({ ok: true, status: await sender.status() });
    }
    badge();
  })();
  return true;
});

chrome.alarms.create("drain", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "drain") sender.drain().then(badge);
});
chrome.runtime.onStartup.addListener(() => sender.drain().then(badge));
chrome.runtime.onInstalled.addListener(() => badge());
