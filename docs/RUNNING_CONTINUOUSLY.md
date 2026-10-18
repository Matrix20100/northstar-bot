# Running continuously

Northstar is safe to restart at any time. On start it:
- refuses to run if another instance already holds `data/northstar.lock`, which prevents duplicate workers;
- re-queues candidates interrupted mid-research;
- checks Discord history before re-sending any alert that was pending when it stopped, so it never double-posts;
- resumes monitoring every open callout from the database.

## Windows: auto-restart script + Task Scheduler

`scripts\run_northstar.bat` restarts the engine 30 seconds after any exit and appends logs to `data\northstar.log`.

To start it automatically at logon:
1. Open **Task Scheduler** and choose **Create Task…**
2. General tab: name it "Northstar", and select **Run only when user is logged on** (simplest).
3. Triggers tab: **New → At log on**.
4. Actions tab: **New → Start a program**. Program: the full path to `scripts\run_northstar.bat`. "Start in": the project folder.
5. Settings tab: untick "Stop the task if it runs longer than…".

Keep the PC from sleeping (**Settings → System → Power → Sleep: Never**), or run it on an always-on machine.

## Linux / VPS: systemd

Edit the paths in `scripts/northstar.service`, then:

```bash
sudo cp scripts/northstar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now northstar
journalctl -u northstar -f
```

## Day-to-day

- `python -m northstar candidates` shows what it decided and why.
- `python -m northstar health` shows provider status and how much of each daily budget is used.
- `python -m northstar report` shows performance of all decisions. Run it weekly. Do not change thresholds based on fewer than ~30 decisions per group; the report warns when samples are too small.
- Back up `data/northstar.db` occasionally. If it is ever corrupted, Northstar moves it aside as `northstar.db.corrupt-<time>` and starts fresh.
