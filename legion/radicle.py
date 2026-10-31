"""Radicle as legion's transport.

Every repository pushes its legion refs to its Radicle node, which keeps them in the node's namespace and
replicates them through seeds, so nodes behind NAT or on different networks reach each other without
opening ports. Inside the namespace, each legion identity publishes under its own prefix
(refs/legion/pub/<identity>/coord), so several agents on one computer can share one node without
overwriting each other. To sync, legion reads every publisher from the local Radicle storage and merges
the coord branches, as in plain p2p.

Radicle only ships for Linux and macOS. On Windows it runs inside WSL, and legion calls it through
`wsl.exe`; the repository and the agents stay on Windows. Radicle built for Windows from source (`rad.exe`,
`radicle-node.exe`, `git-remote-rad.exe`) runs natively instead, without WSL: see `native`.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PureWindowsPath

IS_WINDOWS = os.name == "nt"
NATIVE_BIN = Path.home() / ".radicle" / "bin"  # where Radicle's installer puts it, and a Windows build too
DEFAULT_DISTRO = "Ubuntu-24.04"
NETWORK_SYNC_EVERY = 60  # seconds between `rad sync --fetch` runs; pushes announce on their own
REPUBLISH_EVERY = 600  # seconds after which an unchanged publish goes out again anyway
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


_native: dict[str, bool] = {}


def native(repo=None) -> bool:
    """Windows only: is Radicle a Windows build, run directly, rather than the one in WSL? LEGION_RAD_NATIVE or
    git config legion.radnative (1/0, true/false) decide; otherwise a repository set up with WSL stays there (its
    node, and so its node ID and storage, are WSL's), and anything else is native when rad.exe is on the PATH or
    in ~/.radicle/bin. Elsewhere there's no WSL, and this is False."""
    if not IS_WINDOWS:
        return False
    key = str(repo or "")
    if key not in _native:
        said = os.environ.get("LEGION_RAD_NATIVE") or _cfg(repo, "legion.radnative")
        if said:
            _native[key] = said.strip().lower() in ("1", "true", "yes", "on")
        elif _cfg(repo, "legion.wsl"):
            _native[key] = False
        else:
            _native[key] = bool(shutil.which("rad.exe") or (NATIVE_BIN / "rad.exe").exists())
    return _native[key]


def host_path(path, repo=None) -> str:
    """The path as Radicle's side sees it: /mnt/c/... under WSL, unchanged elsewhere."""
    if not IS_WINDOWS or native(repo):
        return str(path)
    p = PureWindowsPath(Path(path).resolve())
    return f"/mnt/{p.drive[0].lower()}" + "/" + "/".join(p.parts[1:])


def run(*args: str, repo=None, home: str | None = None, timeout: float = 60, check: bool = True,
        cwd=None) -> subprocess.CompletedProcess:
    """Run a command where Radicle lives (WSL on Windows, unless it's native), with Radicle on the PATH."""
    home = home if home is not None else (repo and _cfg(repo, "legion.radhome")) or ""
    env = None
    if IS_WINDOWS and native(repo):
        if home and not Path(home).is_absolute():
            raise RadError(f"the Radicle home must be an absolute path, like C:\\Users\\you\\.radicle; got {home!r}")
        env = {**os.environ, "RAD_PASSPHRASE": "", "PATH": f"{NATIVE_BIN}{os.pathsep}{os.environ.get('PATH', '')}"}
        if home:
            env["RAD_HOME"] = home
        # Windows looks the program up in this process's PATH, not the child's: find it here.
        cmd = [shutil.which(args[0], path=env["PATH"]) or args[0], *args[1:]]
    elif home and not home.startswith(("/", "~")):
        # e.g. Git Bash turns /home/x into C:/Program Files/Git/home/x before Python sees it.
        raise RadError(f"the Radicle home must be a path on Radicle's side, like /home/you/.radicle; got {home!r}"
                       " (in Git Bash, set MSYS_NO_PATHCONV=1)")
    else:
        cmd = ["sh", "-c", SHIM, "sh", home, *args]
        if IS_WINDOWS:
            cmd = ["wsl.exe", "-d", distro(repo), *(["--cd", host_path(cwd)] if cwd else []), "-e", *cmd]
            cwd = None
    try:
        if env is None:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        else:
            # Into files, not pipes: `rad node start` leaves radicle-node running with every handle rad inherited,
            # and reading a pipe would wait for the node to exit.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   timeout=timeout)
                out.seek(0)
                err.seek(0)
                p.stdout, p.stderr = (f.read().decode("utf-8", "replace") for f in (out, err))
    except subprocess.TimeoutExpired:
        raise RadError(f"`{' '.join(args[:3])}` timed out after {timeout:.0f}s")
    if IS_WINDOWS and p.returncode and "E_ACCESSDENIED" in (p.stdout + p.stderr).replace("\x00", ""):
        # wsl.exe refuses processes running with a restricted token, which is how agent sandboxes run them.
        raise RadError("WSL refused access to this process (Wsl/E_ACCESSDENIED): the agent's sandbox doesn't let "
                       "it use WSL, where Radicle runs on Windows. Run legion's commands outside the sandbox: in "
                       "Codex, approve them to run without the sandbox, or use full access for this project")
    if check and p.returncode:
        raise RadError(f"`{' '.join(args[:3])}` failed: {clean(p.stderr or p.stdout).strip()[-600:]}")
    return p


def clean(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text or "")


def rad(repo, *args: str, **kw) -> str:
    return clean(run("rad", *args, repo=repo, **kw).stdout).strip()


def rgit(repo, *args: str, **kw) -> subprocess.CompletedProcess:
    """git with Radicle's remote helper available, on this repository."""
    return run("git", "-C", host_path(repo, repo), *args, repo=repo, **kw)


def read_file(path: str, home: str | None = None, repo=None) -> str:
    """A file on Radicle's side (a node's config or log)."""
    if IS_WINDOWS and native(repo):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise RadError(f"couldn't read {path}: {e}")
    return run("cat", path, repo=repo, home=home, timeout=30).stdout


def write_file(path: str, text: str, repo=None) -> None:
    if not IS_WINDOWS or native(repo):
        Path(path).write_text(text, encoding="utf-8")
        return
    p = subprocess.run(["wsl.exe", "-d", distro(), "-e", "sh", "-c", f"cat > '{path}'"], input=text,
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RadError(f"couldn't write the node config: {p.stderr.strip()}")


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
    if IS_WINDOWS and native():
        raise RadError(f"Radicle has no installer for Windows: build rad, radicle-node and git-remote-rad from its "
                       f"source (heartwood) and put them in {NATIVE_BIN}, or set legion.radnative to false to use WSL")
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
    pid = re.search(r"Process ID: \(?(\d+)", clean(p.stdout + p.stderr))
    if IS_WINDOWS and native(repo) and pid:
        try:
            _pid_file(home, repo).write_text(pid.group(1), encoding="utf-8")
        except (OSError, RadError):
            pass
    if p.returncode and not node_running(repo, home):  # a start that "fails" because it's already up is fine
        raise RadError(f"the Radicle node didn't start: {clean(p.stderr or p.stdout).strip()[-400:]}")


def configure_node(home: str, listen: str | None = None, network: str | None = None,
                   connect: list | None = None) -> None:
    """Adjust a node's config (used by the simulation to run several isolated nodes on one machine)."""
    c = json.loads(read_file(f"{home}/config.json", home))
    if listen:
        c["node"]["listen"] = [listen]
    if network:
        c["node"]["network"] = network
        if network == "test":
            c["node"]["peers"] = {"type": "static"}  # no bootstrap to the public network
    if connect is not None:
        c["node"]["connect"] = connect
    write_file(f"{home}/config.json", json.dumps(c, indent=2))


# --- networks that only let HTTP(S) out ---------------------------------------------------------

BRIDGE = ("127.0.0.1", 1080)
# Radicle's public seeds. Behind a proxy the node is told to always connect to them: one that has been
# failing to reach its bootstrap nodes (before the proxy was set) otherwise waits a long while to retry.
PUBLIC_SEEDS = ["z6Mkmqogy2qEM2ummccUthFEaaHvyYmYBYh3dbe9W4ebScxo@rosa.radicle.network:8776",
                "z6MkrLMMsiPWUcNPHcRajuMi9mDfYckSoJyPwwnknocNYPm7@iris.radicle.network:8776"]


def _config_path(home: str | None) -> str:
    return f"{home or rad(None, 'path', timeout=30).splitlines()[-1].strip()}/config.json"


def _edit_config(home: str | None, change) -> None:
    """Apply `change` to the node's config.json, on Radicle's side (WSL on Windows)."""
    path = _config_path(home)
    c = json.loads(read_file(path, home))
    change(c)
    write_file(path, json.dumps(c, indent=2))


# The project's own seed: an always-on node that replicates the whole project, which every node keeps a
# connection to and asks first. Public seeds are volunteers' machines, often slow or down: the explicit fetch
# timed out at 45 s, and what an agent published reached the others when those seeds got to it.

def make_seed(repo, address: str, home: str | None = None) -> str:
    """Make this node the project's seed: it listens on the port of `address` (HOST:PORT, reachable from the
    others) and replicates every namespace of the project. Returns what the others pass to `prefer_seed`."""
    port = address.rsplit(":", 1)[1]
    _edit_config(home, lambda c: (c["node"].__setitem__("listen", [f"0.0.0.0:{port}"]),
                                  c["node"].__setitem__("externalAddresses", [address])))
    run("rad", "seed", rid(repo), "--scope", "all", repo=repo, home=home, timeout=60, check=False)
    restart_node(home)
    ensure_node(repo, home)
    return f"{node_id(repo, home)}@{address}"


def prefer_seed(repo, seed: str, home: str | None = None, restart: bool = True) -> None:
    """Keep a connection to the project's seed (NID@HOST:PORT) and fetch from it first. Without `restart`, the
    node reads the config at its next start, and the caller connects it by hand meanwhile."""
    if not re.fullmatch(r"z6Mk\w+@[\w.-]+:\d+", seed):
        raise RadError("a seed is NID@HOST:PORT, like z6Mk…@seed.example.org:8776 (`legion radicle seed` prints it)")

    def change(c):
        c["node"]["connect"] = [seed] + [s for s in c["node"].get("connect") or [] if s != seed]
        c["preferredSeeds"] = [seed] + [s for s in c.get("preferredSeeds") or [] if s != seed]
    _edit_config(home, change)
    subprocess.run(["git", "config", "legion.seed", seed], cwd=repo, capture_output=True)
    if restart:
        restart_node(home)
        ensure_node(repo, home)


def node_proxy(home: str | None = None) -> str | None:
    try:
        return json.loads(read_file(_config_path(home), home))["node"].get("proxy")
    except (RadError, ValueError, KeyError):
        return None


def set_node_proxy(home: str | None, address: str | None) -> None:
    path = _config_path(home)
    c = json.loads(read_file(path, home))
    c["node"]["proxy"] = address
    if address and c["node"].get("network", "main") == "main":
        connect = c["node"].get("connect") or []
        c["node"]["connect"] = connect + [s for s in PUBLIC_SEEDS if s not in connect]
    write_file(path, json.dumps(c, indent=2))


def bridge_running() -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(BRIDGE) == 0


def start_bridge(proxy_url: str) -> None:
    """Run legion's SOCKS5-to-HTTP-proxy bridge in the background, outliving this command."""
    from .identity import home as legion_home
    log = open(legion_home() / "bridge.log", "ab")
    subprocess.Popen([sys.executable, "-m", "legion.bridge", "--proxy", proxy_url,
                      "--listen", f"{BRIDGE[0]}:{BRIDGE[1]}"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)})
    for _ in range(20):
        if bridge_running():
            return
        time.sleep(0.25)
    raise RadError(f"the proxy bridge didn't start; see {legion_home() / 'bridge.log'}")


def keep_bridge(repo) -> None:
    """Behind an HTTP proxy, restart the bridge if it died (a reboot, say). Cheap when there's no proxy."""
    from .bridge import proxy_from_env
    url = proxy_from_env()
    if url and not IS_WINDOWS and not bridge_running() and \
            node_proxy(_cfg(repo, "legion.radhome")) == f"{BRIDGE[0]}:{BRIDGE[1]}":
        start_bridge(url)


def _pid_file(home: str | None, repo=None) -> Path:
    return Path(home or rad(repo, "path", timeout=30).splitlines()[-1].strip()) / "node" / "legion-node.pid"


def _kill_native_node(home: str | None) -> bool:
    """The native node we started, killed. On Windows a node that had been fetching for a while logged its shutdown
    and then never exited, and `rad node stop` waited for it."""
    try:
        pid = _pid_file(home).read_text(encoding="utf-8").strip()
    except (OSError, RadError):
        return False
    listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                            text=True).stdout
    if "radicle-node" not in listed:  # gone, or the number now belongs to another program
        return False
    return subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True).returncode == 0


