# Data sources, verification and limitations

Endpoints were checked against provider documentation and live responses on **2026-10-15**. Providers change APIs without notice. `python -m northstar health` and the opt-in live tests (`NORTHSTAR_LIVE_TESTS=1 pytest -m live`) re-check them on your machine.

| Source | Key? | Used for | Trust label |
|---|---|---|---|
| DexScreener API | none | discovery, pairs, price, liquidity, FDV/market cap, volume, txn counts, trending metas, same-ticker search | OBSERVED |
| Solana JSON-RPC (public or Helius) | optional (Helius) | mint account, authorities, Token-2022 extensions, supply, top-20 holders, swap transactions, wallet age and funders | VERIFIED |
| RugCheck API | none | LP lock %, lockers, insider labels, creator, prior creator tokens, risk list, rugged flag, cross-checks | OBSERVED |
| PumpPortal websocket | none for free streams | pump.fun launches and migrations | OBSERVED |

## DexScreener

Base `https://api.dexscreener.com`. The documented limit is 60 requests/min for the profile, boost and meta endpoints. Pair and token endpoints are faster; Northstar defaults to 200/min to stay conservative.

| Endpoint | Shape | Use |
|---|---|---|
| `GET /token-profiles/latest/v1` | list of `{url, chainId, tokenAddress, icon, header, description, links[], …}` | discovery + narrative description |
| `GET /community-takeovers/latest/v1` | profile + `claimDate` | discovery |
| `GET /token-boosts/latest/v1`, `/token-boosts/top/v1` | list of `{url, chainId, tokenAddress, amount, totalAmount}` | discovery only; **boosts are paid promotion and earn no narrative points** |
| `GET /metas/trending/v1` | list of `{name, slug, tokenCount, marketCap, liquidity, volume, marketCapChange{m5,h1,h6,h24}, …}` | narrative timeliness and saturation |
| `GET /token-pairs/v1/solana/{mint}` | list of Pair | pool selection |
| `GET /tokens/v1/solana/{mint,…≤30}` | list of Pair | batched monitoring and outcomes |
| `GET /latest/dex/search?q=` | `{pairs: [Pair]}` | competing same-ticker tokens |

Pair fields used: `pairAddress, dexId, baseToken, quoteToken, priceUsd, priceNative, txns{m5,h1,h6,h24}{buys,sells}, volume{m5,h1,h6,h24}, priceChange{…}, liquidity{usd,base,quote}, fdv, marketCap, pairCreatedAt (ms), labels, info{imageUrl,websites,socials}, boosts{active}`.

**Limitations:** no 15-minute window; `txns` are transaction counts, not unique wallets; no free OHLC candles; no public "all new pairs" or "top movers" feed, so volume-growth discovery comes from re-researching known candidates; `marketCap` is sometimes null. Northstar then shows market cap as UNAVAILABLE and never substitutes FDV.

## Solana JSON-RPC

Default `https://api.mainnet-beta.solana.com`, or `https://mainnet.helius-rpc.com/?api-key=…` when `NORTHSTAR_HELIUS_API_KEY` is set. The key is never logged.

Methods: `getAccountInfo` (jsonParsed) for mint verification, authorities and extensions; `getTokenSupply`; `getTokenLargestAccounts` (top 20); `getMultipleAccounts` to resolve token-account owners; `getSignaturesForAddress` and `getTransaction` (jsonParsed, v0) for swap sampling, launch sampling, wallet age and first funder.

**How swaps are read:** for each transaction, the change in `preTokenBalances` and `postTokenBalances` for the mint is computed per owner. The signer whose balance changed is the trader: an increase is a buy, a decrease a sell. Pool vaults never sign, so they are not mistaken for traders.

**Limitations:** the public RPC has strict limits and often rejects `getTokenLargestAccounts` for busy mints. When that happens, Northstar falls back to RugCheck holders, labelled OBSERVED. Wallet age is only exact when the wallet has fewer than 1,000 signatures; otherwise it is reported as older than the sampled history.

## RugCheck

Base `https://api.rugcheck.xyz`. Uses `GET /v1/tokens/{mint}/report` (full report) and `GET /v1/tokens/{mint}/report/summary` (prior-token rug check). Fields used: `creator, creatorBalance, creatorTokens, token{supply,decimals,mintAuthority,freezeAuthority}, topHolders[{address,owner,pct,uiAmount,insider}], markets[{pubkey,liquidityA,liquidityB,lp{lpLockedPct,…}}], lockers{…unlockDate…}, risks[{name,level,description}], rugged, knownAccounts, price, transferFee`.

Many fields are `null` for young tokens, and Northstar treats null as UNKNOWN. RugCheck's `lpLockedPct` does not distinguish burned from locked LP unless a locker is listed, so the embed says "BURNED or LOCKED". Concentrated-liquidity pools (CLMM/DLMM/Whirlpool) have no fungible LP token to lock or burn, and the embed says so.

## PumpPortal

`wss://pumpportal.fun/api/data`, one connection, `{"method":"subscribeNewToken"}` and `{"method":"subscribeMigration"}`. Both streams are documented as free. Message fields are not formally documented, so Northstar only relies on `mint` (validated) and treats `name`, `symbol` and `traderPublicKey` as OBSERVED hints. New launches are researched after `NEW_TOKEN_INITIAL_DELAY_SECONDS` (default 10 min); migrations are researched immediately. PumpPortal times out clients that open many connections, so Northstar uses exactly one, with exponential reconnect backoff.

## What is NOT used, and why

- **X/Twitter, Telegram:** no free API with permitted access. Not scraped.
- **Helius Enhanced Transactions API:** Helius marks it as legacy/maintenance. Standard RPC gives the same balances and is stable.
- **Paid data feeds** (Birdeye, Bitquery and similar): the spec requires the basic pipeline to run free.
