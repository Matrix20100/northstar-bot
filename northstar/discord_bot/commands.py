"""Northstar application slash commands. Commands are read-only except watchlist and admin controls."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database
from ..research import Researcher
from ..engine import build_providers
from .embeds import usd_or_sol

log = logging.getLogger("northstar.discord.commands")


class NorthstarCommands(commands.Bot):
    def __init__(self, settings, db_path: str):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.db_path = db_path
        self.started_at = time.time()
        self.paused = False
        self._vip_expiry_task: asyncio.Task | None = None

    def init_vip_storage(self):
        """Create storage for temporary VIP roles."""
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS northstar_temporary_roles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    granted_by INTEGER,
                    PRIMARY KEY (guild_id, user_id, role_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_northstar_temporary_roles_expiry
                ON northstar_temporary_roles(expires_at)
                """
            )
            conn.commit()

    async def vip_expiry_worker(self):
        """Remove expired temporary roles. Retry failures instead of deleting their records."""
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                now = time.time()
                with sqlite3.connect(self.db_path, timeout=15) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        """
                        SELECT guild_id, user_id, role_id
                        FROM northstar_temporary_roles
                        WHERE expires_at <= ?
                        """,
                        (now,),
                    ).fetchall()

                for row in rows:
                    guild_id = int(row["guild_id"])
                    user_id = int(row["user_id"])
                    role_id = int(row["role_id"])
                    guild = self.get_guild(guild_id)

                    # If the guild is unavailable, retain the record and retry later.
                    if guild is None:
                        continue

                    role = guild.get_role(role_id)
                    if role is None:
                        # The role was deleted; there is nothing left to remove.
                        self._delete_vip_record(guild_id, user_id, role_id)
                        continue

                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        # The member left the server; the role cannot be assigned there.
                        self._delete_vip_record(guild_id, user_id, role_id)
                        continue
                    except discord.Forbidden:
                        log.error(
                            "Cannot inspect expired VIP role for user %s in guild %s: "
                            "missing permissions",
                            user_id,
                            guild_id,
                        )
                        continue
                    except Exception:
                        log.exception(
                            "Could not fetch member %s while expiring VIP role", user_id
                        )
                        continue

                    try:
                        if role in member.roles:
                            await member.remove_roles(
                                role, reason="Temporary Northstar VIP role expired"
                            )
                    except discord.Forbidden:
                        log.error(
                            "Cannot remove expired VIP role %s in guild %s: "
                            "missing permissions or role hierarchy",
                            role_id,
                            guild_id,
                        )
                        continue
                    except Exception:
                        log.exception(
                            "Failed to remove expired VIP role for user %s", user_id
                        )
                        continue

                    self._delete_vip_record(guild_id, user_id, role_id)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Temporary VIP role expiry check failed")

            await asyncio.sleep(60)

    def _delete_vip_record(self, guild_id: int, user_id: int, role_id: int):
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute(
                """
                DELETE FROM northstar_temporary_roles
                WHERE guild_id = ? AND user_id = ? AND role_id = ?
                """,
                (guild_id, user_id, role_id),
            )
            conn.commit()

    async def setup_hook(self):
        """Initialize VIP expiry and sync commands globally and to an optional guild."""
        try:
            self.init_vip_storage()
            if self._vip_expiry_task is None or self._vip_expiry_task.done():
                self._vip_expiry_task = asyncio.create_task(self.vip_expiry_worker())
        except Exception:
            # VIP commands can still be registered, but use will fail until storage is fixed.
            log.exception("Temporary VIP role system could not initialize")

        # Register globally. Guild sync below makes commands appear immediately in one server.
        try:
            synced = await self.tree.sync()
            log.info(
                "Synced %d global Northstar slash commands: %s",
                len(synced),
                [command.name for command in synced],
            )
        except Exception:
            log.exception("Global slash command sync failed")

        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if not guild_id:
            log.warning(
                "DISCORD_GUILD_ID is missing; server-specific slash command sync skipped."
            )
            return

        try:
            guild = discord.Object(id=int(guild_id))
            # Copy registered global definitions to this guild, then sync them there.
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d server-specific Northstar slash commands: %s",
                len(guild_synced),
                [command.name for command in guild_synced],
            )
        except ValueError:
            log.error("DISCORD_GUILD_ID must be a numeric Discord server ID.")
        except Exception:
            log.exception("Server-specific slash command sync failed")

    async def on_ready(self):
        log.info("Northstar commands online as %s", self.user)

    def db(self):
        d = Database(self.db_path)
        d.open()
        return d

    async def adb(self):
        """Open the database off the event loop."""
        return await asyncio.to_thread(self.db)

    def admin_only(self, interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild
            and interaction.user.guild_permissions.administrator
        )


