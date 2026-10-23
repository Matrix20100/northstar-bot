// Prints the Discord payloads the extension builds for synthetic Vision items (used by the Python test
// tests/test_vision_forwarder.py to check the bot's existing Discord reader understands them).
"use strict";
const L = require("../lib.js");
const MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU";
const items = [
  ["X Tracker", { id: "1849000000000000001", type: "TWEET", created_at: 1792771687000, __source: "CT_Tracker5",
    author: { handle: "alice", verified: true, profile: { name: "Alice" }, metrics: { followers: 59800 } },
    body: { text: `new coin ${MINT} $DOGWIF`, mentions: [], urls: [] } }],
  ["Fomo Trades", { type: "fomo_trade", id: "2288a518-0000", side: "buy", createdAt: 1792771690000,
    caller: { platform: "fomo", id: "c1", username: "trader1" }, coin: { mint: MINT, symbol: "DOGWIF", marketCap: 43000 },
    tokenAmount: 1000, quoteAmount: 500, quoteMint: "USD", isFirstBuy: false, isSolana: true }],
  ["X Tracker", { type: "user.unfollow", id: "u1", created_at: 1792771690000, user: { handle: "dave" },
    following: { handle: "erin" } }],
];
process.stdout.write(JSON.stringify(items.map(([feed, it]) => L.buildMessage(L.normalize(it, feed)))));
