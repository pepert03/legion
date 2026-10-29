"""Keep legion itself up to date: if it was installed from a git URL, reinstall when that branch moves.

Installed from GitHub, it also works without git's https transport: several agents' machines lacked it (`git:
'remote-https' is not a git command`), `update` then told them they were up to date, and they installed GitHub's
zip by hand, which `update` took for a working copy. Now it asks GitHub's API for the latest commit, installs the
zip when `pip install git+…` fails, recognizes a zip install, and says so when it can't tell."""

import json
import os
import re
import subprocess
import sys
import urllib.request
from importlib import metadata
from pathlib import Path

BRANCH = "main"
GITHUB = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)")
ARCHIVE_SHA = re.compile(r"/archive/([0-9a-f]{40})\.(?:zip|tar\.gz)$")


def installed() -> tuple[str, str] | None:
    """(url, commit) this copy of legion was installed from, or None (a working copy, or not from git). From a
    GitHub zip, the url is the repository's and the commit is the zip's, or "" when it was a branch's zip."""
    try:
        dist = metadata.distribution("legion")
        info = json.loads(dist.read_text("direct_url.json") or "null")
    except (metadata.PackageNotFoundError, ValueError):
        return None
    if not info or info.get("dir_info", {}).get("editable"):
        return None
    # The running code must be that installation, not a checkout that shadows it on PYTHONPATH.
    here = Path(__file__).resolve().parent
    if Path(dist.locate_file("legion")).resolve() != here:
        return None
    if "vcs_info" in info:
        return info["url"], info["vcs_info"].get("commit_id", "")
    m = GITHUB.match(info.get("url", ""))
    if "archive_info" in info and m:
        sha = ARCHIVE_SHA.search(info["url"])
        return f"https://github.com/{m.group(1)}/{m.group(2)}", sha.group(1) if sha else ""
    return None


def latest(url: str) -> str | None:
    """The branch's latest commit: from git, or for GitHub, from its API when git can't reach it."""
    try:
        p = subprocess.run(["git", "ls-remote", url, f"refs/heads/{BRANCH}"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})  # never ask for a password
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    m = GITHUB.match(url)
    if not m:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/commits/{BRANCH}",
                                 headers={"Accept": "application/vnd.github.sha", "User-Agent": "legion"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sha = r.read().decode().strip()
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
    except OSError:
        return None


def _pip(target: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--force-reinstall", "--no-deps", target],
                          capture_output=True, text=True, timeout=600)


def check(quiet: bool = False) -> bool | None:
    """Update legion if there is a newer version. True if it updated (the new code runs from the next command),
    False if it's up to date (or not installed from git), None if it couldn't tell or couldn't update."""
    if os.environ.get("LEGION_NO_UPDATE"):
        return False
    src = installed()
    if not src:
        return False
    url, have = src
    new = latest(url)
    if not new:
        if not quiet:
            print(f"legion: couldn't reach {url} to look for a newer version", file=sys.stderr)
        return None
    if new == have:
        return False
    print(f"legion: updating legion ({have[:8] or '?'} → {new[:8]}) from {url}…", file=sys.stderr, flush=True)
    p = _pip(f"git+{url}@{new}")
    m = GITHUB.match(url)
    if p.returncode and m:  # no git transport here: GitHub's zip of that commit
        p = _pip(f"https://github.com/{m.group(1)}/{m.group(2)}/archive/{new}.zip")
    if p.returncode:
        print(f"legion: couldn't update ({p.stderr.strip()[-300:]}); update by hand with:\n"
              f"  {sys.executable} -m pip install --force-reinstall --no-deps git+{url}", file=sys.stderr)
        return None
    if not quiet:
        print("legion: updated; the new version runs from your next command.", file=sys.stderr)
    return True
