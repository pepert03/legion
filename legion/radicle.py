"""Radicle as legion's transport.

Every node pushes its own legion refs (refs/legion/pub/*, refs/legion/patches/*) to Radicle, which keeps
each node's refs in its own namespace and replicates them through seeds, so nodes behind NAT or on
different networks reach each other without opening ports. To sync, legion reads every namespace from
the local Radicle storage and merges the coord branches, as in plain p2p.

Radicle only ships for Linux and macOS. On Windows it runs inside WSL, and legion calls it through
`wsl.exe`; the repository and the agents stay on Windows.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path, PureWindowsPath

IS_WINDOWS = os.name == "nt"
DEFAULT_DISTRO = "Ubuntu-24.04"
NETWORK_SYNC_EVERY = 15  # seconds between `rad sync --fetch` calls; pushes announce on their own
# Runs a command with Radicle on the PATH and, if given, a specific RAD_HOME (several nodes per machine).
SHIM = ('rh="$1"; shift; export PATH="$HOME/.radicle/bin:$PATH" RAD_PASSPHRASE=""; '
        '[ -n "$rh" ] && export RAD_HOME="$rh"; exec "$@"')


class RadError(RuntimeError):
    pass


def _cfg(repo, key: str) -> str | None:
    p = subprocess.run(["git", "config", "--get", key], cwd=repo, capture_output=True, text=True)
    if p.returncode:
        return None
    return p.stdout.strip() or None


def distro(repo=None) -> str:
    return os.environ.get("LEGION_WSL_DISTRO") or (repo and _cfg(repo, "legion.wsl")) or DEFAULT_DISTRO


def host_path(path) -> str:
    """The path as Radicle's side sees it: /mnt/c/... under WSL, unchanged elsewhere."""
    if not IS_WINDOWS:
        return str(path)
    p = PureWindowsPath(Path(path).resolve())
    return f"/mnt/{p.drive[0].lower()}" + "/" + "/".join(p.parts[1:])


def run(*args: str, repo=None, home: str | None = None, timeout: float = 60, check: bool = True,
        cwd=None) -> subprocess.CompletedProcess:
    """Run a command where Radicle lives (WSL on Windows), with Radicle on the PATH."""
    home = home if home is not None else (repo and _cfg(repo, "legion.radhome")) or ""
    if home and not home.startswith(("/", "~")):
        # e.g. Git Bash turns /home/x into C:/Program Files/Git/home/x before Python sees it.
        raise RadError(f"the Radicle home must be a path on Radicle's side, like /home/you/.radicle; got {home!r}"
                       " (in Git Bash, set MSYS_NO_PATHCONV=1)")
    cmd = ["sh", "-c", SHIM, "sh", home, *args]
    if IS_WINDOWS:
        cmd = ["wsl.exe", "-d", distro(repo), *(["--cd", host_path(cwd)] if cwd else []), "-e", *cmd]
        cwd = None
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RadError(f"`{' '.join(args[:3])}` timed out after {timeout:.0f}s")
    if check and p.returncode:
        raise RadError(f"`{' '.join(args[:3])}` failed: {clean(p.stderr or p.stdout).strip()[-600:]}")
    return p


def clean(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text or "")


def rad(repo, *args: str, **kw) -> str:
    return clean(run("rad", *args, repo=repo, **kw).stdout).strip()


def rgit(repo, *args: str, **kw) -> subprocess.CompletedProcess:
    """git with Radicle's remote helper available, on this repository."""
    return run("git", "-C", host_path(repo), *args, repo=repo, **kw)


# --- setup -----------------------------------------------------------------------------------


WSL_CONF = "[user]\ndefault=legion\n[interop]\nappendWindowsPath=false\n"


def prepare_wsl() -> None:
    """Windows only: make sure the WSL distro exists and has an ordinary default user, without any prompt."""
    d = distro()
    listed = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True).stdout.decode("utf-16-le", "replace")
    if d not in listed.split():
        p = subprocess.run(["wsl.exe", "--install", "-d", d, "--no-launch"], capture_output=True, timeout=1800)
        out = p.stdout.decode("utf-16-le", "replace")
        if p.returncode:
            raise RadError(f"`wsl --install -d {d}` failed (it may need an administrator, or a reboot "
                           f"after enabling WSL): {out.strip()[-300:]}")
    uid = subprocess.run(["wsl.exe", "-d", d, "-e", "id", "-u"], capture_output=True, text=True, timeout=300)
    if uid.returncode:
        raise RadError(f"WSL distro {d} doesn't start: {uid.stderr.strip()[-300:]} (if WSL was just enabled, reboot)")
    if uid.stdout.strip() == "0":  # fresh distro: everything would run as root; give it a plain user instead
        script = f"id legion >/dev/null 2>&1 || useradd -m -s /bin/bash legion; printf '{WSL_CONF}' > /etc/wsl.conf"
        subprocess.run(["wsl.exe", "-d", d, "-u", "root", "-e", "sh", "-c", script], check=True,
                       capture_output=True, timeout=120)
        subprocess.run(["wsl.exe", "--terminate", d], capture_output=True, timeout=60)