bot_ref = None


def _clip(text, limit=4000):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _value(report, key):
    """Read an OBSERVED market value from a research report ({'value': ..., 'status': ...})."""
    item = (report.get("market") or {}).get(key)
    if isinstance(item, dict):
        item = item.get("value")
    return item if isinstance(item, (int, float)) else None


def _money(v):
    if v is None:
        return "UNKNOWN"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.2f}" if v >= 1 else f"${v:.4g}"


def _embed(title, description, color=0x18212B):
    e = discord.Embed(
        title=title,
        description=_clip(description),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    e.set_footer(text="NORTHSTAR • Research is informational, not a trade instruction")
    return e


async def _ack(interaction, ephemeral=True):
    """Acknowledge within Discord's 3-second window before doing work."""
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)


UNKNOWN_MESSAGE, UNKNOWN_WEBHOOK, UNKNOWN_INTERACTION = 10008, 10015, 10062


def _gone(exc) -> bool:
    """True when Discord says the interaction reply target no longer exists."""
    return getattr(exc, "code", None) in (
        UNKNOWN_MESSAGE,
        UNKNOWN_WEBHOOK,
        UNKNOWN_INTERACTION,
    )


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
    except Exception as exc:  # noqa: BLE001
        if not _gone(exc):
            raise
        log.warning(
            "/%s reply target is gone (Discord %s); %s",
            getattr(interaction.command, "name", "?"),
            getattr(exc, "code", "?"),
            "posting in the channel instead" if not ephemeral else "private reply dropped",
        )

    # Never turn a private response into a public message.
    if ephemeral:
        return
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
        log.warning("Could not post fallback reply in channel", exc_info=True)


async def _close(db):
    await asyncio.to_thread(db.close)


def socials_line(soc: dict) -> str:
    """Show available links and Telegram size without claiming X activity."""
    kinds = {link.get("kind") for link in soc.get("links") or []}
    parts = []
    if soc.get("recycled"):
        parts.append("⚠️ RECYCLED from other tokens (" + ", ".join(soc["recycled"]) + ")")
    parts.append(
        "X "
        + (
            "account"
            if "x_account" in kinds
            else "community"
            if "x_community" in kinds
            else "post link only"
            if "x_post" in kinds
            else "none"
        )
    )
    if soc.get("telegram_members") is not None:
        parts.append(f"Telegram {soc['telegram_members']:,} members")
    elif "telegram" in kinds:
        parts.append("Telegram (size unknown)")
    if "website" in kinds:
        parts.append("website")
    return " • ".join(parts)


