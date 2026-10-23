/* Northstar Vision Forwarder: queue, duplicate prevention and Discord delivery (service worker).
 *
 * store: async get(keys) / set(obj) (chrome.storage.local in the extension, a Map in tests)
 * fetchFn: fetch;  now: () => ms;  sleep: ms => Promise
 *
 * Rules
 *  - a post whose key was forwarded before (`sent`, last 20,000 keys) or is already queued is skipped:
 *    reloading the page or reconnecting never posts the same thing twice;
 *  - on the very first run (the first 30 s after the first post arrives), posts Vision loaded from its
 *    history (fromHistory) are recorded as seen, not posted, so installing does not dump the backlog;
 *    later, history posts not forwarded yet (missed while Chrome was closed) are sent as catch-up;
 *  - 429: wait the time Discord asks (retry_after / Retry-After) and send again;
 *  - 5xx or a network error: back off 2 s, 4 s, ... up to 60 s and send again, forever;
 *  - 400 (Discord refused the embed): send the same post once more as plain text; if that is refused
 *    too it goes to `failed` (shown in the popup, "Retry failed" requeues it). Nothing is dropped;
 *  - 401 / 403 / 404 (the webhook was deleted or is wrong): pause and show the error. The queue is kept;
 *  - the queue holds up to 5,000 posts. Beyond that the oldest are moved to `failed`, counted, and
 *    shown, never silently discarded.
 */