def available(home: str | None = None) -> bool:
    try:
        return run("rad", "--version", home=home, check=False, timeout=30).returncode == 0
    except (RadError, FileNotFoundError):
        return False


def install() -> str:
    """Radicle's official installer (it checks the release signature). Needs curl, tar, xz and ssh-keygen."""
    p = run("sh", "-c", "curl -sSf https://radicle.dev/install | sh -s -- --no-modify-path", home="", timeout=300)
    return clean(p.stdout).strip().splitlines()[-1] if p.stdout else "installed"


def ensure_identity(alias: str, home: str | None = None) -> str:
    """Create the node's key if missing (no passphrase, so nothing ever prompts). Returns the NID."""
    if run("rad", "self", "--did", home=home, check=False, timeout=30).returncode:
        run("rad", "auth", "--alias", alias, home=home, timeout=60)
    return node_id(home=home)


def node_id(repo=None, home: str | None = None) -> str:
    out = clean(run("rad", "node", "status", "--only", "nid", repo=repo, home=home, check=False, timeout=30).stdout)
    nid = next((w for w in out.split() if w.startswith("z6Mk")), None)
    if not nid:  # older releases
        out = clean(run("rad", "self", "--nid", repo=repo, home=home, check=False, timeout=30).stdout)
        nid = next((w for w in out.split() if w.startswith("z6Mk")), None)
    if not nid:
        raise RadError("couldn't read this node's id; is Radicle set up? (`legion radicle setup`)")
    return nid


def node_running(repo=None, home: str | None = None) -> bool:
    out = clean(run("rad", "node", "status", repo=repo, home=home, check=False, timeout=30).stdout)
    return "Node is running" in out


def ensure_node(repo=None, home: str | None = None) -> None:
    if node_running(repo, home):
        return
    p = run("rad", "node", "start", repo=repo, home=home, timeout=60, check=False)
    if p.returncode and not node_running(repo, home):  # a start that "fails" because it's already up is fine
        raise RadError(f"the Radicle node didn't start: {clean(p.stderr or p.stdout).strip()[-400:]}")


def configure_node(home: str, listen: str | None = None, network: str | None = None,
                   connect: list | None = None) -> None:
    """Adjust a node's config (used by the simulation to run several isolated nodes on one machine)."""
    raw = run("cat", f"{home}/config.json", home=home, timeout=30).stdout
    c = json.loads(raw)
    if listen:
        c["node"]["listen"] = [listen]
    if network:
        c["node"]["network"] = network
        if network == "test":
            c["node"]["peers"] = {"type": "static"}  # no bootstrap to the public network
    if connect is not None:
        c["node"]["connect"] = connect
    p = subprocess.run((["wsl.exe", "-d", distro(), "-e"] if IS_WINDOWS else []) +
                       ["sh", "-c", f"cat > '{home}/config.json'"], input=json.dumps(c, indent=2),
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RadError(f"couldn't write the node config: {p.stderr.strip()}")


# --- the repository ----------------------------------------------------------------------------


def rid(repo) -> str | None:
    return _cfg(repo, "legion.rid")


def enable(repo, rid_: str, home: str | None = None) -> None:
    """Mark the repository as using Radicle as its transport."""
    # longpaths: peers' refs carry their 48-character node ID, which pushes deep paths past Windows' limit.
    for key, value in (("legion.transport", "radicle"), ("legion.rid", rid_), ("core.filemode", "false"),
                       ("core.autocrlf", "false"), ("core.longpaths", "true")):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True, capture_output=True)
    if home:
        subprocess.run(["git", "config", "legion.radhome", home], cwd=repo, check=True, capture_output=True)
    if IS_WINDOWS:
        subprocess.run(["git", "config", "legion.wsl", distro(repo)], cwd=repo, check=True, capture_output=True)


