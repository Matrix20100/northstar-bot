"""Northstar application slash commands. Commands are read-only except watchlist and admin controls."""
from __future__ import annotations
import asyncio, json, logging, threading, time
import discord
from discord import app_commands
from discord.ext import commands
from ..db import Database
from ..research import Researcher
from ..engine import build_providers
from .embeds import usd_or_sol

log = logging.getLogger('northstar.discord.commands')

class NorthstarCommands(commands.Bot):
    def __init__(self, settings, db_path: str):
        intents = discord.Intents.none(); intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings, self.db_path = settings, db_path
        self.started_at = time.time()
        self.paused = False

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            log.info('Synced %d Northstar slash commands', len(synced))
        except Exception:
            log.exception('Slash command sync failed')

    async def on_ready(self):
        log.info('Northstar commands online as %s', self.user)

    def db(self):
        d = Database(self.db_path); d.open(); return d

    async def adb(self):
        """Open the database off the event loop (integrity check + schema write can be slow)."""
        return await asyncio.to_thread(self.db)

    def admin_only(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.guild and interaction.user.guild_permissions.administrator)


bot_ref = None

def _clip(text, limit=4000):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + '…'

def _value(report, key):
    """Read an OBSERVED market value from a research report ({'value':..., 'status':...})."""
    item = (report.get('market') or {}).get(key)
    if isinstance(item, dict):
        item = item.get('value')
    return item if isinstance(item, (int, float)) else None

def _money(v):
    if v is None:
        return 'UNKNOWN'
    if v >= 1e6:
        return f'${v/1e6:.2f}M'
    if v >= 1e3:
        return f'${v/1e3:.1f}K'
    return f'${v:,.2f}' if v >= 1 else f'${v:.4g}'

def _embed(title, description, color=0x18212B):
    e=discord.Embed(title=title, description=_clip(description), color=color, timestamp=discord.utils.utcnow())
    e.set_footer(text='NORTHSTAR • Research is informational, not a trade instruction')
    return e

async def _ack(interaction, ephemeral=True):
    """Acknowledge within Discord's 3-second window before doing any work."""
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)


UNKNOWN_MESSAGE, UNKNOWN_WEBHOOK, UNKNOWN_INTERACTION = 10008, 10015, 10062


def _gone(exc) -> bool:
    """True when Discord says the 'thinking...' placeholder or the interaction no longer exists.

    Happens when the placeholder is deleted while research runs, or the 15-minute
    interaction token expires. The result itself is fine; only the reply target is gone.
    """
    return getattr(exc, "code", None) in (UNKNOWN_MESSAGE, UNKNOWN_WEBHOOK, UNKNOWN_INTERACTION)


async def _reply(interaction, *, embed=None, content=None, ephemeral=True):
    kwargs = {"ephemeral": ephemeral}
    if embed is not None:
        kwargs["embed"] = embed
    if content is not None:
        kwargs["content"] = content
    if not interaction.response.is_done():
        await interaction.response.send_message(**kwargs)
        return
    try:
        await interaction.followup.send(**kwargs)
        return
    except Exception as exc:  # noqa: BLE001 - only the "reply target vanished" case is handled here
        if not _gone(exc):
            raise
        log.warning("/%s reply target is gone (Discord %s); %s",
                    getattr(interaction.command, "name", "?"), getattr(exc, "code", "?"),
                    "posting in the channel instead" if not ephemeral else "private reply dropped")
    if ephemeral:
        return  # a private answer must never be re-posted publicly
    channel = getattr(interaction, "channel", None)
    if channel is None:
        return
    mention = getattr(getattr(interaction, "user", None), "mention", None)
    kwargs.pop("ephemeral", None)
    if mention:
        kwargs["content"] = (mention + " " + (kwargs.get("content") or "")).strip()
    try:
        await channel.send(**kwargs)
    except Exception:  # noqa: BLE001
        log.warning("could not post the fallback reply in the channel either", exc_info=True)


async def _close(db):
    await asyncio.to_thread(db.close)


