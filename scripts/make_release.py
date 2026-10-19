"""Build a complete, versioned release folder from the committed code.

    python scripts/make_release.py [--out "C:/Users/you/Downloads/Northstar Versions"]

Creates  <out>/Northstar v<VERSION> (<date>)/  with the FULL project (every file in git at HEAD),
so each release contains all earlier fixes and features. Refuses to run with uncommitted changes
(a release must be exactly what is in git) or if that version folder already exists.
Your .env, data/ and database are never included.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "Northstar Versions"))
    args = ap.parse_args()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("Uncommitted changes — commit first so the release is exactly what is in git:\n" + dirty)
        return 1
    version = (ROOT / "VERSION").read_text().strip()
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True).stdout.strip()
    dest = Path(args.out) / f"Northstar v{version} ({time.strftime('%Y-%m-%d')})"
    if dest.exists():
        print(f"{dest} already exists — bump VERSION for a new release")
        return 1
    tar = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True, check=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    (dest / "RELEASE.txt").write_text(f"Northstar v{version}\ncommit {commit}\nbuilt {time.strftime('%Y-%m-%d %H:%M')}\n"
                                      "Complete project: includes every earlier fix and feature.\n"
                                      "Copy your own .env and data/ next to main.py; they are never shipped.\n")
    print(f"release written to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