def restart_node(home: str | None = None) -> None:
    """Stop the node and make sure it's gone, so the next start reads the config again."""
    native_ = IS_WINDOWS and native()
    try:
        run("rad", "node", "stop", home=home, check=False, timeout=20 if native_ else 60)
    except RadError:
        if not native_:
            raise
    for attempt in range(2):
        for _ in range(30):
            if not node_running(home=home):
                return
            time.sleep(0.5)
        if not (native_ and attempt == 0 and _kill_native_node(home)):
            break
    raise RadError("the Radicle node didn't stop, so it can't pick up the proxy setting; stop it "
                   "(`rad node stop`, or kill radicle-node) and run this again")


def node_log(home: str | None = None, lines: int = 400) -> list[str]:
    path = f"{home or rad(None, 'path', timeout=30).splitlines()[-1].strip()}/node/node.log"
    if IS_WINDOWS and native():
        try:
            return clean(read_file(path, home)).splitlines()[-lines:]
        except RadError:
            return []
    return clean(run("tail", "-n", str(lines), path, home=home, check=False, timeout=30).stdout).splitlines()


def bridge_connections() -> int:
    from .identity import home as legion_home
    try:
        return (legion_home() / "bridge.log").read_text(errors="replace").count(": connected")
    except OSError:
        return 0