def socials_line(soc: dict) -> str:
    """One line: which links exist, Telegram size, recycled warning. X activity is never claimed."""
    kinds = {l.get('kind') for l in soc.get('links') or []}
    parts = []
    if soc.get('recycled'):
        parts.append("⚠️ RECYCLED from other tokens (" + ", ".join(soc['recycled']) + ")")
    parts.append("X " + ("account" if 'x_account' in kinds else "community" if 'x_community' in kinds
                         else "post link only" if 'x_post' in kinds else "none"))
    if soc.get('telegram_members') is not None:
        parts.append(f"Telegram {soc['telegram_members']:,} members")
    elif 'telegram' in kinds:
        parts.append("Telegram (size unknown)")
    if 'website' in kinds:
        parts.append("website")
    return " • ".join(parts)


def research_summary(out) -> tuple[str, str, int]:
    """Title, description and colour for a research outcome (pool or bonding curve)."""
    r = out.report
    ident = r.get('identity') or {}
    mint = r.get('mint', '')
    title = f"{out.action.value} • {ident.get('name') or 'Unknown token'} (${ident.get('symbol') or '?'})"
    lines = [f"`{mint}`", ""]
    # One score: out.score IS the decision's score (northstar/final.py). v2.4 printed the old
    # v2.3 opportunity score here while the reasons below quoted the unified score.
    final = r.get('final') or {}
    score = f"{out.score:.0f}/100" if isinstance(out.score, (int, float)) else 'UNAVAILABLE'
    if final.get('score_source') and final.get('score_source') != 'unified decision engine':
        score += f" ({final['score_source']})"
    lines.append(f"**Score:** {score}  • **Risk:** {out.risk.label}")
    if final.get('decision'):
        lines.append(f"**Decision:** {final['decision']} • **Safety:** {final.get('safety') or '?'} • "
                     f"**Entry timing:** {final.get('early_entry') or 'UNKNOWN'} • "
                     f"**Confidence:** {final.get('confidence')} • **Why:** "
                     f"{(final.get('reason_class') or '?').replace('_', ' ').lower()}")
    pm = r.get('pre_migration')
    if r.get('lifecycle') == 'PRE_MIGRATION_BONDING_CURVE' and pm:
        delta = pm.get('market_cap_change_pct')
        lines += [
            "**Stage:** Bonding curve (pre-migration) — read on-chain",
            f"**Market Cap:** {usd_or_sol(pm.get('market_cap_sol_current'), pm.get('sol_usd'))}"
            f" • **5m Buy Flow:** {usd_or_sol(pm.get('buy_sol_5m', 0), pm.get('sol_usd'))}",
            f"**Since Launch:** {delta:+.1f}%" if isinstance(delta, (int, float)) else "**Since Launch:** UNAVAILABLE",
            f"**5m Trades:** {pm.get('trade_events', 0)} ({pm.get('buys', 0)} buys / {pm.get('sells', 0)} sells)"
            f" • **Unique Buyers:** {pm.get('unique_buyers', 0)}",
            f"**Holders:** {pm.get('holder_risk') or (r.get('security') or {}).get('holder_status') or 'UNKNOWN'}",
        ]
        if pm.get('creator'):
            c = pm['creator']
            lines.append(f"**Creator:** `{c[:4]}…{c[-4:]}`")
    else:
        price = _value(r, 'price_usd'); liq = _value(r, 'liquidity_usd'); mcap = _value(r, 'market_cap')
        price_text = (f'${price:.4g}' if price is not None and price < 1 else _money(price))
        lines.append(f"**Price:** {price_text}  • **Liquidity:** {_money(liq)}  • **Market cap:** {_money(mcap)}")
    soc = (pm or {}).get('socials') or (r.get('narrative') or {}).get('free_socials')
    if soc:
        lines.append("**Socials:** " + socials_line(soc))
    if r.get('verdict'):
        from ..analysis.verdict import embed_text
        lines += ["", "**🧠 Rug check**", embed_text(r['verdict'], max_red=5, max_green=3)]
    lines += ["", "**Reasons**"] + ['• ' + str(x) for x in out.reasons[:6]]
    colour = 0x2ECC71 if out.action.value == 'AUTO_POST' else 0xE74C3C if out.action.value == 'REJECT' else 0xE67E22
    return title, "\n".join(lines), colour


