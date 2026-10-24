// node --test tools/vision-forwarder/test/
// Synthetic Vision items shaped like the ones the Vision page holds (field names checked on the live
// page; every value here is made up).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../lib.js");
const { Sender } = require("../sender.js");

const MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU";   // a valid 32-byte base58 key

const tweet = {
  id: "1849000000000000001", type: "TWEET", created_at: 1792771687000, __source: "CT_Tracker5", __insertionId: 5,
  author: { handle: "alice", verified: true, profile: { name: "Alice", avatar: "https://pbs.twimg.com/a.jpg" },
            metrics: { followers: 59800, following: 10 } },
  body: { text: `new coin ${MINT} $DOGWIF @everyone`, mentions: [], urls: [{ name: "site", url: "https://example.com/x", tco: "https://t.co/1" }] },
  media: { images: ["https://pbs.twimg.com/media/1.jpg"] },
};
const reply = { ...tweet, id: "1849000000000000002", type: "REPLY", body: { text: "this", mentions: [], urls: [] }, media: undefined,
  subtweet: { id: "1849000000000000000", type: "TWEET", author: { handle: "bob" }, body: { text: "original post" } } };
const deleted = { type: "tweet.delete", id: "d1", deleted_at: 1792771690000, __source: "TweetDeleted",
  tweet: { id: "1849", text: "gone soon", author: { handle: "carol" } }, user: { handle: "carol", profile: { name: "Carol" } } };
const unfollow = { type: "user.unfollow", id: "u1", action: "unfollow", created_at: 1792771690000, __source: "FollowUpdate",
  user: { handle: "dave", profile: { name: "Dave" } }, following: { handle: "erin", profile: { name: "Erin" } } };
// Fomo trade: field names as the live Vision page holds them (values made up)
const callout = { type: "fomo_trade", id: "2288a518-0000", side: "buy", createdAt: 1792771690000, publishedAt: 1792771690500,
  caller: { platform: "fomo", id: "c1", username: "trader1", avatar: "https://img/a.png" },
  coin: { mint: MINT, symbol: "DOGWIF", image: "https://ipfs.io/x.png", marketCap: 43000 },
  tokenAmount: 27674766.1, quoteAmount: 500, quoteMint: "USD", isFirstBuy: true, networkId: 1, isSolana: true, tradeId: "t1" };
// PumpCallouts websocket shape (seen on the Ably channel): amount in SOL
const pumpCallout = { type: "pumpfun", id: "abc", side: "buy", venue: "pump", solAmount: 2.5, createdAt: 1792771690000,
  caller: { platform: "x", id: "c2", username: "trader2" }, coin: { mint: MINT, name: "Dog Wif", symbol: "DOGWIF", marketCap: 43000 } };

test("tweets keep text, author, links, images and the contract address", () => {
  const p = L.normalize(tweet, "X Tracker");
  assert.equal(p.key, "X Tracker:TWEET:1849000000000000001");
  assert.deepEqual(p.mints, [MINT]);
  assert.equal(p.author.handle, "alice");
  assert.deepEqual(p.urls, ["https://example.com/x"]);
  const m = L.buildMessage(p);
  const e = m.embeds[0];
  assert.deepEqual(m.allowed_mentions, { parse: [] }, "a tweet saying @everyone must not ping");
  assert.equal(e.url, "https://x.com/alice/status/1849000000000000001");
  assert.equal(e.author.url, "https://x.com/alice");
  assert.equal(e.image.url, "https://pbs.twimg.com/media/1.jpg");
  assert.ok(e.fields.some((f) => f.name === "CA" && f.value.includes(MINT)));
  assert.ok(e.description.includes("$DOGWIF"));
  assert.equal(e.timestamp, new Date(1792771687000).toISOString());
});

test("replies, deletes, unfollows and Fomo trades all become posts", () => {
  const r = L.buildMessage(L.normalize(reply, "X Tracker")).embeds[0];
  assert.match(r.title, /Reply to @bob/);
  assert.match(r.description, /original post/);
  const d = L.buildMessage(L.normalize(deleted, "X Tracker")).embeds[0];
  assert.match(d.title, /Deleted/);
  assert.match(d.description, /gone soon/);
  assert.equal(d.author.url, "https://x.com/carol");
  const u = L.buildMessage(L.normalize(unfollow, "X Tracker")).embeds[0];
  assert.match(u.title, /Unfollowed @erin/);
  const c = L.normalize(callout, "Fomo Trades");
  assert.deepEqual(c.mints, [MINT]);
  const ce = L.buildMessage(c).embeds[0];
  assert.match(ce.title, /BUY \$DOGWIF/);
  assert.ok(ce.fields.some((f) => f.name === "Trade" && f.value === "BUY $500 · first buy · fomo"));
  assert.equal(ce.author.url, undefined, "a Fomo caller is not an X account");
  const pe = L.buildMessage(L.normalize(pumpCallout, "Fomo Trades")).embeds[0];
  assert.ok(pe.fields.some((f) => f.name === "Trade" && /BUY 2\.5 SOL on pump/.test(f.value)));
  assert.equal(pe.author.url, "https://x.com/trader2");
});

