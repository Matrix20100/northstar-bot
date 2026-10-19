# Rug check (the second brain)

Every coin gets one verdict, built from the same checklist a pro runs:

| Area | What is checked |
|---|---|
| Traps | mint authority, freeze authority, permanent delegate, transfer hooks/fees, paused or non-transferable tokens, RugCheck "rugged" |
| Liquidity | LP burned/locked (verified on-chain for PumpSwap), partial burns, concentrated positions, sharp liquidity drops |
| Bubblemap | who funded the top holders (two hops: dev -> middle wallet -> holder), wallets funding each other, connected groups and the supply they hold; exchange/router funders ignored as proof |
| Wallet age | age of every top holder, supply held by wallets under 24 h old, share of brand-new wallets |
| Launch | launch bundle (bought in the first block(s)), launch buyers among holders |
| Holders | top-10 share, largest single wallet, insiders |
| The dev | holdings, selling since launch, earlier tokens rugged, serial launching, recycled socials |
| Exit | what a $100 / $500 / $1,000 sell back to SOL would actually return right now (Jupiter): price impact, the biggest size that still routes, the first size that cannot be filled |
| Trading | wash trading, implausible volume, a few wallets doing most of the buying, copycat ticker |

**Levels:** ☠️ RUG LIKELY (a trap) · 🔴 HIGH RISK · 🟠 CAUTION (any red flag) · 🟢 LOOKS CLEAN (no
red flags and the key checks ran) · ⚪ NOT ENOUGH DATA.

**It learns.** Every decision is followed for 24 hours. For each red flag the bot measures how often
the coin then fell below 0.3x, shows it next to the flag ("crashed 92% of 39 times"), and upgrades a
medium flag that keeps proving deadly. The dashboard's Learning tab lists what really predicts a rug.

**Can you get out?** Looking good on paper is not the same as being sellable. Before any call, the
bot asks Jupiter for a real quote on selling $100, $500 and $1,000 of the coin back to SOL. Not being
able to sell even the smallest size is treated as a trap; a normal size that cannot be filled is high
risk on its own, whatever else looks clean. 15%+ impact is a red flag, 5%+ a yellow one, and a clean
exit is shown as a green line. No key needed.

**Limits.** Holder data covers the top 20 accounts. A wallet with 1000+ transactions has no complete
history, so its age and first funder are unknown (shown as "old / very active"). A verdict is a
summary, not a guarantee; the safety gates still decide what may be called.