def research_summary(out) -> tuple[str, str, int]:
    """Build title, description and colour for a research outcome."""
    report = out.report
    ident = report.get("identity") or {}
    mint = report.get("mint", "")
    title = f"{out.action.value} • {ident.get('name') or 'Unknown token'} (${ident.get('symbol') or '?'})"
    lines = [f"`{mint}`", ""]
    final = report.get("final") or {}
    score = f"{out.score:.0f}/100" if isinstance(out.score, (int, float)) else "UNAVAILABLE"
    if final.get("score_source") and final.get("score_source") != "unified decision engine":
        score += f" ({final['score_source']})"
    lines.append(f"**Score:** {score}  • **Risk:** {out.risk.label}")
    if final.get("decision"):
        lines.append(
            f"**Decision:** {final['decision']} • **Safety:** {final.get('safety') or '?'} • "
            f"**Entry timing:** {final.get('early_entry') or 'UNKNOWN'} • "
            f"**Confidence:** {final.get('confidence')} • **Why:** "
            f"{(final.get('reason_class') or '?').replace('_', ' ').lower()}"
        )

    pm = report.get("pre_migration")
    if report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE" and pm:
        delta = pm.get("market_cap_change_pct")
        lines += [
            "**Stage:** Bonding curve (pre-migration) — read on-chain",
            f"**Market Cap:** {usd_or_sol(pm.get('market_cap_sol_current'), pm.get('sol_usd'))}"
            f" • **5m Buy Flow:** {usd_or_sol(pm.get('buy_sol_5m', 0), pm.get('sol_usd'))}",
            f"**Since Launch:** {delta:+.1f}%" if isinstance(delta, (int, float)) else "**Since Launch:** UNAVAILABLE",
            f"**5m Trades:** {pm.get('trade_events', 0)} ({pm.get('buys', 0)} buys / {pm.get('sells', 0)} sells)"
            f" • **Unique Buyers:** {pm.get('unique_buyers', 0)}",
            f"**Holders:** {pm.get('holder_risk') or (report.get('security') or {}).get('holder_status') or 'UNKNOWN'}",
        ]
        if pm.get("creator"):
            creator = pm["creator"]
            lines.append(f"**Creator:** `{creator[:4]}…{creator[-4:]}`")
    else:
        price = _value(report, "price_usd")
        liquidity = _value(report, "liquidity_usd")
        market_cap = _value(report, "market_cap")
        price_text = f"${price:.4g}" if price is not None and price < 1 else _money(price)
        lines.append(
            f"**Price:** {price_text}  • **Liquidity:** {_money(liquidity)}  • "
            f"**Market cap:** {_money(market_cap)}"
        )

    socials = (pm or {}).get("socials") or (report.get("narrative") or {}).get("free_socials")
    if socials:
        lines.append("**Socials:** " + socials_line(socials))
    if report.get("verdict"):
        from ..analysis.verdict import embed_text
        lines += ["", "**🧠 Rug check**", embed_text(report["verdict"], max_red=5, max_green=3)]
    lines += ["", "**Reasons**"] + ["• " + str(reason) for reason in out.reasons[:6]]
    colour = (
        0x2ECC71
        if out.action.value == "AUTO_POST"
        else 0xE74C3C
        if out.action.value == "REJECT"
        else 0xE67E22
    )
    return title, "\n".join(lines), colour


async def run_research(bot, token: str):
    db = await bot.adb()
    providers, clients = build_providers(bot.settings, db)
    try:
        return await Researcher(bot.settings, db, providers).research(token.strip(), force=True)
    finally:
        for client in clients:
            await client.close()
        await _close(db)


async def more_info(bot, interaction) -> None:
    """Show saved full research privately to the user who clicked the button."""
    from .embeds import MORE_INFO_PREFIX, more_info_embeds

    custom_id = str((interaction.data or {}).get("custom_id") or "")
    mint = custom_id[len(MORE_INFO_PREFIX):].strip()[:64]
    await interaction.response.defer(ephemeral=True, thinking=True)
    db = await bot.adb()
    try:
        row = db.fetchone(
            "SELECT report FROM research_snapshots WHERE mint=? ORDER BY id DESC LIMIT 1",
            (mint,),
        )
        if row is None:
            try:
                row = db.fetchone("SELECT report FROM early_calls WHERE mint=?", (mint,))
            except Exception:  # noqa: BLE001
                row = None
    finally:
        await _close(db)

    try:
        report = json.loads(row["report"]) if row else None
    except (TypeError, ValueError):
        report = None
    if not report:
        await interaction.followup.send(
            "No saved research for this coin any more.", ephemeral=True
        )
        return
    embeds = [discord.Embed.from_dict(item) for item in more_info_embeds(report)][:10]
    await interaction.followup.send(embeds=embeds, ephemeral=True)