(function (root) {
  "use strict";
  const MAX_SENT = 20000;
  const MAX_QUEUE = 5000;
  const MAX_FAILED = 500;
  const BASELINE_MS = 30000;
  // operator: "the discord uxento bot cannot keep up, send posts in packages, collect 5 posts in a bundle"
  const BUNDLE_SIZE = 5;          // posts per Discord message (Discord allows 10 embeds)
  const BUNDLE_WAIT_MS = 4000;    // wait at most this long for a bundle to fill
  const BUNDLE_CHARS = 5500;      // Discord's limit is 6,000 characters across all embeds of one message

  function embedChars(e) {
    let n = (e.title || "").length + (e.description || "").length + ((e.footer || {}).text || "").length
      + ((e.author || {}).name || "").length;
    for (const f of e.fields || []) n += (f.name || "").length + (f.value || "").length;
    return n;
  }      // first run: history loaded in this window is recorded, not posted

  class Sender {
    constructor({ store, fetchFn, now, sleep, lib, log }) {
      this.store = store;
      this.fetch = fetchFn;
      this.now = now || (() => Date.now());
      this.sleep = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
      this.L = lib;
      this.log = log || (() => {});
      this.draining = null;
      this.state = null;
    }

    async load() {
      if (this.state) return this.state;
      const s = await this.store.get(["cfg", "sent", "queue", "failed", "stats", "initialized"]);
      this.state = {
        cfg: s.cfg || { enabled: true, webhook: "", routes: {} },
        sent: s.sent || [],
        queue: s.queue || [],
        failed: s.failed || [],
        stats: s.stats || { forwarded: 0, failed: 0, overflowed: 0, lastError: null, lastOkAt: null, pausedUntil: 0,
                            baselined: 0 },
        initialized: s.initialized || 0,         // time of the first post ever received (0 = never)
      };
      this.sentSet = new Set(this.state.sent);
      return this.state;
    }

    async save(keys) {
      const st = this.state;
      const out = {};
      for (const k of keys) out[k] = st[k];
      await this.store.set(out);
    }

    webhookFor(feed) {
      const cfg = this.state.cfg;
      const routes = cfg.routes || {};
      return routes[feed] || cfg.webhook || "";
    }

    async enqueue(posts) {
      const st = await this.load();
      const queued = new Set(st.queue.map((q) => q.post.key));
      let added = 0;
      for (const post of posts) {
        if (!post || typeof post.key !== "string") continue;
        if (this.sentSet.has(post.key) || queued.has(post.key)) continue;
        if (!st.initialized) st.initialized = this.now();
        if (post.fromHistory && this.now() - st.initialized < BASELINE_MS) {
          this.markSent(post.key);                 // first run: the backlog is seen, not posted
          st.stats.baselined += 1;
          continue;
        }
        st.queue.push({ post, tries: 0, next: 0, at: this.now() });
        queued.add(post.key);
        added += 1;
      }
      while (st.queue.length > MAX_QUEUE) {
        const old = st.queue.shift();
        st.failed.push({ post: old.post, why: "queue full (Discord unreachable for a long time)", at: this.now() });
        st.stats.overflowed += 1;
      }
      st.failed = st.failed.slice(-MAX_FAILED);
      await this.save(["queue", "sent", "failed", "stats", "initialized"]);
      return added;
    }

    markSent(key) {
      if (this.sentSet.has(key)) return;
      this.sentSet.add(key);
      this.state.sent.push(key);
      if (this.state.sent.length > MAX_SENT) {
        const drop = this.state.sent.splice(0, this.state.sent.length - MAX_SENT);
        drop.forEach((k) => this.sentSet.delete(k));
      }
    }

    drain() {
      if (!this.draining) {
        this.draining = this._drain().finally(() => { this.draining = null; });
      }
      return this.draining;
    }

    async _drain() {
      const st = await this.load();
      let sentNow = 0;
      while (st.queue.length) {
        if (!st.cfg.enabled) return sentNow;
        const wait = Math.max(0, (st.stats.pausedUntil || 0) - this.now());
        const item = st.queue[0];
        const url = this.webhookFor(item.post.feed);
        if (!url) {
          st.stats.lastError = "no Discord webhook set (open the extension popup)";
          await this.save(["stats"]);
          return sentNow;
        }
        if (wait > 0 || item.next > this.now()) {
          const ms = Math.max(wait, item.next - this.now());
          if (ms > 30000) return sentNow;          // the 1-minute alarm resumes it
          await this.sleep(ms);
        }
        let bundle = this.pickBundle(url);
        const age = this.now() - (item.at || 0);
        if (bundle.length < BUNDLE_SIZE && bundle.length === st.queue.length && !item.single && !item.plain
            && age < BUNDLE_WAIT_MS) {
          await this.sleep(BUNDLE_WAIT_MS - age);     // let a few more posts arrive, then send them together
          bundle = this.pickBundle(url);
        }
        const res = await this.send(url, bundle);
        if (res === "ok" || res === "failed") {
          st.queue.splice(0, bundle.length);
          if (res === "ok") {
            for (const b of bundle) this.markSent(b.post.key);
            st.stats.forwarded += bundle.length;
            st.stats.messages = (st.stats.messages || 0) + 1;
            st.stats.lastOkAt = this.now();
            st.stats.lastError = null;
            sentNow += bundle.length;
          }
        } else if (res === "auth") {
          await this.save(["queue", "sent", "failed", "stats"]);
          return sentNow;
        }
        await this.save(["queue", "sent", "failed", "stats"]);
      }
      return sentNow;
    }

    // the posts at the head of the queue that go out together: same webhook, up to 5, within the size limit
    pickBundle(url) {
      const q = this.state.queue;
      const first = q[0];
      if (!first || first.single || first.plain) return [first];
      const out = [];
      let chars = 0;
      for (const it of q) {
        if (out.length >= BUNDLE_SIZE || it.single || it.plain || this.webhookFor(it.post.feed) !== url) break;
        const n = embedChars(this.L.buildMessage(it.post).embeds[0]);
        if (out.length && chars + n > BUNDLE_CHARS) break;
        out.push(it);
        chars += n;
      }
      return out.length ? out : [first];
    }

    // one delivery attempt for a bundle (1-5 posts): "ok" | "retry" | "failed" | "auth"
    async send(url, items) {
      const st = this.state;
      if (!Array.isArray(items)) items = [items];
      const item = items[0];
      let payload;
      if (items.length > 1) {
        payload = { username: "Uxento Vision", embeds: items.map((i) => this.L.buildMessage(i.post).embeds[0]),
                    allowed_mentions: { parse: [] } };
      } else {
        payload = item.plain ? this.L.plainMessage(item.post) : this.L.buildMessage(item.post);
      }
      let r;
      try {
        r = await this.fetch(url + (url.includes("?") ? "&" : "?") + "wait=true", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
      } catch (e) {
        return this.backoff(item, `network error: ${(e && e.name) || "error"}`);
      }
      const status = r.status;
      if (status >= 200 && status < 300) {
        const remaining = r.headers && r.headers.get && r.headers.get("X-RateLimit-Remaining");
        const reset = r.headers && r.headers.get && Number(r.headers.get("X-RateLimit-Reset-After"));
        if (remaining === "0" && reset > 0) st.stats.pausedUntil = this.now() + reset * 1000;
        return "ok";
      }
      let body = null;
      try { body = await r.json(); } catch (_) { body = null; }
      if (status === 429) {
        const after = Number((body && body.retry_after) || (r.headers && r.headers.get && r.headers.get("Retry-After")) || 1);
        const ms = Math.ceil((after > 1000 ? after : after * 1000)) + 100;
        st.stats.pausedUntil = this.now() + ms;
        st.stats.lastError = `Discord rate limit: waiting ${Math.ceil(ms / 1000)} s`;
        return "retry";
      }
      if (status === 400) {
        if (items.length > 1) {
          for (const i of items) i.single = true;  // one embed in the bundle was refused: send them one by one
          st.stats.lastError = `Discord refused a bundle (${(body && body.message) || 400}); sending those posts one by one`;
          return "retry";
        }
        if (!item.plain) {
          item.plain = true;                       // try again as plain text
          st.stats.lastError = `Discord refused an embed (${(body && body.message) || 400}); resent as text`;
          return "retry";
        }
        st.failed.push({ post: item.post, why: `Discord refused it: ${(body && body.message) || 400}`.slice(0, 200), at: this.now() });
        st.failed = st.failed.slice(-MAX_FAILED);
        st.stats.failed += 1;
        st.stats.lastError = "a post was refused by Discord (see Failed)";
        return "failed";
      }
      if (status === 401 || status === 403 || status === 404) {
        st.stats.lastError = `the Discord webhook was refused (${status}): set a valid webhook URL`;
        st.stats.pausedUntil = this.now() + 60000;
        return "auth";
      }
      return this.backoff(item, `Discord answered ${status}`);
    }

    backoff(item, why) {
      item.tries += 1;
      item.next = this.now() + Math.min(60000, 1000 * 2 ** Math.min(item.tries, 6));
      this.state.stats.lastError = `${why}; retrying (attempt ${item.tries})`;
      return "retry";
    }

    async retryFailed() {
      const st = await this.load();
      const back = st.failed.splice(0).map((f) => ({ post: f.post, tries: 0, next: 0, plain: true }));
      st.queue.unshift(...back);
      await this.save(["queue", "failed"]);
      return back.length;
    }

    async status() {
      const st = await this.load();
      const hook = st.cfg.webhook || "";
      return {
        enabled: st.cfg.enabled, webhookSet: !!hook, webhookHint: hook ? "…" + hook.slice(-4) : null,
        routes: Object.keys(st.cfg.routes || {}), queued: st.queue.length, failed: st.failed.length,
        failedSample: st.failed.slice(-5).map((f) => ({ feed: f.post.feed, title: this.L.title(f.post), why: f.why })),
        remembered: st.sent.length, ...st.stats,
      };
    }

    async configure(cfg) {
      const st = await this.load();
      const ok = (u) => u === "" || /^https:\/\/(?:canary\.|ptb\.)?(?:discord|discordapp)\.com\/api\/webhooks\/\d+\/[\w-]+$/.test(u);
      const webhook = String(cfg.webhook ?? st.cfg.webhook ?? "").trim();
      if (!ok(webhook)) throw new Error("that is not a Discord webhook URL (https://discord.com/api/webhooks/…)");
      const routes = cfg.routes === undefined ? { ...(st.cfg.routes || {}) } : {};
      for (const [feed, u] of Object.entries(cfg.routes || {})) {
        const url = String(u).trim();
        if (!ok(url)) throw new Error(`the webhook for "${feed}" is not a Discord webhook URL`);
        if (url) routes[feed] = url;
      }
      st.cfg = { enabled: cfg.enabled !== undefined ? !!cfg.enabled : st.cfg.enabled, webhook, routes };
      st.stats.pausedUntil = 0;
      st.stats.lastError = null;
      await this.save(["cfg", "stats"]);
    }
  }

  if (typeof module === "object" && module.exports) module.exports = { Sender };
  else root.NSSender = Sender;
})(typeof globalThis !== "undefined" ? globalThis : this);