def check_proxy_in_use(home: str | None, before: int, wait: float = 60) -> None:
    """After routing the node through the bridge: did it really go out through it? If not, say why."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if bridge_connections() > before:
            return
        time.sleep(2)
    log = node_log(home)
    lookups = [l for l in log if "lookup address" in l]
    version = clean(run("rad", "--version", home=home, check=False, timeout=30).stdout).strip()
    why = ("its log shows it resolving names itself (" + lookups[-1].strip()[-160:] + "), so it isn't using "
           "the proxy" if lookups else "nothing in its log says why")
    raise RadError(f"the Radicle node was set to go out through legion's bridge ({BRIDGE[0]}:{BRIDGE[1]}) but "
                   f"made no connection through it in {wait:.0f} s: {why}. Radicle {version}, config "
                   f"{_config_path(home)} with node.proxy = {node_proxy(home)!r}. "
                   "`python -m legion radicle doctor` shows more")


def route_through_proxy(home: str | None = None) -> str | None:
    """If the seeds are only reachable through an HTTP proxy, send the node through it. Returns what it did."""
    from . import netcheck
    from .bridge import Proxy, proxy_from_env
    bridge = f"{BRIDGE[0]}:{BRIDGE[1]}"
    url = proxy_from_env()
    if node_proxy(home) == bridge:  # set up before: keep the bridge alive (after a reboot, say)
        if not bridge_running():
            if not url:
                raise RadError("Radicle is set to go out through legion's proxy bridge, but there's no HTTPS_PROXY now")
            start_bridge(url)
        return None
    if IS_WINDOWS or not url:
        return None
    seeds = netcheck.SEEDS[:2]
    if any(netcheck.direct(h, p) == "ok" for h, p in seeds):
        return None
    if not any(netcheck.through(Proxy(url), h, p) == "ok" for h, p in seeds):
        raise RadError("this machine reaches no Radicle seed, directly or through its HTTP proxy. "
                       "`python -m legion radicle doctor` shows the details")
    if not bridge_running():
        start_bridge(url)
    set_node_proxy(home, bridge)
    if node_running(home=home):  # a node started earlier never read the setting: restart it
        restart_node(home)
    return f"the seeds are only reachable through the HTTP proxy: Radicle now goes out through it ({bridge})"


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
    if IS_WINDOWS and native():
        subprocess.run(["git", "config", "legion.radnative", "true"], cwd=repo, check=True, capture_output=True)
    elif IS_WINDOWS:
        subprocess.run(["git", "config", "legion.wsl", distro(repo)], cwd=repo, check=True, capture_output=True)


def init_repo(repo, name: str, description: str) -> str:
    """`rad init` on an existing repository (public: Radicle's public seeds will carry it). Returns the RID."""
    trust_repositories(repo=repo)
    rad(repo, "init", host_path(repo, repo), "--name", name, "--description", description,
        "--default-branch", "main", "--public", "--no-confirm", timeout=120)
    url = rgit(repo, "remote", "get-url", "rad").stdout.strip()
    m = re.match(r"rad://(z[1-9A-HJ-NP-Za-km-z]+)", url)
    if not m:
        raise RadError(f"rad init didn't leave a rad remote (got {url!r})")
    return "rad:" + m.group(1)


def trust_repositories(repo=None, home: str | None = None) -> None:
    """In WSL, every repository under /mnt/c belongs to another user, and git refuses to work in it unless told
    to trust it. A native Radicle reads them as their owner, and the user's own git config is left alone."""
    if not (IS_WINDOWS and native(repo)):
        run("git", "config", "--global", "--add", "safe.directory", "*", repo=repo, home=home, timeout=30)


def clone(rid_: str, dest: Path, home: str | None = None) -> None:
    trust_repositories(home=home)
    # A node that has just started knows no seeds for a while ("no candidate seeds"), and one whose routing
    # table never filled (it was offline, or behind a proxy, when it connected) may not learn of any: keep
    # trying, and from the second try on also ask the public seeds directly.
    deadline = time.time() + 180
    tries = 0
    while True:
        extra = seed_args(home) if tries else []
        p = run("rad", "clone", rid_, host_path(dest), "--scope", "all", *extra, home=home, timeout=300, check=False)
        if p.returncode == 0:
            return
        err = clean(p.stderr or p.stdout).strip()
        if not ("no candidate seeds" in err or "no seeds found" in err) or time.time() > deadline:
            raise RadError(f"`rad clone {rid_}` failed: {err[-600:]}")
        tries += 1
        time.sleep(15)


def seed_args(home: str | None) -> list[str]:
    """`--seed` options for Radicle's public seeds, if this node is on the public network."""
    try:
        network = json.loads(read_file(_config_path(home), home))["node"].get("network")
    except (RadError, ValueError, KeyError):
        network = None
    if network not in (None, "main"):
        return []
    return [arg for s in PUBLIC_SEEDS for arg in ("--seed", s.split("@")[0])]


def storage(repo) -> str:
    home = rad(repo, "path", timeout=30).splitlines()[-1].strip()
    return f"{home}/storage/{rid(repo)[4:]}"


def _storage_refs(repo) -> list[str]:
    return list(_storage_values(repo))


def _storage_values(repo) -> dict[str, str]:
    """Every ref in the Radicle storage, and the commit it points to."""
    out = run("git", "--git-dir", storage(repo), "for-each-ref", "--format=%(objectname) %(refname)",
              "refs/namespaces/", repo=repo, timeout=60).stdout
    return {ref: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines() if " " in line)}