def init_repo(repo, name: str, description: str) -> str:
    """`rad init` on an existing repository (public: Radicle's public seeds will carry it). Returns the RID."""
    run("git", "config", "--global", "--add", "safe.directory", "*", repo=repo, timeout=30)
    rad(repo, "init", host_path(repo), "--name", name, "--description", description,
        "--default-branch", "main", "--public", "--no-confirm", timeout=120)
    url = rgit(repo, "remote", "get-url", "rad").stdout.strip()
    m = re.match(r"rad://(z[1-9A-HJ-NP-Za-km-z]+)", url)
    if not m:
        raise RadError(f"rad init didn't leave a rad remote (got {url!r})")
    return "rad:" + m.group(1)


def clone(rid_: str, dest: Path, home: str | None = None) -> None:
    run("git", "config", "--global", "--add", "safe.directory", "*", home=home, timeout=30)
    # A node that has just started knows no seeds for a while ("no candidate seeds"): keep trying.
    deadline = time.time() + 180
    while True:
        p = run("rad", "clone", rid_, host_path(dest), "--scope", "all", home=home, timeout=300, check=False)
        if p.returncode == 0:
            return
        err = clean(p.stderr or p.stdout).strip()
        if "no candidate seeds" not in err or time.time() > deadline:
            raise RadError(f"`rad clone {rid_}` failed: {err[-600:]}")
        time.sleep(15)


def storage(repo) -> str:
    home = rad(repo, "path", timeout=30).splitlines()[-1].strip()
    return f"{home}/storage/{rid(repo)[4:]}"


def namespaces(repo) -> list[str]:
    """Every node that has published legion refs for this repository, as seen by the local storage."""
    out = run("git", "--git-dir", storage(repo), "for-each-ref", "--format=%(refname)", "refs/namespaces/",
              repo=repo, timeout=60).stdout
    return sorted({m.group(1) for m in re.finditer(r"refs/namespaces/([^/]+)/refs/legion/pub/coord", out)})


def network_sync(repo, force: bool = False) -> bool:
    """Ask the node to fetch the latest from the network; rate-limited, since it takes a few seconds."""
    last = float(_cfg(repo, "legion.radsynced") or 0)
    every = float(os.environ.get("LEGION_RAD_SYNC_EVERY", NETWORK_SYNC_EVERY))
    if not force and time.time() - last < every:
        return True
    ok = run("rad", "sync", "--fetch", rid(repo), repo=repo, check=False, timeout=45).returncode == 0
    subprocess.run(["git", "config", "legion.radsynced", str(time.time())], cwd=repo, capture_output=True)
    return ok


def fetch_all(repo) -> list[str]:
    """Copy every node's published legion refs from the Radicle storage into this repository."""
    nids = namespaces(repo)
    if nids:
        store = storage(repo)
        specs = [f"+refs/namespaces/{n}/refs/legion/pub/*:refs/legion/peers/{n}/*" for n in nids]
        # Every node re-publishes the patches it has seen (that's the gossip), so one patch can sit in
        # several namespaces. Patches are immutable, so take each one from the first namespace that has it.
        out = run("git", "--git-dir", store, "for-each-ref", "--format=%(refname)", "refs/namespaces/",
                  repo=repo, timeout=60).stdout
        chosen = {}
        for ref in out.splitlines():
            m = re.match(r"refs/namespaces/[^/]+/(refs/legion/patches/.+)$", ref)
            if m and m.group(1) not in chosen:
                chosen[m.group(1)] = ref
        specs += [f"+{src}:{dest}" for dest, src in sorted(chosen.items())]
        for attempt in range(3):
            try:
                rgit(repo, "fetch", "-q", "--no-tags", store, *specs, timeout=120)
                break
            except RadError as e:  # another legion process (a viewer) fetching into the same refs right now
                if "cannot lock ref" not in str(e) or attempt == 2:
                    raise
                time.sleep(1)
    return nids


def publish(repo) -> None:
    """Push this node's legion refs to its Radicle namespace; the node announces them to the network."""
    specs = []
    for prefix in ("refs/legion/pub/", "refs/legion/patches/"):
        has = subprocess.run(["git", "for-each-ref", "--count=1", "--format=x", prefix], cwd=repo,
                             capture_output=True, text=True).stdout.strip()
        if has:  # a pattern that matches nothing makes the whole push fail
            specs.append(f"{prefix}*:{prefix}*")
    if specs:
        rgit(repo, "push", "-q", "--force", "rad", *specs, timeout=120)