test("Fomo Thesis posts keep the caller's text and the coin", () => {
  // field names as the live Vision page holds them (values made up)
  const th = { type: "fomo_thesis", id: "th-1", text: "sending this, dev is based", createdAt: 1792771690000,
    caller: { platform: "fomo", id: "c3", username: "caller3" }, coin: { mint: MINT, symbol: "DOGWIF", name: "Dog Wif" },
    networkId: 1, isSolana: true, tradeId: "t3" };
  const p = L.normalize(th, "Fomo Thesis");
  const e = L.buildMessage(p).embeds[0];
  assert.equal(e.title, "💡 Thesis $DOGWIF");
  assert.match(e.description, /dev is based/);
  assert.deepEqual(p.mints, [MINT]);
});

test("an unfamiliar post type is still forwarded with its own text", () => {
  const p = L.normalize({ id: "z9", kind: "new_widget", headline: "Something new", score: 7 }, "Future Feed");
  assert.ok(p.generic);
  assert.match(p.text, /headline: Something new/);
  assert.match(L.buildMessage(p).embeds[0].footer.text, /unrecognised post type/);
});

test("screen fallback cards get a stable key", () => {
  const a = L.normalize({ __dom: true, text: `hello ${MINT}`, links: [], images: [] }, "X Tracker");
  const b = L.normalize({ __dom: true, text: `hello ${MINT}`, links: [], images: [] }, "X Tracker");
  assert.equal(a.key, b.key);
  assert.deepEqual(a.mints, [MINT]);
});

test("embeds stay inside Discord's limits", () => {
  const p = L.normalize({ ...tweet, body: { text: "x".repeat(9000), urls: [] } }, "X Tracker");
  const e = L.buildMessage(p).embeds[0];
  assert.ok(e.description.length <= 4096 && e.title.length <= 256 && e.author.name.length <= 256);
  assert.ok(JSON.stringify(e).length < 6000);
});

test("findMints ignores tweet ids and non-32-byte strings", () => {
  assert.deepEqual(L.findMints("1849000000000000001 abcdefghijkmnopqrstuvwxyzABCDEFGH", MINT), [MINT]);
});

// ------------------------------------------------------------------ sender
function harness(responses) {
  const data = {};
  let clock = 1_000_000;
  const calls = [];
  const store = { get: async (keys) => Object.fromEntries(keys.filter((k) => k in data).map((k) => [k, structuredClone(data[k])])),
                  set: async (o) => { Object.assign(data, structuredClone(o)); } };
  const fetchFn = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    const r = responses.length ? responses.shift() : { status: 204 };
    if (r.throw) throw new TypeError("Failed to fetch");
    return { status: r.status, headers: { get: (h) => (r.headers || {})[h] ?? null }, json: async () => r.body || {} };
  };
  const s = new Sender({ store, fetchFn, lib: L, now: () => clock, sleep: async (ms) => { clock += ms; } });
  return { s, data, calls, tick: (ms) => { clock += ms; } };
}
const HOOK = "https://discord.com/api/webhooks/123/abc-DEF";
const post = (n, extra = {}) => ({ ...L.normalize({ ...tweet, id: "18490000000000001" + String(n).padStart(2, "0") }, "X Tracker"), ...extra });

test("forwards once: reconnects and page reloads never post twice", async () => {
  const h = harness([]);
  await h.s.configure({ webhook: HOOK });
  h.tick(60000);
  await h.s.enqueue([post(1), post(2)]);
  await h.s.drain();
  await h.s.enqueue([post(1), post(2), post(3)]);          // the page was reloaded
  await h.s.drain();
  assert.equal(h.calls.length, 2, "posts 1+2 in one bundle, then post 3 alone");
  assert.equal(h.calls[0].body.embeds.length, 2);
  assert.ok(h.calls[0].url.endsWith("?wait=true"));
  const fresh = new Sender({ store: h.s.store, fetchFn: h.s.fetch, lib: L, now: h.s.now, sleep: h.s.sleep });   // worker restarted
  await fresh.enqueue([post(1), post(3)]);
  await fresh.drain();
  assert.equal(h.calls.length, 2);
  assert.equal((await fresh.status()).forwarded, 3);
});

test("first run: the history Vision loaded is recorded, not posted; later catch-up is posted", async () => {
  const h = harness([]);
  await h.s.configure({ webhook: HOOK });
  await h.s.enqueue([post(1, { fromHistory: true }), post(2, { fromHistory: true }), post(3)]);
  await h.s.drain();
  assert.equal(h.calls.length, 1);
  h.tick(40000);
  await h.s.enqueue([post(4, { fromHistory: true })]);   // missed while Chrome was closed
  await h.s.drain();
  assert.equal(h.calls.length, 2);
  assert.match(h.calls[1].body.embeds[0].footer.text, /catch-up/);
});

test("429 waits the time Discord asks, then sends", async () => {
  const h = harness([{ status: 429, body: { retry_after: 1.5 } }, { status: 204 }]);
  await h.s.configure({ webhook: HOOK });
  h.tick(60000);
  await h.s.enqueue([post(1)]);
  await h.s.drain();
  assert.equal(h.calls.length, 2);
  assert.equal((await h.s.status()).forwarded, 1);
});