def local_values(repo, *prefixes: str) -> dict[str, str]:
    """This repository's refs under these prefixes, and the commit each points to."""
    out = subprocess.run(["git", "for-each-ref", "--format=%(objectname) %(refname)", *prefixes], cwd=repo,
                         capture_output=True, text=True).stdout
    return {ref: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines() if " " in line)}


PUB_RE = re.compile(r"refs/namespaces/([^/]+)/refs/legion/pub/(?:([0-9a-f]{16})/)?coord$")


def publishers(repo, refs: list[str] | None = None) -> dict[str, str]:
    """Every legion repository that has published state, as seen by the local storage:
    {local peer name: its prefix in the storage}. The name is <identity>-<end of node id>; repositories that
    published before identities had their own prefix (one per node) are named after the node."""
    out = {}
    for ref in refs if refs is not None else _storage_refs(repo):
        m = PUB_RE.match(ref)
        if m:
            nid, who = m.groups()
            name = f"{who}-{nid[-6:]}" if who else nid
            out[name] = ref[:-len("coord")]
    return dict(sorted(out.items()))


def namespaces(repo) -> list[str]:
    return sorted({prefix.split("/")[2] for prefix in publishers(repo).values()})


def network_sync(repo, force: bool = False) -> bool:
    """Ask the node to fetch the latest from the network; rate-limited, since it takes a few seconds."""
    last = float(_cfg(repo, "legion.radsynced") or 0)
    every = float(os.environ.get("LEGION_RAD_SYNC_EVERY", NETWORK_SYNC_EVERY))
    if not force and time.time() - last < every:
        return True
    seed = _cfg(repo, "legion.seed")
    p = None
    if seed:  # the project's own seed first: it has everything, and answers
        p = run("rad", "sync", "--fetch", rid(repo), "--seed", seed.split("@")[0], "--timeout", "30sec", repo=repo,
                check=False, timeout=45)
    if p is None or p.returncode:
        p = run("rad", "sync", "--fetch", rid(repo), repo=repo, check=False, timeout=45)
    if p.returncode and "seeds" in clean(p.stderr + p.stdout):  # an empty routing table: ask the public seeds
        p = run("rad", "sync", "--fetch", rid(repo), *seed_args(_cfg(repo, "legion.radhome")), repo=repo,
                check=False, timeout=45)
    ok = p.returncode == 0
    subprocess.run(["git", "config", "legion.radsynced", str(time.time())], cwd=repo, capture_output=True)
    return ok


