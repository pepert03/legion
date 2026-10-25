"""Keep legion itself up to date: if it was installed from a git URL, reinstall when that branch moves."""

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

BRANCH = "main"


def installed() -> tuple[str, str] | None:
    """(url, commit) this copy of legion was installed from, or None (a working copy, or not from git)."""
    try:
        dist = metadata.distribution("legion")
        info = json.loads(dist.read_text("direct_url.json") or "null")
    except (metadata.PackageNotFoundError, ValueError):
        return None
    if not info or "vcs_info" not in info or info.get("dir_info", {}).get("editable"):
        return None
    # The running code must be that installation, not a checkout that shadows it on PYTHONPATH.
    here = Path(__file__).resolve().parent
    if Path(dist.locate_file("legion")).resolve() != here:
        return None
    return info["url"], info["vcs_info"].get("commit_id", "")


def latest(url: str) -> str | None:
    p = subprocess.run(["git", "ls-remote", url, f"refs/heads/{BRANCH}"], capture_output=True, text=True,
                       timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})  # never ask for a password
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.strip() else None


def check(quiet: bool = False) -> bool:
    """Update legion if there is a newer version. True if it updated (the new code runs from the next command)."""
    if os.environ.get("LEGION_NO_UPDATE"):
        return False
    src = installed()
    if not src:
        return False
    url, have = src
    try:
        new = latest(url)
    except (OSError, subprocess.SubprocessError):
        new = None
    if not new or new == have:
        return False
    print(f"legion: updating legion ({have[:8]} → {new[:8]}) from {url}…", file=sys.stderr, flush=True)
    p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "--no-deps",
                        f"git+{url}@{new}"], capture_output=True, text=True, timeout=600)
    if p.returncode:
        print(f"legion: couldn't update ({p.stderr.strip()[-300:]}); update by hand with:\n"
              f"  {sys.executable} -m pip install --upgrade git+{url}", file=sys.stderr)
        return False
    if not quiet:
        print("legion: updated; the new version runs from your next command.", file=sys.stderr)
    return True
