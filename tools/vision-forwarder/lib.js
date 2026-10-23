/* Northstar Vision Forwarder: shared logic (page script, service worker and node tests).
 *
 * normalize(item, feed)  one Vision list item -> a plain post object (never null for an object input)
 * buildMessage(post)     post -> Discord webhook payload (one compact embed, no mentions)
 * findMints(...texts)    Solana mint addresses (base58, 32 bytes) in free text
 *
 * Nothing here talks to the network.
 */
(function (root) {
  "use strict";

  const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const MINT_RE = /(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])/g;

  function b58Bytes(s) {
    const bytes = [];
    for (const ch of s) {
      let carry = B58.indexOf(ch);
      if (carry < 0) return -1;
      for (let i = 0; i < bytes.length; i++) {
        carry += bytes[i] * 58;
        bytes[i] = carry & 0xff;
        carry >>= 8;
      }
      while (carry) {
        bytes.push(carry & 0xff);
        carry >>= 8;
      }
    }
    let lead = 0;
    while (lead < s.length && s[lead] === "1") lead++;
    return lead + bytes.length;
  }

  function findMints(...texts) {
    const out = [];
    for (const t of texts) {
      if (!t) continue;
      for (const m of String(t).match(MINT_RE) || []) {
        if (!out.includes(m) && b58Bytes(m) === 32) out.push(m);
      }
    }
    return out.slice(0, 5);
  }

  function str(x) {
    if (typeof x === "string") return x;
    if (x === null || x === undefined) return "";
    if (typeof x === "number" || typeof x === "boolean") return String(x);
    return "";
  }

  function num(x) {
    const n = typeof x === "string" ? Number(x) : x;
    return typeof n === "number" && isFinite(n) ? n : null;
  }

  function hash(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h.toString(16).padStart(8, "0");
  }

  function person(a) {
    if (!a || typeof a !== "object") return null;
    const p = a.profile && typeof a.profile === "object" ? a.profile : {};
    const handle = str(a.handle || a.username || a.screen_name) || null;
    const name = str(p.name || a.name || a.displayName) || null;
    if (!handle && !name) return null;
    return {
      handle,
      name,
      avatar: str(p.avatar || a.avatar || a.image) || null,
      followers: num((a.metrics || {}).followers ?? a.followers),
      verified: !!a.verified,
    };
  }

  function tweetBits(t) {
    if (!t || typeof t !== "object") return null;
    const handle = str((t.author && t.author.handle) || t.handle || (t.user && t.user.handle)) || null;
    const text = str((t.body && t.body.text) || t.text).slice(0, 600) || null;
    const id = t.id != null ? str(t.id) : null;
    if (!handle && !text && !id) return null;
    return { id, handle, text };
  }

  // a readable dump of an unknown item's own text fields, so an unfamiliar post is still forwarded
  function genericText(item) {
    const parts = [];
    const walk = (v, path, depth) => {
      if (parts.join(" ").length > 900 || depth > 3) return;
      if (typeof v === "string") {
        if (v.length >= 2 && !/^https?:\/\/\S+\.(png|jpe?g|gif|webp)/i.test(v)) parts.push(`${path}: ${v.slice(0, 300)}`);
      } else if (typeof v === "number" && !/(^|\.)(__|ts$|slot$)/.test(path)) {
        parts.push(`${path}: ${v}`);
      } else if (v && typeof v === "object") {
        for (const k of Object.keys(v).slice(0, 25)) {
          if (k.startsWith("__")) continue;
          walk(v[k], path ? `${path}.${k}` : k, depth + 1);
        }
      }
    };
    walk(item, "", 0);
    return parts.join("\n").slice(0, 1200);
  }

  function normalize(item, feed) {
    if (!item || typeof item !== "object") return null;
    feed = str(feed) || "Vision";
    if (item.__dom) {
      // fallback: a card read from the page when the list data could not be reached
      const text = str(item.text).slice(0, 1500);
      const post = {
        feed, type: "card", id: null, source: "dom", created_at: Date.now(), fromHistory: false, author: null,
        text, urls: (item.links || []).filter((u) => /^https?:/.test(u)).slice(0, 10),
        images: (item.images || []).filter((u) => /^https:/.test(u)).slice(0, 2), link_preview: null,
        quoted: null, retweeted: null, reply: null, subtweet: null, deleted: null, following: null, action: null,
        coin: null, side: null, venue: null, solAmount: null, amount: null, amountUnit: null, firstBuy: false,
        platform: null, generic: false,
      };
      post.mints = findMints(text, post.urls.join(" "));
      post.key = `${feed}:card:${hash(text)}`;
      return post;
    }
    const body = item.body && typeof item.body === "object" ? item.body : {};
    const coin = item.coin && typeof item.coin === "object" ? item.coin : null;
    const subtweet = item.subtweet || (item.context && item.context.subtweet);
    const post = {
      feed,
      type: str(item.type || item.name || item.kind) || "post",
      id: item.id != null ? str(item.id) : null,
      source: str(item.__source) || null,
      created_at: num(item.created_at ?? item.createdAt ?? item.publishedAt ?? item.deleted_at ?? item.__ts),
      fromHistory: !!item.__fromHistory,
      author: person(item.author || item.user || item.caller),
      text: str(body.text || item.text || item.thesis || item.message || item.content || item.description).slice(0, 1500),
      urls: (Array.isArray(body.urls) ? body.urls : [])
        .map((u) => (u && typeof u === "object" ? str(u.url || u.name) : str(u)))
        .filter((u) => /^https?:/.test(u)).slice(0, 10),
      images: ((item.media && Array.isArray(item.media.images) && item.media.images) || [])
        .filter((u) => typeof u === "string" && /^https:/.test(u)).slice(0, 4),
      link_preview: item.link_preview && typeof item.link_preview === "object"
        ? { title: str(item.link_preview.title).slice(0, 200) || null,
            description: str(item.link_preview.description).slice(0, 300) || null,
            image: /^https:/.test(str(item.link_preview.image)) ? item.link_preview.image : null }
        : null,
      quoted: tweetBits(item.quoted),
      retweeted: tweetBits(item.retweeted),
      reply: tweetBits(item.reply),
      subtweet: tweetBits(subtweet),
      deleted: tweetBits(item.tweet),
      following: person(item.following),
      action: str(item.action) || null,
      coin: coin ? { mint: str(coin.mint) || null, symbol: str(coin.symbol) || null, name: str(coin.name) || null,
                     image: /^https:/.test(str(coin.image)) ? coin.image : null, marketCap: num(coin.marketCap) } : null,
      side: str(item.side) || null,
      venue: str(item.venue) || null,
      solAmount: num(item.solAmount),
      // Fomo trades (checked live): quoteAmount in quoteMint units ("USD" on Fomo), isFirstBuy
      amount: num(item.quoteAmount),
      amountUnit: str(item.quoteMint) || null,
      firstBuy: item.isFirstBuy === true,
      platform: str(item.caller && item.caller.platform) || null,
      generic: false,
    };
    if (!post.text && !post.author && !post.coin && !post.deleted && !post.following) {
      post.text = genericText(item);          // never drop a post because its shape is unfamiliar
      post.generic = true;
    }
    if (post.deleted && !post.author) post.author = person(item.user) || null;
    const lp = post.link_preview || {};
    post.mints = findMints(
      post.coin && post.coin.mint, post.text, post.urls.join(" "), lp.title, lp.description,
      post.quoted && post.quoted.text, post.subtweet && post.subtweet.text, post.reply && post.reply.text,
      post.deleted && post.deleted.text,
    );
    post.key = `${feed}:${post.type}:${post.id || hash(JSON.stringify([post.text, post.author, post.coin, post.created_at]))}`;
    return post;
  }

  // ------------------------------------------------------------------ Discord
  const FEED_COLORS = { "X Tracker": 0x1d9bf0, "Fomo Trades": 0x22c55e, "Fomo Thesis": 0xa855f7 };

  function short(n) {
    if (n === null || n === undefined) return null;
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n * 100) / 100);
  }

  function clip(s, n) {
    s = str(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function httpsUrl(u) {
    u = str(u);
    return /^https:\/\/\S+$/.test(u) && u.length <= 2000 ? u : null;
  }

  function isX(post) {
    return /tweet|reply|quote|retweet|follow|ct_tracker|pinned/i.test(`${post.type} ${post.source || ""}`)
      || (post.platform && /^(x|twitter)$/i.test(post.platform));
  }

  function title(post) {
    const t = post.type.toUpperCase();
    const h = (b) => (b && b.handle ? ` @${b.handle}` : "");
    if (/thesis/i.test(post.type)) return `💡 Thesis${post.coin && post.coin.symbol ? " $" + post.coin.symbol : ""}`;
    if (post.coin || post.side) {
      const side = (post.side || "").toUpperCase();
      const icon = side === "BUY" ? "🟢" : side === "SELL" ? "🔴" : "📣";
      const sym = post.coin && post.coin.symbol ? ` $${post.coin.symbol}` : "";
      return `${icon} ${side || post.type}${sym}`;
    }
    if (t === "TWEET") return "🐦 Tweet";
    if (t === "REPLY") return `↩️ Reply${post.reply ? " to" + h(post.reply) : post.subtweet ? " to" + h(post.subtweet) : ""}`;
    if (t === "QUOTE") return `💬 Quote${post.quoted ? " of" + h(post.quoted) : ""}`;
    if (t === "RETWEET") return `🔁 Retweet${post.retweeted ? " of" + h(post.retweeted) : ""}`;
    if (/delete/i.test(post.type)) return "🗑️ Deleted a tweet";
    if (/unfollow/i.test(post.type)) return `👋 Unfollowed${h(post.following)}`;
    if (/follow/i.test(post.type)) return `➕ ${post.action ? clip(post.action, 40) : "Follow update"}${h(post.following)}`;
    if (/pin/i.test(post.type)) return "📌 Pinned a tweet";
    return `📰 ${clip(post.type, 60)}`;
  }

  function buildMessage(post) {
    const a = post.author || {};
    const x = isX(post);
    const lines = [];
    if (post.text) lines.push(clip(post.text, 1500));
    const quote = (label, b) => {
      if (b && (b.text || b.handle)) lines.push(`> **${label}${b.handle ? " @" + b.handle : ""}:** ${clip(b.text || "", 500).replace(/\n/g, "\n> ")}`);
    };
    quote("Quoting", post.quoted);
    if (!post.quoted) quote("Replying to", post.reply || (post.type.toUpperCase() === "REPLY" ? post.subtweet : null));
    if (post.type.toUpperCase() !== "REPLY") quote("On", post.subtweet);
    quote("Deleted", post.deleted);
    if (post.link_preview && (post.link_preview.title || post.link_preview.description)) {
      lines.push(`🔗 **${clip(post.link_preview.title || "", 150)}** ${clip(post.link_preview.description || "", 200)}`.trim());
    }
    if (post.urls.length) lines.push(post.urls.map((u) => `<${u}>`).join("\n"));
    let description = clip(lines.join("\n\n"), 3500);

    const fields = [];
    if (post.coin) {
      const c = post.coin;
      fields.push({ name: "Coin", value: clip(`$${c.symbol || "?"}${c.name ? " · " + c.name : ""}${c.marketCap ? " · MC $" + short(c.marketCap) : ""}`, 1000), inline: true });
    }
    const side = (post.side || "").toUpperCase();
    if (post.amount !== null && post.amount !== undefined) {
      const unit = post.amountUnit === "USD" ? `$${short(post.amount)}` : `${short(post.amount)} ${post.amountUnit && post.amountUnit.length <= 6 ? post.amountUnit : "(quote)"}`;
      fields.push({ name: "Trade", value: clip(`${side} ${unit}${post.firstBuy ? " · first buy" : ""}${post.platform ? " · " + post.platform : ""}`.trim(), 1000), inline: true });
    } else if (post.solAmount !== null && post.solAmount !== undefined) {
      fields.push({ name: "Trade", value: clip(`${side} ${short(post.solAmount)} SOL${post.venue ? " on " + post.venue : ""}`.trim(), 1000), inline: true });
    }
    for (const m of post.mints.slice(0, 3)) {
      fields.push({ name: "CA", value: `\`${m}\`\n[pump.fun](https://pump.fun/coin/${m}) · [DexScreener](https://dexscreener.com/solana/${m})`, inline: false });
    }

    const embed = {
      title: clip(title(post), 250),
      color: FEED_COLORS[post.feed] || 0x64748b,
      footer: { text: clip(`Uxento Vision · ${post.feed}${post.fromHistory ? " · catch-up" : ""}${post.generic ? " · unrecognised post type" : ""}`, 200) },
    };
    if (description) embed.description = description;
    if (fields.length) embed.fields = fields.slice(0, 10);
    const handle = a.handle;
    if (handle || a.name) {
      const followers = a.followers !== null && a.followers !== undefined ? ` · ${short(a.followers)} followers` : "";
      embed.author = { name: clip(`${a.name || handle}${handle ? " (@" + handle + ")" : ""}${a.verified ? " ✓" : ""}${followers}`, 250) };
      if (handle && x) embed.author.url = `https://x.com/${encodeURIComponent(handle)}`;
      const icon = httpsUrl(a.avatar);
      if (icon) embed.author.icon_url = icon;
    }
    if (x && handle && post.id && /^\d{5,25}$/.test(post.id) && !/follow/i.test(post.type)) {
      embed.url = `https://x.com/${encodeURIComponent(handle)}/status/${post.id}`;
    } else if (post.mints.length) {
      embed.url = `https://pump.fun/coin/${post.mints[0]}`;
    }
    const image = httpsUrl(post.images[0]) || httpsUrl(post.link_preview && post.link_preview.image);
    if (image) embed.image = { url: image };
    else if (post.coin && httpsUrl(post.coin.image)) embed.thumbnail = { url: post.coin.image };
    if (post.created_at) {
      const ms = post.created_at < 1e12 ? post.created_at * 1000 : post.created_at;
      const d = new Date(ms);
      if (!isNaN(d.getTime())) embed.timestamp = d.toISOString();
    }
    return { username: "Uxento Vision", embeds: [embed], allowed_mentions: { parse: [] } };
  }

  // text-only fallback when Discord refuses an embed (a bad image URL, an odd character): never lose the post
  function plainMessage(post) {
    const a = post.author || {};
    const head = `**${title(post)}**${a.handle ? " — @" + a.handle : ""} · ${post.feed}`;
    const body = [post.text, post.deleted && post.deleted.text, post.mints.length ? "CA: " + post.mints.join(" ") : ""]
      .filter(Boolean).join("\n");
    return { username: "Uxento Vision", content: clip(`${head}\n${body}`, 1900), allowed_mentions: { parse: [] } };
  }

  const api = { normalize, buildMessage, plainMessage, findMints, hash, title };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.__NSVisionLib = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
