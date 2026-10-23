/* Northstar Vision Forwarder: relays posts from the page script to the service worker (isolated world).
 * Only "posts" and "heartbeat" messages are passed on; the page cannot choose where anything is sent. */
(() => {
  "use strict";
  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const d = e.data;
    if (!d || d.source !== "ns-vision") return;
    let msg = null;
    if (d.kind === "posts" && Array.isArray(d.posts)) {
      msg = { kind: "posts", posts: d.posts.filter((p) => p && typeof p === "object" && typeof p.key === "string").slice(0, 50) };
    } else if (d.kind === "heartbeat" && d.info && typeof d.info === "object") {
      msg = { kind: "heartbeat", info: d.info };
    }
    if (!msg) return;
    try {
      chrome.runtime.sendMessage(msg).catch(() => {});
    } catch (_) {
      // the extension was reloaded: this old page script stops; reload the Vision tab to reconnect
    }
  });
})();
