# Windows setup

Tested layout: Windows 10/11, PowerShell, Python 3.11 or newer.

## 1. Install Python

1. Download Python 3.11+ from python.org and run the installer.
2. On the first screen, **tick "Add python.exe to PATH"**.
3. Open a new PowerShell window and check: `python --version` should show 3.11 or higher.

## 2. Unpack and install Northstar

```powershell
cd $HOME\Documents
Expand-Archive .\northstar-alpha-engine.zip -DestinationPath .
cd .\northstar-alpha-engine
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `Activate.ps1` is blocked by execution policy, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## 3. Configure

```powershell
copy .env.example .env
notepad .env
```

Fill in at least:
- `NORTHSTAR_HELIUS_API_KEY`: free key from helius.dev (strongly recommended)
- Keep `NORTHSTAR_DRY_RUN=true` for now

## 4. Check and try

```powershell
python -m northstar health
python -m northstar analyze <SOME_SOLANA_MINT>
python -m pytest -q
```

## 5. Run

```powershell
python -m northstar run
```

Stop it with Ctrl+C; it shuts down cleanly. Logs are JSON lines. To save them to a file:
`python -m northstar run *>> data\northstar.log`

When you're satisfied with the dry-run decisions, set up the Discord bot ([DISCORD_BOT_SETUP.md](DISCORD_BOT_SETUP.md)), set `NORTHSTAR_DRY_RUN=false`, and run `python -m northstar test-discord` once.

For 24/7 running (auto-restart, start at logon), see [RUNNING_CONTINUOUSLY.md](RUNNING_CONTINUOUSLY.md).
