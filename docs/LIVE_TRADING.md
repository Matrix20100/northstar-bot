# Live trading and the exit engine (v5.0.0)

Not financial advice. Most memecoins in the bot's band go to zero within a day. Only fund the bot's
wallet with an amount you can lose completely.

## The exit engine (paper and real)

Every open position is re-read every `NORTHSTAR_EXIT_POLL_SECONDS` (10 s) from its pool on
DexScreener. `northstar/exits.py` then decides what to sell and records why:

| Rule | Default | What it does |
|---|---|---|
| Stop loss | -20% | Sells everything when the price is 20% under the entry. |
| Trailing stop | from 1.3x | First moves the stop to break-even. It then follows the high, giving back at most 25% below 2x, 20% from 2x and 15% from 5x. It only ever moves up. |
| Sellers took over | 1.5 sells per buy, -10% in 5 min | Sells everything before the dump finishes (only while under the high). |
| Volume faded | 5-min volume under 25% of the hour's pace | Sells when the move is over (10+ min in, 15% under the high). |
| Never got going | 8 min, never 1.15x, now under 0.92x | Cuts dead money early. |
| Time stop | 180 min | Curve coins rarely survive the day. |
| Take profit | 35% at 1.5x, 35% at 2.5x | The rest rides the trailing stop. |

While a coin is making new highs with buyers in control, the momentum rules wait. The stop and the
trailing stop always apply.

Every value is a setting (Settings tab → "Exits (paper and real)"). The paper accounts use the engine
by default (`NORTHSTAR_PAPER_EXIT_MODE=engine`; `plan` follows the posted trade plan as before). The
Calls tab's **Bot exit** column shows where the bot sold each call: average sell multiple, the
result after fees, and the reason.

## Live trading: setup

1. **Create a NEW wallet used only by the bot**, e.g. a new account in Phantom or Solflare. Never use
   your main wallet.
2. **Fund it** with what you're prepared to lose, plus about 0.05 SOL for fees.
3. **Export its private key** (Phantom: Settings → Manage accounts → the account → Show private key).
4. On Wispbyte, open the File Manager, edit `.env` in the server's main folder and add:
   ```
   NORTHSTAR_TRADE_WALLET_PRIVATE_KEY=<the exported key>
   ```
   `.env` is git-ignored and never pulled from or pushed to GitHub. The dashboard can't show or change
   the key. The bot only ever shows the wallet's public address.
5. Restart. The **Trading** tab shows the wallet and its balance. Trading is **off** and in
   **test mode**.
6. Set your limits (Settings tab → "Live trading (real money)"):

   | Setting | Default | Meaning |
   |---|---|---|
   | `trade_sol_per_trade` | 0.1 | SOL spent per buy. |
   | `trade_max_open` | 3 | Positions open at once. |
   | `trade_daily_loss_limit_sol` | 0.5 | No new buys once today's result (UTC) reaches -0.5 SOL. Exits keep running. |
   | `trade_min_sol_reserve` | 0.03 | Always left in the wallet for fees. |
   | `trade_max_chase_pct` | 25 | Skips a call whose coin is already up 25% since the call. |
   | `trade_slippage_pct` | 15 | Slippage allowed per trade. |
   | `trade_follow` | runner | `runner` = runner calls only; `all` = every call except rug scalps. |

7. **Turn trading on in test mode first.** Every trade is built by PumpPortal, signed by the bot and
   **simulated** on-chain (`simulateTransaction`), but never sent. Positions show a "test" tag. This
   checks the whole loop with your real wallet without spending anything. Leave it for a day.
8. When you're satisfied, type `REAL` and press **Use real money**.

## Controls

- **Turn trading off**: no new buys. Open positions are still sold by the exit engine.
- **Stop & sell everything** (kill switch): turns trading off (it stays off after a restart) and sells
  every open position at once.
- To take money out, send SOL from the bot's wallet in your wallet app at any time.

## How a trade works

1. **Buy.** The bot asks PumpPortal's Local Transaction API (`https://pumpportal.fun/api/trade-local`)
   to build a buy. Pool is "auto": pump.fun curve, PumpSwap or Raydium.
2. **Check and sign.** The bot checks that the transaction is for its own wallet, then signs it on
   the server (`northstar/solana_tx.py`).
3. **Send and read back.** It sends the transaction through your RPC and waits for confirmation. The
   SOL spent and tokens received are read from the confirmed transaction itself.
4. **Sell.** Sells use a percentage of the bot's token balance ("35%", "100%"). The SOL received is
   read from the chain the same way.
5. **Failures.** A failed sell is retried on the next tick. A failed buy is recorded and skipped.

## Limits of this version

- It hasn't been run against the live PumpPortal API from the development environment. That's what
  test mode is for: check a day of simulated trades before switching to real money.
- Prices come from DexScreener every 10 s. A rug that drains a pool between two reads can still fill
  far below the stop. Stop-losses on memecoins can't guarantee a price.
- Every trade pays the network fee, the priority fee (`trade_priority_fee_sol`) and PumpPortal's
  trading fee.