async def run_research(bot, token: str):
    db = await bot.adb()
    providers, clients = build_providers(bot.settings, db)
    try:
        return await Researcher(bot.settings, db, providers).research(token.strip(), force=True)
    finally:
        for c in clients:
            await c.close()
        await _close(db)


async def more_info(bot, interaction) -> None:
    """v3.0.0 "More info" button on a call: the full research, only to the person who clicked."""
    from .embeds import MORE_INFO_PREFIX, more_info_embeds
    cid = str((interaction.data or {}).get('custom_id') or '')
    mint = cid[len(MORE_INFO_PREFIX):].strip()[:64]
    await interaction.response.defer(ephemeral=True, thinking=True)
    db = await bot.adb()
    try:
        row = db.fetchone('SELECT report FROM research_snapshots WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,))
        if row is None:
            try:
                row = db.fetchone('SELECT report FROM early_calls WHERE mint=?', (mint,))
            except Exception:  # noqa: BLE001 - no early calls table yet
                row = None
    finally:
        await _close(db)
    try:
        report = json.loads(row['report']) if row else None
    except (TypeError, ValueError):
        report = None
    if not report:
        await interaction.followup.send('No saved research for this coin any more.', ephemeral=True)
        return
    embeds = [discord.Embed.from_dict(e) for e in more_info_embeds(report)][:10]
    await interaction.followup.send(embeds=embeds, ephemeral=True)


def install_commands(bot: NorthstarCommands):
    async def on_interaction(interaction: discord.Interaction):
        from .embeds import MORE_INFO_PREFIX
        if interaction.type != discord.InteractionType.component:
            return
        if not str((interaction.data or {}).get('custom_id') or '').startswith(MORE_INFO_PREFIX):
            return
        try:
            await more_info(bot, interaction)
        except Exception:
            log.exception('More info button failed')
            try:
                if interaction.response.is_done():
                    await interaction.followup.send('Could not load the research for this coin.', ephemeral=True)
                else:
                    await interaction.response.send_message('Could not load the research for this coin.',
                                                            ephemeral=True)
            except Exception:  # noqa: BLE001
                pass

    bot.on_more_info = on_interaction
    if hasattr(bot, 'event'):
        bot.event(on_interaction)

    @bot.tree.error
    async def on_tree_error(interaction: discord.Interaction, error):
        cause = getattr(error, 'original', None) or error
        if _gone(cause):
            log.warning('Slash command /%s: Discord reply target gone (%s); nothing else failed',
                        getattr(interaction.command, 'name', '?'), getattr(cause, 'code', '?'))
            return
        log.error('Slash command /%s failed: %s', getattr(interaction.command, 'name', '?'), error,
                  exc_info=error)
        try:
            await _reply(interaction, content='Command failed. The error was logged; no data was fabricated.')
        except Exception:
            log.exception('could not report slash command failure')

    @bot.tree.command(name='research', description='Research a Solana token address')
    @app_commands.describe(token='Solana mint address')
    async def research(interaction: discord.Interaction, token: str):
        await _ack(interaction, ephemeral=False)
        out = await run_research(bot, token)
        title, desc, colour = research_summary(out)
        await _reply(interaction, embed=_embed(title, desc, colour), ephemeral=False)

    @bot.tree.command(name='check', description='Quick risk and data-quality check')
    async def check(interaction: discord.Interaction, token: str):
        await _ack(interaction)
        db=await bot.adb()
        try:
            row=db.fetchone('SELECT action,score,risk,confidence,report,ts FROM research_snapshots WHERE mint=? ORDER BY ts DESC LIMIT 1',(token.strip(),))
            if not row:
                # Nothing saved yet: run a fresh on-chain/DEX assessment instead of giving up.
                await _close(db); db = None
                out = await run_research(bot, token)
                title, desc, colour = research_summary(out)
                return await _reply(interaction, embed=_embed('QUICK CHECK • ' + title, desc, colour))
            else:
                r=json.loads(row['report'] or '{}'); flags=r.get('flags') or []
                msg=f"Decision: **{row['action']}** • Score: **{row['score'] if row['score'] is not None else 'UNKNOWN'}** • Risk: **{row['risk'] or 'UNKNOWN'}**\n"+'\n'.join('• '+str(f.get('message','Risk finding')) for f in flags[:8])
            await _reply(interaction, embed=_embed('QUICK CHECK',msg))
        finally:
            if db is not None: await _close(db)

    @bot.tree.command(name='scan', description='Show the latest scan queue and decisions')
    async def scan(interaction: discord.Interaction):
        await _ack(interaction)
        db=await bot.adb()
        try:
            rows=db.fetchall('SELECT symbol,mint,state,last_score,last_risk,updated_at FROM candidates ORDER BY updated_at DESC LIMIT 10')
            msg='\n'.join(f"`{r['state']}` **{r['symbol'] or 'UNKNOWN'}** • score {r['last_score'] if r['last_score'] is not None else '?'} • risk {r['last_risk'] or '?'}\n`{r['mint']}`" for r in rows) or 'No candidates recorded yet.'
            await _reply(interaction, embed=_embed('LATEST SCAN QUEUE',msg))
        finally: await _close(db)

    @bot.tree.command(name='calls', description='List recent Northstar callouts')
    async def calls(interaction: discord.Interaction):
        await _ack(interaction)
        db=await bot.adb()
        try:
            rows=db.fetchall("SELECT symbol,mint,status,entry_mcap,current_mcap,created_at FROM alerts ORDER BY created_at DESC LIMIT 10")
            msg='\n'.join(f"**{r['symbol'] or 'UNKNOWN'}** — {r['status']} — entry MC ${r['entry_mcap'] or 'UNKNOWN'} — current MC ${r['current_mcap'] or 'UNKNOWN'}\n`{r['mint']}`" for r in rows) or 'No callouts recorded yet.'
            await _reply(interaction, embed=_embed('RECENT CALLOUTS',msg))
        finally: await _close(db)

    @bot.tree.command(name='performance', description='Show measured callout performance')
    async def performance(interaction: discord.Interaction):
        from ..performance import performance_report, format_report
        await _ack(interaction)
        db=await bot.adb()
        try:
            report=performance_report(db)
            await _reply(interaction, embed=_embed('PERFORMANCE',format_report(report)[:4000]))
        finally: await _close(db)

    @bot.tree.command(name='paper', description='Paper trading: how the simulated account following every call is doing')
    async def paper(interaction: discord.Interaction):
        from .. import paper as PT
        await _ack(interaction)
        db=await bot.adb()
        try:
            d=PT.summary(db, bot.settings)
        finally: await _close(db)
        run=d['run']
        def usd(v): return '—' if v is None else ('-' if v<0 else '')+f"${abs(v):,.2f}"
        lines=[f"**Balance:** {usd(d['equity'])} (started with {usd(run['start_usd'])})",
               f"**Profit / loss:** {'+' if d['pnl']>=0 else ''}{usd(d['pnl'])} ({d['pnl_pct']:+.1f}%)",
               f"**Cash:** {usd(d['cash'])} · **open:** {d['open']} worth {usd(d['open_value'])}",
               f"**Closed trades:** {d['closed']} ({d['wins']} in profit)"]
        recent=[t for t in d['trades'] if t['status']!='SKIPPED'][:6]
        if recent:
            lines.append('')
            lines+=[f"• ${t['symbol'] or t['mint'][:6]}: "+(f"{'+' if t['pnl']>=0 else ''}{usd(t['pnl'])} ({t['pnl_pct']:+.0f}%)" if t['pnl'] is not None else '—')
                    +(f" · {t['exit']}" if t['status']=='CLOSED' else ' · open') for t in recent]
        lines.append(f"\n{run['position_pct']:g}% of cash per call · {run['fee_pct']:g}% fee per side · simulated, no real trades. Change the amount on the dashboard's Paper tab.")
        await _reply(interaction, embed=_embed('PAPER TRADING','\n'.join(lines)[:4000]))

    @bot.tree.command(name='why', description='Explain the latest saved decision for a token')
    async def why(interaction: discord.Interaction, token: str):
        await _ack(interaction)
        db=await bot.adb()
        try:
            row=db.fetchone('SELECT action,score,risk,confidence,report,ts FROM research_snapshots WHERE mint=? ORDER BY ts DESC LIMIT 1',(token.strip(),))
            if not row: msg='No saved decision found. Run `/research` first.'
            else:
                r=json.loads(row['report'] or '{}'); reasons=r.get('reasons') or (r.get('decision') or {}).get('reasons') or []
                flags=r.get('flags') or []
                msg=f"Decision: **{row['action']}** • score **{row['score'] if row['score'] is not None else 'UNKNOWN'}** • risk **{row['risk'] or 'UNKNOWN'}**\n\n**Reasons**\n"+'\n'.join('• '+str(x) for x in reasons[:8])+'\n\n**Flags**\n'+'\n'.join('• '+str(x.get('message')) for x in flags[:6])
            await _reply(interaction, embed=_embed('WHY THIS DECISION?',msg))
        finally: await _close(db)

    @bot.tree.command(name='watchlist', description='Show your saved watchlist')
    async def watchlist(interaction: discord.Interaction):
        await _ack(interaction)
        db=await bot.adb()
        try:
            db.execute('CREATE TABLE IF NOT EXISTS user_watchlist (user_id TEXT NOT NULL,mint TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(user_id,mint))')
            rows=db.fetchall('SELECT mint,created_at FROM user_watchlist WHERE user_id=? ORDER BY created_at DESC LIMIT 20',(str(interaction.user.id),))
            msg='\n'.join('• `'+r['mint']+'`' for r in rows) or 'Your watchlist is empty. Use `/watch_add` to add a mint.'
            await _reply(interaction, embed=_embed('YOUR WATCHLIST',msg))
        finally: await _close(db)

    @bot.tree.command(name='watch_add', description='Add a token mint to your personal watchlist')
    async def watch_add(interaction: discord.Interaction, token: str):
        await _ack(interaction)
        db=await bot.adb()
        try:
            db.execute('CREATE TABLE IF NOT EXISTS user_watchlist (user_id TEXT NOT NULL,mint TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(user_id,mint))')
            db.execute('INSERT OR IGNORE INTO user_watchlist(user_id,mint,created_at) VALUES(?,?,?)',(str(interaction.user.id),token.strip(),time.time()))
            await _reply(interaction, content='Added to your watchlist: `'+token.strip()+'`')
        finally: await _close(db)

    @bot.tree.command(name='compare', description='Compare the latest saved research for two tokens')
    async def compare(interaction: discord.Interaction, token1: str, token2: str):
        await _ack(interaction)
        db=await bot.adb()
        try:
            lines=[]
            for mint in (token1.strip(),token2.strip()):
                r=db.fetchone('SELECT action,score,risk,confidence FROM research_snapshots WHERE mint=? ORDER BY ts DESC LIMIT 1',(mint,))
                lines.append(f"`{mint}`\nDecision: {r['action'] if r else 'NO DATA'} • Score: {r['score'] if r and r['score'] is not None else 'UNKNOWN'} • Risk: {r['risk'] if r else 'UNKNOWN'}")
            await _reply(interaction, embed=_embed('TOKEN COMPARISON','\n\n'.join(lines)))
        finally: await _close(db)

    @bot.tree.command(name='deepresearch', description='Run full research now for a Solana token')
    async def deepresearch(interaction: discord.Interaction, token: str):
        await research.callback(interaction, token)

    @bot.tree.command(name='northstar_status', description='Admin: inspect bot and provider status')
    async def northstar_status(interaction: discord.Interaction):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        await _ack(interaction)
        db=await bot.adb()
        try:
            health=db.provider_health(); rows='\n'.join(f"• **{x['provider']}**: {x['status']} (errors {x['err_count']})" for x in health) or 'No provider health recorded yet.'
            await _reply(interaction, embed=_embed('NORTHSTAR STATUS',f"Commands: ONLINE\nEngine pause flag: {bot.paused}\n\n{rows}"))
        finally: await _close(db)

    @bot.tree.command(name='northstar_diagnostics', description='Admin: show recent candidates and provider errors')
    async def northstar_diagnostics(interaction: discord.Interaction):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        await _ack(interaction)
        db=await bot.adb()
        try:
            rows=db.provider_health(); msg='\n'.join(f"**{r['provider']}** — {r['status']} — {r['last_error'] or 'no recent error'}" for r in rows) or 'No provider diagnostics available.'
            await _reply(interaction, embed=_embed('DIAGNOSTICS',msg))
        finally: await _close(db)

    @bot.tree.command(name='northstar_settings', description='Admin: view current scan thresholds and controls')
    async def northstar_settings(interaction: discord.Interaction):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        s=bot.settings
        msg=f"Minimum liquidity: ${s.min_liquidity_usd:,.0f}\nMinimum liquidity/MC: {s.min_liquidity_to_mcap:.0%}\nMax top-10 adjusted: {s.max_top10_adjusted_pct:.1f}%\nMax creator holding: {s.max_creator_holding_pct:.1f}%\nDry run: {s.dry_run}\n\nThis command is view-only; edit environment settings and restart to change thresholds."
        await _reply(interaction, embed=_embed('CURRENT SETTINGS',msg))

    @bot.tree.command(name='learning', description='What Northstar has learned from its own results')
    async def learning_cmd(interaction: discord.Interaction):
        await _ack(interaction)
        from .. import learning as L
        import copy
        db = await bot.adb()
        try:
            L.init_schema(db)
            s = copy.copy(bot.settings)
            L.apply_learned(db, s)
            info = await asyncio.to_thread(L.summary, db, s, s.learning_days)
        finally:
            await _close(db)
        def pct_s(v):
            return 'n/a' if v is None else f"{v * 100:+.1f}%"
        lines = [f"**Coins scored on their real price path:** {info['evaluated']} (waiting: {info['pending']})",
                 f"**Standard calls (simulated):** {info['standard_trades']} trades · avg {pct_s(info['standard_avg'])} each",
                 f"**Rug scalps (simulated):** {info['scalp_trades']} trades · avg {pct_s(info['scalp_avg'])} each",
                 f"**Median peak after a decision:** {info['median_peak'] or 'n/a'}x", ""]
        if info['learned']:
            lines.append("**Learned settings (override .env):**")
            lines += [f"• `{k}` = {v:g}" for k, v in sorted(info['learned'].items())]
        else:
            lines.append("No learned changes yet — settings are your .env values.")
        lines.append(f"\n**🧠 Brain:** {info['brain']}")
        if info.get('brain_signals'):
            lines.append("**What predicts a runner:** " + ", ".join(
                f"{'▲' if c > 0 else '▼'} {name}" for name, c in info['brain_signals']))
        recent = [r for r in info['recent_changes'] if r['key'] != 'RESET'][:5]
        if recent:
            lines.append("\n**Recent changes:**")
            for r in recent:
                lines.append(f"• <t:{int(r['ts'])}:R> `{r['key']}` {r['old']:g} → {r['new']:g}")
        lines.append("\nSimulated = the trade plan replayed on 1-minute candles with fees and worst-case fills. "
                     "Not a promise of future results.")
        await _reply(interaction, embed=_embed('🧠 NORTHSTAR LEARNING', "\n".join(lines)))

    @bot.tree.command(name='learning_reset', description='Admin: forget learned settings (back to .env values)')
    async def learning_reset(interaction: discord.Interaction):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        await _ack(interaction)
        from .. import learning as L
        db = await bot.adb()
        try:
            L.init_schema(db)
            keys = L.reset(db, bot.settings, bot.settings)
        finally:
            await _close(db)
        msg = ("Forgot: " + ", ".join(f"`{k}`" for k in keys)) if keys else "Nothing had been learned yet."
        msg += "\nThe engine switches back to your .env values within a minute. Learning keeps running."
        await _reply(interaction, embed=_embed('🧠 LEARNING RESET', msg))

    @bot.tree.command(name='untrack', description='Admin: stop tracking a coin (watch list, research, call and early-watch updates)')
    @app_commands.describe(token='Solana mint address', reason='Why (optional, kept with the coin)')
    async def untrack(interaction: discord.Interaction, token: str, reason: str = ''):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        await _ack(interaction)
        from ..analysis.identity import is_valid_mint_format
        token = token.strip()
        if not is_valid_mint_format(token):
            return await _reply(interaction, content='That is not a Solana mint address.')
        db = await bot.adb()
        try:
            res = db.untrack(token, reason[:200], f'discord:{interaction.user}')
        finally:
            await _close(db)
        if not (res['candidate'] or res['calls_closed'] or res['early_watches_ended'] or res['early_calls_closed']):
            return await _reply(interaction, content='Northstar was not tracking that coin.')
        parts = ['No longer researched, watched or brought back by the radar.']
        if res['calls_closed']: parts.append(f"Call closed ({res['calls_closed']}).")
        if res['early_watches_ended'] or res['early_calls_closed']: parts.append('Early-watch updates stopped.')
        parts.append('Its price is still recorded for learning. Undo with /retrack.')
        await _reply(interaction, embed=_embed('🛑 UNTRACKED', f"`{token}`\n" + ' '.join(parts)))

    @bot.tree.command(name='retrack', description='Admin: start tracking a coin you untracked again')
    async def retrack(interaction: discord.Interaction, token: str):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        await _ack(interaction)
        db = await bot.adb()
        try:
            ok = db.retrack(token.strip())
        finally:
            await _close(db)
        await _reply(interaction, content=('Back in the research queue.' if ok else 'That coin was not untracked.'))

    @bot.tree.command(name='northstar_scan', description='Admin: show scan queue (does not force a new provider scan)')
    async def northstar_scan(interaction: discord.Interaction):
        if not bot.admin_only(interaction): return await _reply(interaction, content='Administrator permission required.')
        await _ack(interaction)
        db=await bot.adb()
        try:
            rows=db.fetchall('SELECT symbol,mint,state,last_score,last_risk FROM candidates ORDER BY updated_at DESC LIMIT 15')
            msg='\n'.join(f"{r['state']} | {r['symbol'] or '?'} | score {r['last_score']} | risk {r['last_risk']} | `{r['mint']}`" for r in rows) or 'Queue is empty.'
            await _reply(interaction, embed=_embed('ADMIN SCAN QUEUE',msg))
        finally: await _close(db)

def start_commands_thread(settings, db_path: str) -> threading.Thread:
    """Run slash commands on their own thread and event loop.

    The research engine does synchronous SQLite and CPU work on its loop; sharing it meant a busy
    moment could delay a command past Discord's 3-second deadline ("The application did not
    respond"). A separate loop keeps commands responsive regardless of engine load.
    """
    thread = threading.Thread(target=lambda: asyncio.run(run_commands(settings, db_path)),
                              name='northstar-slash-commands', daemon=True)
    thread.start()
    return thread


async def run_commands(settings, db_path: str):
    if not settings.discord_token:
        log.warning('Discord slash commands disabled: NORTHSTAR_DISCORD_TOKEN is not set'); return
    bot=NorthstarCommands(settings,db_path); install_commands(bot)
    try:
        await bot.start(settings.discord_token.get_secret_value())
    except Exception:
        # Slash commands are optional; never let them take the callout engine down.
        log.exception('Discord slash-command client stopped; callouts continue without commands')
    finally:
        if not bot.is_closed():
            await bot.close()