# Commands don't wait for the network. In Erdős #902 every command paid for `rad sync --fetch` (45 s, and it timed
# out each time) and, when it failed, for a second try: a minute and a half before reading anything, which agents
# whose tools cut a call at two minutes saw as failures. The node hears of new refs by gossip on its own; the
# explicit fetch, which catches up after the node was off, now runs in a process of its own, at most every
# NETWORK_SYNC_EVERY seconds, and leaves its outcome in .git/legion-netsync.json for the next command to read.

def _netsync_file(repo, name: str = "netsync") -> Path:
    git_dir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo, capture_output=True,
                             text=True).stdout.strip()
    return Path(git_dir or Path(repo) / ".git") / f"legion-{name}.json"


def netsync_status(repo, name: str = "netsync") -> dict:
    try:
        return json.loads(_netsync_file(repo, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_netsync(repo, status: dict, name: str = "netsync") -> None:
    try:
        _netsync_file(repo, name).write_text(json.dumps(status), encoding="utf-8")
    except OSError:
        pass


# Nor do they wait for the seeds to take what they publish: a push that also syncs waited ten seconds for their
# answer, twice per publish. The push only updates the node; telling the network runs in a process of its own,
# one at a time, which goes again when more was published while it ran.

def kick_announce(repo) -> None:
    _kick(repo, "announce")


def pid_alive(pid) -> bool:
    """Whether the process runs. Sandboxes kill the background processes, which leave "running" behind them:
    a publish taken for under way for ten minutes after it died, and a node nobody restarted meanwhile."""
    if not isinstance(pid, int) or pid <= 0:
        return True  # an older legion's process, which noted no pid: as before, its age decides
    if IS_WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # someone else's: it exists
    return True


def _under_way(status: dict, stale: float) -> bool:
    return (bool(status.get("running")) and time.time() - status.get("started", 0) < stale
            and pid_alive(status.get("pid")))


def _kick(repo, name: str, stale: float = 120) -> None:
    """Run `legion netsync --<name>` in a process of its own, one at a time: if one runs, it goes again."""
    now, status = time.time(), netsync_status(repo, name)
    if _under_way(status, stale):
        _write_netsync(repo, dict(status, again=True), name)
        return
    _write_netsync(repo, {"started": now, "running": True}, name)
    try:
        p = spawn_detached([sys.executable, "-m", "legion", "netsync", f"--{name}"], repo)
        _write_netsync(repo, {"started": now, "running": True, "pid": getattr(p, "pid", None)}, name)
    except OSError:
        _write_netsync(repo, {"started": now, "running": False}, name)


def _rounds(repo, name: str, work) -> None:
    """What a kicked process does: the work, again while more was asked for meanwhile."""
    try:
        while True:
            work()
            if not netsync_status(repo, name).get("again"):
                break
            _write_netsync(repo, {"started": time.time(), "running": True, "pid": os.getpid()}, name)
    finally:
        _write_netsync(repo, {"started": time.time(), "running": False}, name)


def run_announce(repo) -> None:
    _rounds(repo, "announce", lambda: run("rad", "sync", "--announce", rid(repo), "--timeout", "30sec", repo=repo,
                                          check=False, timeout=60))


# Where no sync process can live (agents' sandboxes kill what runs in the background every half hour or so), each
# command synced by itself, push included: 20 s of CPU in Radicle's helper, and commands cut off at 60-110 s. The
# command only brings the coord up to date now, and a process of its own pushes, which lives long enough for that.

def kick_publish(repo) -> None:
    _kick(repo, "publish", stale=PUSH_TIMEOUT)


def wait_marked(repo, seconds: float) -> bool:
    """Wait until this clone's latest events and main are what the node holds from it (published_mark), as a
    sync process records once it pushed them, up to `seconds`. True if they are."""
    end = time.time() + seconds
    while True:
        last = (_cfg(repo, "legion.published") or "").split()
        if last and last[0] == published_mark(repo):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


def wait_published(repo, seconds: float) -> bool:
    """Wait until no publish runs (it finished, or died), up to `seconds`. True if it's done."""
    end = time.time() + seconds
    while _under_way(netsync_status(repo, "publish"), PUSH_TIMEOUT):
        if time.time() >= end:
            return False
        time.sleep(0.5)
    return True


def run_publish(repo) -> None:
    from .coordgit import publish_radicle
    from .identity import Identity
    ident = Identity.load(repo=repo)
    if ident is not None:
        _rounds(repo, "publish", lambda: publish_radicle(repo, ident))


def kick_network_sync(repo) -> bool:
    """Start a network fetch in the background, unless one ran lately or is running, and don't wait for it.
    False when the last one, recently, couldn't reach the network: the caller says so."""
    now, status = time.time(), netsync_status(repo)
    every = float(os.environ.get("LEGION_RAD_SYNC_EVERY", NETWORK_SYNC_EVERY))
    running = _under_way(status, 300)
    if not running and (now - status.get("started", 0) >= every or status.get("running")):  # or it died
        _write_netsync(repo, dict(status, started=now, running=True))
        try:
            p = spawn_detached([sys.executable, "-m", "legion", "netsync"], repo)
            _write_netsync(repo, dict(status, started=now, running=True, pid=getattr(p, "pid", None)))
        except OSError:
            _write_netsync(repo, dict(status, started=now, running=False, ok=False))
    return status.get("ok", True) or now - status.get("ended", 0) > 900


def spawn_detached(cmd: list[str], cwd, log=None, env=None) -> subprocess.Popen:
    """Start a process that outlives this one: its own hidden console and process group on Windows, out of the
    agent's job when the job lets it go; its own session elsewhere."""
    out = open(log, "a", encoding="utf-8") if log else subprocess.DEVNULL  # noqa: SIM115 (the child keeps it)
    kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
              stderr=subprocess.STDOUT if log else subprocess.DEVNULL)
    if not IS_WINDOWS:
        return subprocess.Popen(cmd, start_new_session=True, **kw)
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(cmd, creationflags=flags, **kw)


def run_network_sync(repo) -> bool:
    """What the background process does: make sure the node runs, and fetch from the network."""
    from . import tunnel
    started, ok = time.time(), False
    try:
        if not node_running(repo):  # stopped (a reboot, a suspended computer): start it
            ensure_node(repo)
            time.sleep(5)  # it needs a moment to connect to the seeds
        tunnel.keep(repo)  # the direct tunnels to the other nodes, if this clone uses them: relaunched if they died
        ok = network_sync(repo, force=True)
    except RadError:
        ok = False
    finally:
        _write_netsync(repo, {"started": started, "ended": time.time(), "running": False, "ok": ok})
    return ok


MAX_ARGS = 20000  # characters of refspecs per fetch: Windows caps a command line at 32767


def batches(specs: list[str]) -> list[list[str]]:
    """The refspecs in groups short enough for one command line."""
    out, size = [[]], 0
    for s in specs:
        if out[-1] and size + len(s) + 1 > MAX_ARGS:
            out.append([])
            size = 0
        out[-1].append(s)
        size += len(s) + 1
    return out


def fetch_all(repo) -> list[str]:
    """Copy every publisher's legion refs from the Radicle storage into this repository."""
    tidy(repo)  # the fetch runs in WSL too: packed, it reads a few files instead of thousands
    held = _storage_values(repo)
    refs = list(held)
    pubs = publishers(repo, refs)
    if pubs:
        wanted = {f"refs/legion/peers/{name}/{ref[len(prefix):]}": ref
                  for name, prefix in pubs.items() for ref in refs if ref.startswith(prefix)}
        # Every node re-publishes the patches it has seen (that's the gossip), so one patch can sit in
        # several namespaces. Patches are immutable, so take each one from the first namespace that has it.
        gone = judged(repo)
        for ref in refs:
            m = re.match(r"refs/namespaces/[^/]+/(refs/legion/patches/.+)$", ref)
            if m and m.group(1) not in wanted and m.group(1) not in gone:
                wanted[m.group(1)] = ref
        # Only what changed since the last fetch: fetching all of it again on every command, from /mnt/c, took
        # minutes even when nothing was new.
        have = local_values(repo, "refs/legion/peers/", "refs/legion/patches/")
        broken = _unfetchable(repo)
        specs = [f"+{src}:{dest}" for dest, src in sorted(wanted.items())
                 if have.get(dest) != held[src] and f"{src} {held[src]}" not in broken]
        for batch in batches(specs) if specs else []:
            _fetch(repo, batch, held)
    # Peers that aren't published any more (a repository that moved to per-identity refs): forget them.
    local = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/legion/peers/"], cwd=repo,
                           capture_output=True, text=True).stdout.splitlines()
    for ref in local:
        if ref.split("/")[3] not in pubs:
            subprocess.run(["git", "update-ref", "-d", ref], cwd=repo, capture_output=True)
    return list(pubs)


