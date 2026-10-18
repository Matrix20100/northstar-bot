"""Wispbyte-friendly entry point. Defaults to the continuous bot when run as main.py."""
import sys
from northstar.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("run")
    raise SystemExit(main())
