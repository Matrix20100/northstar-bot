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

## Before deploying: back up

1. Stop the server on Wispbyte. If it's running, recent writes may still sit in the `-wal` file.
2. Files → `data` → download `northstar.db`, and `northstar.db-wal` and `northstar.db-shm` if they
   exist. Keep them together, unchanged, until v2.5.4 has run cleanly for a day.
3. Start the server again.

The first start on v2.5.4 adds three columns (`discovery_events.seen`, `discovery_events.last_ts`,
`decision_tracking.last_polled_at`) and runs the first retention pass in the background. On a
copy of the 21 Oct database (394 MB) that pass took about 20 s in short batches: other writes
waited at most 0.18 s and the WAL file peaked at 24 MB. A restart part-way is safe, because the
next pass continues where it stopped.

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

## Recovery

- **The bot misbehaves after the update.** Point `main` back to v2.5.3 (`cb0d49c`) and restart.
  v2.5.3 runs on a v2.5.4 database: it ignores the three new columns and the merged discovery
  rows.
- **The database is damaged** (the log shows `quick_check` failing or a quarantined file).
  1. Stop the server.
  2. Upload the backup `northstar.db` to `data/`.
  3. Delete any `northstar.db-wal` and `northstar.db-shm` there, then upload the backup's own
     `-wal` and `-shm` if you have them.
  4. Start the server.

  Decisions made after the backup are lost; callouts already posted in Discord are not affected.
- **A VACUUM was interrupted.** SQLite leaves the original file as it was. Restart normally.
