# Database size and retention (v2.5.4)

Northstar keeps its database bounded by itself. Each hour, and once right after start, it runs
a retention pass (`northstar/retention.py`):

- **Discovery events.** Repeats of the same event are merged into one row with a `seen`
  counter. The first run merges the existing repeats, in short batches.
- **Bonding-curve events.** Older than `NORTHSTAR_CURVE_RETENTION_HOURS` (6) they are deleted.
  Research only reads the last 5 minutes.
- **Old research and discovery rows.** The existing rules still apply:
  `NORTHSTAR_DISCOVERY_RETENTION_DAYS` (4) and `NORTHSTAR_SNAPSHOT_RETENTION_DAYS` (7).
- **Prefilter-rejection reports.** Older than `NORTHSTAR_SNAPSHOT_COMPACT_HOURS` (24), their
  provider metadata is dropped. The reasons, the decision and every market number are kept.

The pass never deletes these:
- callouts (`alerts`, `price_history`) and early calls;
- decision tracking and outcomes (the runner/rug labels);
- learning shadows, shadow tests and anything else the brain trains on;
- the evidence `python main.py audit` reads.

Deleted rows free space *inside* the file, and SQLite reuses it, so the file stops growing.
The file itself only gets smaller after a VACUUM.

## Commands

Run these from the bot folder, where `main.py` is.

| What | Command | Changes data? |
|---|---|---|
| Space per table, and what the pass would remove | `python main.py db` | **No.** This is the default dry run. |
| Run the retention pass now | `python main.py db --apply` | Yes. Safe while the bot runs. |
| Pass, then shrink the file | `python main.py db --apply --vacuum` | Yes. **The bot must be stopped.** |

`--vacuum` refuses to run:
- while the bot holds its lock (`data/northstar.lock`);
- when free disk space is under about 1.1 times the database size, because VACUUM writes a
  temporary copy.

If VACUUM fails part-way, SQLite keeps the original file intact. `--vacuum` without `--apply`
is ignored, and the command stays a dry run.

### Shrinking the file on Wispbyte

The panel console can only run commands while the server is running, and VACUUM needs it
stopped. Shrinking is optional, because the file stops growing either way. If you want it:

1. Stop the server.
2. Download `data/northstar.db`, and `northstar.db-wal` if it exists. Keep this download as a
   backup and don't touch it.
3. On your PC, in a Northstar folder whose `data` folder holds a copy of that file, run
   `python main.py db --apply --vacuum`.
4. Upload the smaller `northstar.db` back to `data/`, delete any old `northstar.db-wal` and
   `northstar.db-shm` there, and start the server.

## Price tracking

Every rejected, watched or called coin is price-checked for 24 hours (`decision_tracking`),
which is how runner/rug outcomes are learned. Each outcome pass, every
`NORTHSTAR_OUTCOME_INTERVAL_SECONDS` (120), checks up to `NORTHSTAR_TRACKING_POLL_LIMIT` (600;
allowed range 1–1200) open decisions, in this order:

1. called-out coins, on every pass;
2. decisions never checked yet;
3. everything else, least recently checked first.

The 21 Oct backup had 2,100–3,300 open decisions. That means each one is checked 120–180 times
a day, against the 30 checks a complete label needs. One pass costs at most 20 DexScreener
requests (30 coins each) on the 200 rpm pairs limiter, or 40 at the 1,200 cap. When there are
too many open decisions to reach 60 checks a day, the log warns once an hour and names the
setting to raise.