def _unfetchable_file(repo) -> Path:
    d = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return Path(d or Path(repo) / ".git") / "legion-unfetchable"


def _unfetchable(repo) -> set[str]:
    """Storage refs, with the commit they pointed to, that couldn't be fetched: skipped until they move."""
    try:
        return set(_unfetchable_file(repo).read_text(encoding="utf-8").splitlines())
    except OSError:
        return set()


def _fetch(repo, specs: list[str], held: dict[str, str]) -> None:
    """Fetch these refspecs from the storage. A batch that fails is split in two until what fails is one ref,
    which is noted and skipped until it moves: in Erdős #902 a node cut off mid-write left one ref pointing at a
    commit the storage didn't have, and every fetch of every clone on that machine failed with it."""
    for attempt in range(3):
        try:
            rgit(repo, "fetch", "-q", "--no-tags", storage(repo), *specs, timeout=120)
            return
        except RadError as e:
            if "cannot lock ref" in str(e) and attempt < 2:  # another legion process fetching the same refs
                time.sleep(1)
                continue
            if "timed out" in str(e):
                raise
            if len(specs) > 1:
                _fetch(repo, specs[:len(specs) // 2], held)
                _fetch(repo, specs[len(specs) // 2:], held)
            else:
                src = specs[0].lstrip("+").split(":", 1)[0]
                with open(_unfetchable_file(repo), "a", encoding="utf-8") as f:
                    f.write(f"{src} {held.get(src, '')}\n")
            return


def _judged_file(repo) -> Path:
    d = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo,
                       capture_output=True, text=True).stdout.strip()
    return Path(d) / "legion-judged"


def judged(repo) -> set[str]:
    """Patch refs this repository dropped once they had a verdict: never fetched or published again. Without
    this, every sync brought them back from the other nodes' namespaces and the next prune deleted them, and a
    process pushing while another one pruned failed on a ref that had just disappeared."""
    try:
        return set(_judged_file(repo).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def forget_judged(repo, refs) -> None:
    new = set(refs) - judged(repo)
    if new:
        with open(_judged_file(repo), "a", encoding="utf-8") as f:
            f.write("".join(f"{r}\n" for r in sorted(new)))


# A push to the node: from a repository under /mnt/c, with the disk cold after a reboot, three minutes and more.
PUSH_TIMEOUT = 600


def publish_specs(repo, who: str) -> list[str]:
    """The refspecs that push this repository's legion refs under its identity's own prefix: its coord, and the
    patches the node doesn't hold yet as they are here. Pushing every patch again on every sync took minutes from
    /mnt/c, longer than the push may take, so nothing went out at all. And the deletion of those the node holds
    that were dropped here: every node kept every save and judged patch it ever saw, 24,000 refs in Erdős #902,
    and Radicle's helper spent eleven seconds of each push going through them."""
    def listed(prefix):
        out = subprocess.run(["git", "for-each-ref", "--format=%(objectname) %(refname)", prefix], cwd=repo,
                             capture_output=True, text=True).stdout
        return {ref: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines() if " " in line)}

    specs = [f"refs/legion/pub/*:refs/legion/pub/{who}/*"] if listed("refs/legion/pub/") else []
    # Patches by name, not with a pattern: judged ones are deleted as they go, and on Windows the git in WSL
    # can still see a just-deleted ref file under /mnt/c, expand the pattern to it and fail the whole push.
    gone, held = judged(repo), node_patches(repo)
    return specs + [f"{ref}:{ref}" for ref, sha in listed("refs/legion/patches/").items()
                    if ref not in gone and (held is None or held.get(ref) != sha)] \
        + [f":{ref}" for ref in held or () if ref in gone]


def node_patches(repo) -> dict[str, str] | None:
    """The patch refs the node already holds from this node, and where they point (None if it can't tell: then
    everything is pushed)."""
    if not rid(repo):
        return None
    try:
        me = _cfg(repo, "legion.nid")
        if not me:
            me = node_id(repo)
            subprocess.run(["git", "config", "legion.nid", me], cwd=repo, capture_output=True)
        out = run("git", "--git-dir", storage(repo), "for-each-ref", "--format=%(objectname) %(refname)",
                  f"refs/namespaces/{me}/refs/legion/patches/", repo=repo, timeout=60).stdout
    except (RadError, OSError):
        return None
    cut = len(f"refs/namespaces/{me}/")
    return {ref[cut:]: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines() if " " in line)}


def main_spec(repo) -> list[str]:
    """The refspec that moves the repository's ordinary `main` branch, the one Radicle's web and `rad clone`
    show, to the official main, if it moved. Not forced: it only moves forward, so a repository with an older
    view (another agent sharing this node, say) can't drag it back."""
    tip = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/legion/main"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    shown = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/remotes/rad/main"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    return ["refs/legion/main:refs/heads/main"] if tip and tip != shown else []


def publish_main(repo) -> None:
    """Push main_spec alone; a refused push is no error."""
    for spec in main_spec(repo):
        rgit(repo, "push", "-q", "-o", "no-sync", "rad", spec, check=False, timeout=PUSH_TIMEOUT)
        main_offered(repo)


def main_offered(repo) -> None:
    """This main went to the node, or the node has a newer one: either way, not again until it moves. Refused,
    it was offered again on every publish, in a push of its own that made the whole publish fail."""
    subprocess.run(["git", "update-ref", "refs/remotes/rad/main", "refs/legion/main"], cwd=repo, capture_output=True)


def only_main_refused(e: RadError) -> bool:
    lines = [l for l in str(e).splitlines() if "rejected]" in l]
    return len(lines) == 1 and "[rejected]" in lines[0] and "-> main " in lines[0]


LOOSE_OBJECTS = 300


def tidy(repo) -> None:
    """Pack loose objects and refs before a push. On Windows the push runs in WSL, which reads the repository
    through /mnt/c one file at a time: with thousands of loose objects (every event and merge makes some) a
    push took three minutes, and packed, twenty seconds. The packing itself runs on this side, and is quick."""
    count = subprocess.run(["git", "count-objects"], cwd=repo, capture_output=True, text=True).stdout.split()
    if count and count[0].isdigit() and int(count[0]) > LOOSE_OBJECTS:
        subprocess.run(["git", "repack", "-d", "-q"], cwd=repo, capture_output=True)
        subprocess.run(["git", "prune-packed", "-q"], cwd=repo, capture_output=True)
    subprocess.run(["git", "pack-refs", "--all"], cwd=repo, capture_output=True)


BIG_PUBLISH = 20  # more refspecs than this besides its own coord: the coord goes out first, alone


def publish(repo, who: str) -> None:
    """Push this repository's legion refs to the node, under `who` (its identity's key, 16 hex characters);
    the node announces them to the network."""
    mark = published_mark(repo)
    last = (_cfg(repo, "legion.published") or "").split()
    if len(last) == 2 and last[0] == mark and time.time() - float(last[1]) < REPUBLISH_EVERY:
        return  # nothing new since the last push: a read-only command doesn't pay 10 s for it
    tidy(repo)
    specs = [s if s.startswith(":") else "+" + s for s in publish_specs(repo, who)]
    own = [s for s in specs if s.startswith("+refs/legion/pub/")]
    rest = [s for s in specs if s not in own]
    main = main_spec(repo)
    try:
        # Everything in one push when it goes through: Radicle's helper spends ten seconds and more on each
        # before it sends anything, and a publish made four. But not when there's a lot besides this node's own
        # events (the first publish after an update deletes hundreds of refs): where a sandbox kills the push
        # when the command returns, the big one never finished, and with it went this node's events.
        if len(rest) > BIG_PUBLISH:
            raise RadError("too much for one push")
        try:
            _push(repo, own + rest + main, strict=True)
        except RadError as e:
            if not only_main_refused(e):  # the node has a newer main: the rest went through
                raise
        if main:
            main_offered(repo)
    except RadError as e:
        if "timed out" in str(e):
            raise
        # Then its own coord first and alone: whatever happens to the patches, its events go out. Then the
        # patches it gossips, in batches split in two on failure: one patch whose objects this clone lacks
        # ("missing necessary objects") made every push fail, and with them every event of this node.
        _push(repo, own, strict=True)
        for i in range(0, len(rest), 200):  # keeps the command line short with many patches
            _push(repo, rest[i:i + 200], strict=False)
        publish_main(repo)
    subprocess.run(["git", "config", "legion.published", f"{mark} {time.time()}"], cwd=repo, capture_output=True)
    kick_announce(repo)


def _push(repo, specs: list[str], strict: bool) -> None:
    """Push these refspecs (forced where they start with +) to the node. Not strict: a batch that fails is split
    in two until what fails is one ref, which is left out (another node re-offers it)."""
    if not specs:
        return
    for attempt in range(3):
        try:
            rgit(repo, "push", "-q", "-o", "no-sync", "rad", *specs, timeout=PUSH_TIMEOUT)
            return
        except RadError as e:
            if "cannot lock ref" in str(e) and attempt < 2:  # another clone pushing to the same node right now
                time.sleep(2)
                continue
            if strict or "timed out" in str(e):
                raise
            if len(specs) > 1:
                _push(repo, specs[:len(specs) // 2], strict)
                _push(repo, specs[len(specs) // 2:], strict)
            return


def published_mark(repo) -> str:
    """What makes a publish worth its time, as one hash: this node's own latest event (a patch of its own comes
    with one) and the official main. Not the coord, which every sync merges with the others' events, nor the
    others' patches it fetched: those reach the network from their authors' namespaces, and this node re-offers
    them every REPUBLISH_EVERY anyway."""
    import hashlib
    out = subprocess.run(["git", "for-each-ref", "--format=%(objectname) %(refname)", "refs/legion/own",
                          "refs/legion/main"], cwd=repo, capture_output=True).stdout
    return hashlib.sha1(out).hexdigest()
