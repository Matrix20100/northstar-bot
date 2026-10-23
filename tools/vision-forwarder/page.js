/* Northstar Vision Forwarder: runs in the Uxento Vision page (MAIN world).
 *
 * Every Vision panel is a react-virtuoso list. The list component is handed the panel's whole post
 * list (`data`), already decrypted and filtered by Uxento for display. That list is read on every
 * change of the page (MutationObserver) and every 2 s, new items are normalized (lib.js) and handed
 * to the extension (bridge.js -> service worker), which forwards them to Discord.
 *
 * Only what the Vision page itself shows is read. Nothing is sent to Uxento, nothing is changed on
 * the page, and no cookie, token or key is touched.
 */
(() => {
  "use strict";
  if (window.__nsVisionForwarderLoaded) return;
  window.__nsVisionForwarderLoaded = true;
  const L = window.__NSVisionLib;
  if (!L) return;

  const seen = new Set();          // keys already handed over during this page session
  const MAX_SEEN = 20000;
  let lastHeartbeat = 0;
  let scheduled = false;

  const onVision = () => location.pathname.startsWith("/vision");

  function feedName(scroller) {
    let e = scroller;
    for (let i = 0; i < 14 && e; i++) {
      e = e.parentElement;
      const h = e && e.querySelector("h3");
      if (h && h.textContent.trim()) return h.textContent.trim();
    }
    return "Vision";
  }

  function listData(scroller) {
    const fk = Object.keys(scroller).find((k) => k.startsWith("__reactFiber"));
    let f = fk ? scroller[fk] : null;
    for (let i = 0; i < 40 && f; i++, f = f.return) {
      const p = f.memoizedProps;
      if (p && Array.isArray(p.data)) return p.data;
    }
    return null;
  }

  function domItems(scroller) {
    return [...scroller.querySelectorAll("[data-index]")].map((el) => ({
      __dom: true,
      text: (el.innerText || "").trim(),
      links: [...el.querySelectorAll("a[href]")].map((a) => a.href),
      images: [...el.querySelectorAll("img")].map((i) => i.currentSrc || i.src),
    }));
  }

  function panelTitles() {
    return [...document.querySelectorAll("main h3")].map((h) => h.textContent.trim()).filter(Boolean);
  }

  function scan() {
    scheduled = false;
    if (!onVision()) return;
    const panels = [];
    const fresh = [];
    for (const sc of document.querySelectorAll("[data-virtuoso-scroller]")) {
      const feed = feedName(sc);
      let items = listData(sc);
      let mode = "data";
      if (!items) {
        items = domItems(sc);
        mode = "screen";
      }
      panels.push({ feed, count: items.length, mode });
      // newest first in the list: walk oldest -> newest so Discord gets them in order
      for (let i = items.length - 1; i >= 0; i--) {
        let post;
        try {
          post = L.normalize(items[i], feed);
        } catch (e) {
          post = L.normalize({ __dom: true, text: `(could not read a ${feed} post: ${e && e.message})` }, feed);
        }
        if (!post || seen.has(post.key)) continue;
        seen.add(post.key);
        fresh.push(post);
      }
    }
    if (seen.size > MAX_SEEN) {
      const keep = [...seen].slice(-MAX_SEEN / 2);
      seen.clear();
      keep.forEach((k) => seen.add(k));
    }
    if (fresh.length) {
      for (let i = 0; i < fresh.length; i += 25) {
        window.postMessage({ source: "ns-vision", kind: "posts", posts: fresh.slice(i, i + 25) }, location.origin);
      }
    }
    const now = Date.now();
    if (fresh.length || now - lastHeartbeat > 10000) {
      lastHeartbeat = now;
      const listed = new Set(panels.map((p) => p.feed));
      for (const t of panelTitles()) if (!listed.has(t)) panels.push({ feed: t, count: 0, mode: "empty" });
      window.postMessage({ source: "ns-vision", kind: "heartbeat",
                           info: { at: now, panels, visible: document.visibilityState === "visible" } },
                         location.origin);
    }
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    setTimeout(scan, 250);
  }

  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(scan, 2000);
  schedule();
})();