def install_commands(bot: NorthstarCommands):
    async def on_interaction(interaction: discord.Interaction):
        from .embeds import MORE_INFO_PREFIX

        if interaction.type != discord.InteractionType.component:
            return
        if not str((interaction.data or {}).get("custom_id") or "").startswith(MORE_INFO_PREFIX):
            return
        try:
            await more_info(bot, interaction)
        except Exception:
            log.exception("More info button failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Could not load the research for this coin.", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "Could not load the research for this coin.", ephemeral=True
                    )
            except Exception:  # noqa: BLE001
                pass

    bot.on_more_info = on_interaction
    bot.event(on_interaction)

    @bot.tree.error
    async def on_tree_error(interaction: discord.Interaction, error):
        cause = getattr(error, "original", None) or error
        if _gone(cause):
            log.warning(
                "Slash command /%s: Discord reply target gone (%s); nothing else failed",
                getattr(interaction.command, "name", "?"),
                getattr(cause, "code", "?"),
            )
            return
        log.error(
            "Slash command /%s failed: %s",
            getattr(interaction.command, "name", "?"),
            error,
            exc_info=error,
        )
        try:
            await _reply(
                interaction,
                content="Command failed. The error was logged; no data was fabricated.",
            )
        except Exception:
            log.exception("Could not report slash command failure")

    @bot.tree.command(
        name="viprole",
        description="Admin: assign a role that expires after a number of days",
    )
    @app_commands.describe(
        member="Member receiving VIP access",
        role="VIP role to assign",
        days="How many days the role lasts (1–365)",
    )
    async def viprole(
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        days: app_commands.Range[int, 1, 365],
    ):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        guild = interaction.guild
        if guild is None:
            return await _reply(interaction, content="Use this command inside your Discord server.")
        if role.is_default() or role.managed:
            return await _reply(interaction, content="Choose a normal, assignable server role.")
        me = guild.me
        if me is None or role >= me.top_role:
            return await _reply(
                interaction,
                content="Move the Northstar bot role above the VIP role in Server Settings → Roles.",
            )
        await _ack(interaction)

        expires_at = time.time() + int(days) * 86400
        try:
            # Persist expiry first so a database failure cannot leave an untracked VIP grant.
            with sqlite3.connect(bot.db_path, timeout=15) as conn:
                conn.execute(
                    """
                    INSERT INTO northstar_temporary_roles
                        (guild_id, user_id, role_id, expires_at, granted_by)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET
                        expires_at=excluded.expires_at,
                        granted_by=excluded.granted_by
                    """,
                    (guild.id, member.id, role.id, expires_at, interaction.user.id),
                )
                conn.commit()
            await member.add_roles(
                role,
                reason=f"Temporary VIP role for {days} day(s), assigned by {interaction.user}",
            )
        except discord.Forbidden:
            # The grant failed; remove the record we just created to avoid a false expiry.
            with sqlite3.connect(bot.db_path, timeout=15) as conn:
                conn.execute(
                    "DELETE FROM northstar_temporary_roles WHERE guild_id=? AND user_id=? AND role_id=?",
                    (guild.id, member.id, role.id),
                )
                conn.commit()
            return await _reply(
                interaction,
                content="Discord denied the role change. Check Manage Roles permission and bot role hierarchy.",
            )
        except Exception:
            log.exception("Could not assign or save temporary VIP role")
            return await _reply(
                interaction,
                content="Could not complete the VIP role change. Check the server logs.",
            )

        expiry_text = discord.utils.format_dt(
            discord.utils.utcnow().fromtimestamp(expires_at, tz=discord.utils.utcnow().tzinfo),
            style="F",
        )
        await _reply(
            interaction,
            embed=_embed(
                "⏳ TEMPORARY VIP ROLE",
                f"{member.mention} received {role.mention}.\n"
                f"Duration: **{days} day(s)**\nExpires: {expiry_text}",
            ),
        )

    @bot.tree.command(
        name="viptime",
        description="Admin: check when a member’s temporary VIP role expires",
    )
    @app_commands.describe(member="Member to check")
    async def viptime(interaction: discord.Interaction, member: discord.Member):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        guild = interaction.guild
        if guild is None:
            return await _reply(interaction, content="Use this command inside your Discord server.")
        try:
            with sqlite3.connect(bot.db_path, timeout=15) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT role_id, expires_at
                    FROM northstar_temporary_roles
                    WHERE guild_id=? AND user_id=?
                    ORDER BY expires_at
                    """,
                    (guild.id, member.id),
                ).fetchall()
        except sqlite3.Error:
            log.exception("Could not read temporary VIP role records")
            return await _reply(interaction, content="Could not read VIP expiry data. Check the server logs.")

        if not rows:
            return await _reply(
                interaction,
                content=f"No active temporary VIP expiry is recorded for {member.mention}.",
            )

        lines = []
        for row in rows:
            role = guild.get_role(int(row["role_id"]))
            role_name = role.mention if role else f"Deleted role ({row['role_id']})"
            expiry = discord.utils.format_dt(
                discord.utils.utcnow().fromtimestamp(
                    float(row["expires_at"]), tz=discord.utils.utcnow().tzinfo
                ),
                style="F",
            )
            lines.append(f"{role_name} — expires {expiry}")
        await _reply(
            interaction,
            embed=_embed("⏳ VIP EXPIRY", f"{member.mention}\n" + "\n".join(lines)),
        )

    @bot.tree.command(name="research", description="Research a Solana token address")
    @app_commands.describe(token="Solana mint address")
    async def research(interaction: discord.Interaction, token: str):
        await _ack(interaction, ephemeral=False)
        out = await run_research(bot, token)
        title, desc, colour = research_summary(out)
        await _reply(interaction, embed=_embed(title, desc, colour), ephemeral=False)

    @bot.tree.command(name="check", description="Quick risk and data-quality check")
    async def check(interaction: discord.Interaction, token: str):
        await _ack(interaction)
        db = await bot.adb()
        try:
            row = db.fetchone(
                "SELECT action,score,risk,confidence,report,ts FROM research_snapshots WHERE mint=? ORDER BY ts DESC LIMIT 1",
                (token.strip(),),
            )
            if not row:
                await _close(db)
                db = None
                out = await run_research(bot, token)
                title, desc, colour = research_summary(out)
                return await _reply(
                    interaction,
                    embed=_embed("QUICK CHECK • " + title, desc, colour),
                )
            report = json.loads(row["report"] or "{}")
            flags = report.get("flags") or []
            msg = (
                f"Decision: **{row['action']}** • Score: "
                f"**{row['score'] if row['score'] is not None else 'UNKNOWN'}** • "
                f"Risk: **{row['risk'] or 'UNKNOWN'}**\n"
                + "\n".join("• " + str(flag.get("message", "Risk finding")) for flag in flags[:8])
            )
            await _reply(interaction, embed=_embed("QUICK CHECK", msg))
        finally:
            if db is not None:
                await _close(db)

    @bot.tree.command(name="scan", description="Show the latest scan queue and decisions")
    async def scan(interaction: discord.Interaction):
        await _ack(interaction)
        db = await bot.adb()
        try:
            rows = db.fetchall(
                "SELECT symbol,mint,state,last_score,last_risk,updated_at "
                "FROM candidates ORDER BY updated_at DESC LIMIT 10"
            )
            msg = "\n".join(
                f"`{row['state']}` **{row['symbol'] or 'UNKNOWN'}** • "
                f"score {row['last_score'] if row['last_score'] is not None else '?'} • "
                f"risk {row['last_risk'] or '?'}\n`{row['mint']}`"
                for row in rows
            ) or "No candidates recorded yet."
            await _reply(interaction, embed=_embed("LATEST SCAN QUEUE", msg))
        finally:
            await _close(db)

    @bot.tree.command(name="calls", description="List recent Northstar callouts")
    async def calls(interaction: discord.Interaction):
        await _ack(interaction)
        db = await bot.adb()
        try:
            rows = db.fetchall(
                "SELECT symbol,mint,status,entry_mcap,current_mcap,created_at "
                "FROM alerts ORDER BY created_at DESC LIMIT 10"
            )
            msg = "\n".join(
                f"**{row['symbol'] or 'UNKNOWN'}** — {row['status']} — "
                f"entry MC ${row['entry_mcap'] or 'UNKNOWN'} — "
                f"current MC ${row['current_mcap'] or 'UNKNOWN'}\n`{row['mint']}`"
                for row in rows
            ) or "No callouts recorded yet."
            await _reply(interaction, embed=_embed("RECENT CALLOUTS", msg))
        finally:
            await _close(db)

    @bot.tree.command(name="performance", description="Show measured callout performance")
    async def performance(interaction: discord.Interaction):
        from ..performance import performance_report, format_report

        await _ack(interaction)
        db = await bot.adb()
        try:
            report = performance_report(db)
            await _reply(
                interaction,
                embed=_embed("PERFORMANCE", format_report(report)[:4000]),
            )
        finally:
            await _close(db)

    @bot.tree.command(
        name="paper",
        description="Paper trading: the calls account and the learning brain's own account",
    )
    async def paper(interaction: discord.Interaction):
        from .. import paper as PT

        await _ack(interaction)
        db = await bot.adb()
        try:
            accounts = [PT.summary(db, bot.settings, account=account) for account in PT.ACCOUNTS]
        finally:
            await _close(db)

        def usd(value):
            return "—" if value is None else ("-" if value < 0 else "") + f"${abs(value):,.2f}"

        lines = []
        for data, label in zip(accounts, ("📣 **Bot's calls**", "🧠 **Brain's own picks**")):
            run = data["run"]
            lines += [
                label,
                f"Balance {usd(data['equity'])} (started {usd(run['start_usd'])}) · "
                f"{'+' if data['pnl'] >= 0 else ''}{usd(data['pnl'])} ({data['pnl_pct']:+.1f}%)",
                f"{data['open']} open · {data['closed']} closed ({data['wins']} in profit)",
            ]
            recent = [trade for trade in data["trades"] if trade["status"] != "SKIPPED"][:4]
            lines += [
                f"• ${trade['symbol'] or trade['mint'][:6]}: "
                + (
                    f"{'+' if trade['pnl'] >= 0 else ''}{usd(trade['pnl'])} ({trade['pnl_pct']:+.0f}%)"
                    if trade["pnl"] is not None
                    else "—"
                )
                + (f" · {trade['exit']}" if trade["status"] == "CLOSED" else " · open")
                for trade in recent
            ]
            lines.append("")
        lines.append(
            "Simulated, no real trades. Change the amounts on the dashboard's Paper tab."
        )
        await _reply(
            interaction,
            embed=_embed("PAPER TRADING", "\n".join(lines)[:4000]),
        )

    @bot.tree.command(name="why", description="Explain the latest saved decision for a token")
    async def why(interaction: discord.Interaction, token: str):
        await _ack(interaction)
        db = await bot.adb()
        try:
            row = db.fetchone(
                "SELECT action,score,risk,confidence,report,ts FROM research_snapshots "
                "WHERE mint=? ORDER BY ts DESC LIMIT 1",
                (token.strip(),),
            )
            if not row:
                msg = "No saved decision found. Run `/research` first."
            else:
                report = json.loads(row["report"] or "{}")
                reasons = report.get("reasons") or (report.get("decision") or {}).get("reasons") or []
                flags = report.get("flags") or []
                msg = (
                    f"Decision: **{row['action']}** • score "
                    f"**{row['score'] if row['score'] is not None else 'UNKNOWN'}** • "
                    f"risk **{row['risk'] or 'UNKNOWN'}**\n\n**Reasons**\n"
                    + "\n".join("• " + str(reason) for reason in reasons[:8])
                    + "\n\n**Flags**\n"
                    + "\n".join("• " + str(flag.get("message")) for flag in flags[:6])
                )
            await _reply(interaction, embed=_embed("WHY THIS DECISION?", msg))
        finally:
            await _close(db)

    @bot.tree.command(name="watchlist", description="Show your saved watchlist")
    async def watchlist(interaction: discord.Interaction):
        await _ack(interaction)
        db = await bot.adb()
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS user_watchlist "
                "(user_id TEXT NOT NULL,mint TEXT NOT NULL,created_at REAL NOT NULL,"
                "PRIMARY KEY(user_id,mint))"
            )
            rows = db.fetchall(
                "SELECT mint,created_at FROM user_watchlist WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT 20",
                (str(interaction.user.id),),
            )
            msg = "\n".join("• `" + row["mint"] + "`" for row in rows) or (
                "Your watchlist is empty. Use `/watch_add` to add a mint."
            )
            await _reply(interaction, embed=_embed("YOUR WATCHLIST", msg))
        finally:
            await _close(db)

    @bot.tree.command(name="watch_add", description="Add a token mint to your personal watchlist")
    async def watch_add(interaction: discord.Interaction, token: str):
        await _ack(interaction)
        db = await bot.adb()
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS user_watchlist "
                "(user_id TEXT NOT NULL,mint TEXT NOT NULL,created_at REAL NOT NULL,"
                "PRIMARY KEY(user_id,mint))"
            )
            db.execute(
                "INSERT OR IGNORE INTO user_watchlist(user_id,mint,created_at) VALUES(?,?,?)",
                (str(interaction.user.id), token.strip(), time.time()),
            )
            await _reply(interaction, content="Added to your watchlist: `" + token.strip() + "`")
        finally:
            await _close(db)

    @bot.tree.command(name="compare", description="Compare the latest saved research for two tokens")
    async def compare(interaction: discord.Interaction, token1: str, token2: str):
        await _ack(interaction)
        db = await bot.adb()
        try:
            lines = []
            for mint in (token1.strip(), token2.strip()):
                row = db.fetchone(
                    "SELECT action,score,risk,confidence FROM research_snapshots "
                    "WHERE mint=? ORDER BY ts DESC LIMIT 1",
                    (mint,),
                )
                lines.append(
                    f"`{mint}`\nDecision: {row['action'] if row else 'NO DATA'} • "
                    f"Score: {row['score'] if row and row['score'] is not None else 'UNKNOWN'} • "
                    f"Risk: {row['risk'] if row else 'UNKNOWN'}"
                )
            await _reply(
                interaction,
                embed=_embed("TOKEN COMPARISON", "\n\n".join(lines)),
            )
        finally:
            await _close(db)

    @bot.tree.command(name="deepresearch", description="Run full research now for a Solana token")
    async def deepresearch(interaction: discord.Interaction, token: str):
        await research.callback(interaction, token)

    @bot.tree.command(name="northstar_status", description="Admin: inspect bot and provider status")
    async def northstar_status(interaction: discord.Interaction):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        await _ack(interaction)
        db = await bot.adb()
        try:
            health = db.provider_health()
            rows = "\n".join(
                f"• **{item['provider']}**: {item['status']} (errors {item['err_count']})"
                for item in health
            ) or "No provider health recorded yet."
            await _reply(
                interaction,
                embed=_embed(
                    "NORTHSTAR STATUS",
                    f"Commands: ONLINE\nEngine pause flag: {bot.paused}\n\n{rows}",
                ),
            )
        finally:
            await _close(db)

    @bot.tree.command(
        name="northstar_diagnostics",
        description="Admin: show recent candidates and provider errors",
    )
    async def northstar_diagnostics(interaction: discord.Interaction):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        await _ack(interaction)
        db = await bot.adb()
        try:
            rows = db.provider_health()
            msg = "\n".join(
                f"**{row['provider']}** — {row['status']} — "
                f"{row['last_error'] or 'no recent error'}"
                for row in rows
            ) or "No provider diagnostics available."
            await _reply(interaction, embed=_embed("DIAGNOSTICS", msg))
        finally:
            await _close(db)

    @bot.tree.command(
        name="northstar_settings",
        description="Admin: view current scan thresholds and controls",
    )
    async def northstar_settings(interaction: discord.Interaction):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        settings = bot.settings
        msg = (
            f"Minimum liquidity: ${settings.min_liquidity_usd:,.0f}\n"
            f"Minimum liquidity/MC: {settings.min_liquidity_to_mcap:.0%}\n"
            f"Max top-10 adjusted: {settings.max_top10_adjusted_pct:.1f}%\n"
            f"Max creator holding: {settings.max_creator_holding_pct:.1f}%\n"
            f"Dry run: {settings.dry_run}\n\n"
            "This command is view-only; edit environment settings and restart to change thresholds."
        )
        await _reply(interaction, embed=_embed("CURRENT SETTINGS", msg))

    @bot.tree.command(name="learning", description="What Northstar has learned from its own results")
    async def learning_cmd(interaction: discord.Interaction):
        await _ack(interaction)
        from .. import learning as learning_module
        import copy

        db = await bot.adb()
        try:
            learning_module.init_schema(db)
            settings = copy.copy(bot.settings)
            learning_module.apply_learned(db, settings)
            info = await asyncio.to_thread(
                learning_module.summary, db, settings, settings.learning_days
            )
        finally:
            await _close(db)

        def pct_s(value):
            return "n/a" if value is None else f"{value * 100:+.1f}%"

        lines = [
            f"**Coins scored on their real price path:** {info['evaluated']} (waiting: {info['pending']})",
            f"**Standard calls (simulated):** {info['standard_trades']} trades · avg {pct_s(info['standard_avg'])} each",
            f"**Rug scalps (simulated):** {info['scalp_trades']} trades · avg {pct_s(info['scalp_avg'])} each",
            f"**Median peak after a decision:** {info['median_peak'] or 'n/a'}x",
            "",
        ]
        if info["learned"]:
            lines.append("**Learned settings (override .env):**")
            lines.extend(
                f"• `{key}` = {value:g}" for key, value in sorted(info["learned"].items())
            )
        else:
            lines.append("No learned changes yet — settings are your .env values.")
        lines.append(f"\n**🧠 Brain:** {info['brain']}")
        if info.get("brain_signals"):
            lines.append(
                "**What predicts a runner:** "
                + ", ".join(
                    f"{'▲' if correlation > 0 else '▼'} {name}"
                    for name, correlation in info["brain_signals"]
                )
            )
        recent = [row for row in info["recent_changes"] if row["key"] != "RESET"][:5]
        if recent:
            lines.append("\n**Recent changes:**")
            lines.extend(
                f"• <t:{int(row['ts'])}:R> `{row['key']}` {row['old']:g} → {row['new']:g}"
                for row in recent
            )
        lines.append(
            "\nSimulated = the trade plan replayed on 1-minute candles with fees and worst-case fills. "
            "Not a promise of future results."
        )
        await _reply(
            interaction,
            embed=_embed("🧠 NORTHSTAR LEARNING", "\n".join(lines)),
        )

    @bot.tree.command(
        name="learning_reset",
        description="Admin: forget learned settings (back to .env values)",
    )
    async def learning_reset(interaction: discord.Interaction):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        await _ack(interaction)
        from .. import learning as learning_module

        db = await bot.adb()
        try:
            learning_module.init_schema(db)
            keys = learning_module.reset(db, bot.settings, bot.settings)
        finally:
            await _close(db)
        message = (
            "Forgot: " + ", ".join(f"`{key}`" for key in keys)
            if keys
            else "Nothing had been learned yet."
        )
        message += "\nThe engine switches back to your .env values within a minute. Learning keeps running."
        await _reply(interaction, embed=_embed("🧠 LEARNING RESET", message))

    @bot.tree.command(
        name="untrack",
        description="Admin: stop tracking a coin (watch list, research, call and early-watch updates)",
    )
    @app_commands.describe(token="Solana mint address", reason="Why (optional, kept with the coin)")
    async def untrack(interaction: discord.Interaction, token: str, reason: str = ""):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        await _ack(interaction)
        from ..analysis.identity import is_valid_mint_format

        token = token.strip()
        if not is_valid_mint_format(token):
            return await _reply(interaction, content="That is not a Solana mint address.")
        db = await bot.adb()
        try:
            result = db.untrack(token, reason[:200], f"discord:{interaction.user}")
        finally:
            await _close(db)
        if not (
            result["candidate"]
            or result["calls_closed"]
            or result["early_watches_ended"]
            or result["early_calls_closed"]
        ):
            return await _reply(interaction, content="Northstar was not tracking that coin.")
        parts = ["No longer researched, watched or brought back by the radar."]
        if result["calls_closed"]:
            parts.append(f"Call closed ({result['calls_closed']}).")
        if result["early_watches_ended"] or result["early_calls_closed"]:
            parts.append("Early-watch updates stopped.")
        parts.append("Its price is still recorded for learning. Undo with /retrack.")
        await _reply(
            interaction,
            embed=_embed("🛑 UNTRACKED", f"`{token}`\n" + " ".join(parts)),
        )

    @bot.tree.command(name="retrack", description="Admin: start tracking a coin you untracked again")
    async def retrack(interaction: discord.Interaction, token: str):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        await _ack(interaction)
        db = await bot.adb()
        try:
            ok = db.retrack(token.strip())
        finally:
            await _close(db)
        await _reply(
            interaction,
            content="Back in the research queue." if ok else "That coin was not untracked.",
        )

    @bot.tree.command(
        name="northstar_scan",
        description="Admin: show scan queue (does not force a new provider scan)",
    )
    async def northstar_scan(interaction: discord.Interaction):
        if not bot.admin_only(interaction):
            return await _reply(interaction, content="Administrator permission required.")
        await _ack(interaction)
        db = await bot.adb()
        try:
            rows = db.fetchall(
                "SELECT symbol,mint,state,last_score,last_risk FROM candidates "
                "ORDER BY updated_at DESC LIMIT 15"
            )
            msg = "\n".join(
                f"{row['state']} | {row['symbol'] or '?'} | score {row['last_score']} | "
                f"risk {row['last_risk']} | `{row['mint']}`"
                for row in rows
            ) or "Queue is empty."
            await _reply(interaction, embed=_embed("ADMIN SCAN QUEUE", msg))
        finally:
            await _close(db)


def start_commands_thread(settings, db_path: str) -> threading.Thread:
    """Run slash commands on their own thread and event loop."""
    thread = threading.Thread(
        target=lambda: asyncio.run(run_commands(settings, db_path)),
        name="northstar-slash-commands",
        daemon=True,
    )
    thread.start()
    return thread


async def run_commands(settings, db_path: str):
    if not settings.discord_token:
        log.warning(
            "Discord slash commands disabled: NORTHSTAR_DISCORD_TOKEN is not set"
        )
        return

    bot = NorthstarCommands(settings, db_path)
    install_commands(bot)
    try:
        await bot.start(settings.discord_token.get_secret_value())
    except Exception:
        # Slash commands are optional; never let the callout engine go down.
        log.exception("Discord slash-command client stopped; callouts continue without commands")
    finally:
        if not bot.is_closed():
            await bot.close()