test("a refused embed is resent as text, and kept as failed if that is refused too", async () => {
  // bundle refused -> one by one: post 1 embed refused, post 1 as text OK; post 2 embed and text refused
  const h = harness([{ status: 400, body: { message: "Invalid Form Body" } }, { status: 400 }, { status: 204 },
                     { status: 400 }, { status: 400 }]);
  await h.s.configure({ webhook: HOOK });
  h.tick(60000);
  await h.s.enqueue([post(1), post(2)]);
  await h.s.drain();
  assert.equal(h.calls[0].body.embeds.length, 2, "first try is the bundle");
  assert.equal(h.calls[1].body.embeds.length, 1, "then one by one");
  assert.ok(h.calls[2].body.content && !h.calls[2].body.embeds, "then as plain text");
  const st = await h.s.status();
  assert.equal(st.forwarded, 1);
  assert.equal(st.failed, 1);
  assert.equal(await h.s.retryFailed(), 1);
  await h.s.drain();
  assert.equal((await h.s.status()).forwarded, 2);
});

test("bundles: up to 5 posts per Discord message, waiting briefly for a bundle to fill", async () => {
  const h = harness([]);
  await h.s.configure({ webhook: HOOK });
  h.tick(60000);
  await h.s.enqueue([1, 2, 3, 4, 5, 6, 7].map((n) => post(n)));
  await h.s.drain();
  assert.deepEqual(h.calls.map((c) => c.body.embeds.length), [5, 2]);
  assert.deepEqual(h.calls[0].body.allowed_mentions, { parse: [] });
  const st = await h.s.status();
  assert.equal(st.forwarded, 7);
  assert.equal(st.messages, 2);
  // a lone post waits up to 4 s for company, then goes alone
  const t0 = h.s.now();
  await h.s.enqueue([post(8)]);
  await h.s.drain();
  assert.equal(h.calls.length, 3);
  assert.ok(h.s.now() - t0 >= 4000 && h.s.now() - t0 < 5000);
});

test("bundles stay under Discord's 6,000-character limit and never mix webhooks", async () => {
  const h = harness([]);
  await h.s.configure({ webhook: HOOK, routes: { "Fomo Trades": "https://discord.com/api/webhooks/9/zz" } });
  h.tick(60000);
  const big = (n) => ({ ...L.normalize({ ...tweet, id: "18490000000000002" + String(n).padStart(2, "0"),
                                         body: { text: "y".repeat(1400), urls: [] } }, "X Tracker") });
  await h.s.enqueue([big(1), big(2), big(3), big(4), big(5), L.normalize(callout, "Fomo Trades")]);
  await h.s.drain();
  for (const c of h.calls) {
    const chars = c.body.embeds.reduce((s, e) => s + (e.description || "").length + (e.title || "").length
      + ((e.footer || {}).text || "").length + ((e.author || {}).name || "").length
      + (e.fields || []).reduce((a, f) => a + f.name.length + f.value.length, 0), 0);
    assert.ok(chars <= 6000, `bundle of ${chars} characters`);
  }
  assert.ok(h.calls.length >= 3);
  const last = h.calls[h.calls.length - 1];
  assert.ok(last.url.startsWith("https://discord.com/api/webhooks/9/zz") && last.body.embeds.length === 1);
  assert.equal((await h.s.status()).forwarded, 6);
});

test("network errors and 5xx back off and retry; a bad webhook pauses but keeps the queue", async () => {
  const h = harness([{ throw: true }, { status: 502 }, { status: 204 }, { status: 404 }]);
  await h.s.configure({ webhook: HOOK });
  h.tick(60000);
  await h.s.enqueue([post(1)]);
  await h.s.drain();
  assert.equal(h.calls.length, 3);
  await h.s.enqueue([post(2)]);
  await h.s.drain();
  const st = await h.s.status();
  assert.equal(st.queued, 1);
  assert.match(st.lastError, /webhook was refused \(404\)/);
});

test("no webhook: posts wait in the queue", async () => {
  const h = harness([]);
  await h.s.enqueue([post(1)]);
  await h.s.drain();
  assert.equal(h.calls.length, 0);
  assert.equal((await h.s.status()).queued, 1);
  await assert.rejects(h.s.configure({ webhook: "https://evil.example/hook" }));
});

test("per-feed webhooks route each panel", async () => {
  const h = harness([]);
  await h.s.configure({ webhook: HOOK, routes: { "Fomo Trades": "https://discord.com/api/webhooks/9/zz" } });
  h.tick(60000);
  await h.s.enqueue([L.normalize(callout, "Fomo Trades"), post(1)]);
  await h.s.drain();
  assert.ok(h.calls[0].url.startsWith("https://discord.com/api/webhooks/9/zz"));
  assert.ok(h.calls[1].url.startsWith(HOOK));
  await h.s.configure({ enabled: true });                 // saving without routes keeps them
  assert.deepEqual((await h.s.status()).routes, ["Fomo Trades"]);
});
