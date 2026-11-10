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
SEED_BACKOFF = 600  # seconds a fetch skips the project's own seed after it failed
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
    home = home or _cfg(repo, "legion.radhome")  # the clone's node, not the default ~/.radicle
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
    # The clone's node: a node whose home isn't the default one (a native node next to WSL's, ~/.radicle-jose) had
    # its config looked for in ~/.radicle, which had none.
    home = home or _cfg(repo, "legion.radhome")

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
    project = rid(repo)
    if not project:
        # not a Radicle clone: on Linux the background fetch's storage upkeep took it for one and crashed
        raise RadError("this clone isn't on Radicle: no storage")
    home = rad(repo, "path", timeout=30).splitlines()[-1].strip()
    return f"{home}/storage/{project[4:]}"


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


def network_sync(repo, force: bool = False, quick: bool = False) -> bool:
    """Ask the node to fetch the latest from the network; rate-limited, since it takes a few seconds. `quick`: one
    try, for a command that waits for it (see fetch_inline)."""
    last = float(_cfg(repo, "legion.radsynced") or 0)
    every = float(os.environ.get("LEGION_RAD_SYNC_EVERY", NETWORK_SYNC_EVERY))
    if not force and time.time() - last < every:
        return True
    seed = _cfg(repo, "legion.seed")
    p = None
    down = time.time() - float(_cfg(repo, "legion.seeddown") or 0) < SEED_BACKOFF
    if seed and not down:  # the project's own seed first: it has everything, and answers (briefly when a command
        # waits: an Umbrel switched off made every quick fetch fail, the others never asked)
        wait = "10sec" if quick else "30sec"
        p = run("rad", "sync", "--fetch", rid(repo), "--seed", seed.split("@")[0], "--timeout", wait, repo=repo,
                check=False, timeout=25 if quick else 45)
        # Down, it cost every fetch its 30 to 45 s before the network was asked: not again for SEED_BACKOFF.
        subprocess.run(["git", "config", "legion.seeddown", "0" if p.returncode == 0 else str(time.time())],
                       cwd=repo, capture_output=True)
    if p is None or p.returncode:
        p = run("rad", "sync", "--fetch", rid(repo), "--timeout", "30sec", repo=repo, check=False, timeout=45)
    if p.returncode and not quick:  # whatever went wrong (not only an empty routing table): the public seeds
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
    from .coordgit import git_dir
    return git_dir(repo) / f"legion-{name}.json"


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


def pid_alive(pid, what: str = "legion") -> bool:
    """Whether the process runs, and is the one meant (its command line names `what`, where it can be read).
    Sandboxes kill the background processes, which leave "running" behind them: a publish taken for under way
    for ten minutes after it died, and a node nobody restarted meanwhile. And a sandbox started anew numbers its
    processes from low again, so the pid of a dead push was soon another process's."""
    if not isinstance(pid, int) or pid <= 0:
        return True  # an older legion's process, which noted no pid: as before, its age decides
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not IS_WINDOWS and Path("/proc").is_dir():
        try:
            return what.encode() in cmdline.read_bytes()
        except OSError:
            return False
    if os.name == "nt":
        # The system this really runs on, whatever IS_WINDOWS was told for a test: below, os.kill(pid, 0) only
        # asks on POSIX. On Windows it ends the process. A test that played Linux on a Windows machine asked
        # whether the machine's real sync process was alive, and killed it: jose's died with every run of the
        # suite, five times in one evening.
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code, size, image = ctypes.c_ulong(), ctypes.c_ulong(1024), ctypes.create_unicode_buffer(1024)
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        named = k.QueryFullProcessImageNameW(h, 0, image, ctypes.byref(size))
        k.CloseHandle(h)
        if not ok or code.value != 259:  # STILL_ACTIVE
            return False
        # Its number may be another program's by now (after a reboot they start low again): legion's own
        # processes are Python's. The command line can't be read here as on Linux; the program's name can.
        name = Path(image.value).name.lower() if named else ""
        return not name or "python" in name or "pypy" in name
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, SystemError):  # Windows' os.kill raises SystemError for some processes of others
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


def timed(what: str, since: float) -> None:
    """With LEGION_TIMING set, how long a step took, on stderr (see transport.timed): instinct's `a` ran
    `netsync --publish` twice to its tools' limit of 120 s, and nothing said where it stood. (A detail of a
    step transport names: see timing.py.)"""
    from . import timing
    timing.timed(what, since, part=True)


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


def announce_waiting(repo) -> bool:
    """Announce this node's refs and wait, up to 40 s, until one seed has fetched them. True if one did."""
    if not rid(repo):
        return False
    try:
        return run("rad", "sync", "--announce", rid(repo), "--replicas", "1", "--timeout", "40sec", repo=repo,
                   check=False, timeout=50).returncode == 0
    except (RadError, OSError):
        return False


def run_announce(repo) -> None:
    _rounds(repo, "announce", lambda: run("rad", "sync", "--announce", rid(repo), "--timeout", "30sec", repo=repo,
                                          check=False, timeout=60))


# Where no sync process can live (agents' sandboxes kill what runs in the background every half hour or so), each
# command synced by itself, push included: 20 s of CPU in Radicle's helper, and commands cut off at 60-110 s. The
# command only brings the coord up to date now, and a process of its own pushes, which lives long enough for that.

PUBLISH_STALE = 300  # seconds after which a publish whose process can't be checked is taken as dead


def kick_publish(repo) -> None:
    _kick(repo, "publish", stale=PUBLISH_STALE)


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
    while _under_way(netsync_status(repo, "publish"), PUBLISH_STALE):
        if time.time() >= end:
            return False
        time.sleep(0.5)
    return True


# A node whose storage was rebuilt (a sandbox that comes back from an older copy, a reinstall) signs its namespace's
# refs anew on top of what it has, not on top of what the network holds from it: the seeds, which keep the older
# chain, take the new one for a fork and drop it, though they fetch it "successfully". In Erdős #902 nothing
# instinct's machine `a` published reached anyone for a day and a half. So before it publishes, a node asks a public
# seed (over git's HTTPS, which needs neither the node nor its connections) what it holds of this node's namespace,
# and when its own chain doesn't descend from that, takes that one back as its starting point: the next signing goes
# on top of it, and the seeds accept it. At most every HEAL_EVERY seconds.
HEAL_SEEDS = ("rosa.radicle.network", "iris.radicle.network", "seed.radicle.at")
HEAL_EVERY = 900  # an hour of a forked chain was an hour of everything this node published lost


def heal_urls(repo) -> list[str]:
    return [f"https://{host}/{rid(repo)[4:]}.git" for host in HEAL_SEEDS]


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def nid_key(nid: str) -> bytes:
    """The ed25519 public key a node id names: z, then base58 of the multicodec ed01 and the 32 bytes."""
    n = 0
    for ch in nid[1:]:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if not nid.startswith("z") or len(raw) != 34 or raw[:2] != b"\xed\x01":
        raise ValueError(f"not an ed25519 node id: {nid}")
    return raw[2:]


def _sig_ok(key: bytes, refs: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(sig, refs)
        return True
    except (InvalidSignature, ValueError):
        return False


def signed_by(store, commit: str, nid: str) -> bool:
    """Whether a commit of signed refs carries nid's signature of its list of refs, as Radicle writes them: the
    tree holds the list in `refs` and the 64 bytes of the signature in `signature`."""
    blob = lambda name: subprocess.run(["git", "-C", str(store), "cat-file", "blob", f"{commit}:{name}"],
                                       capture_output=True)
    refs, sig = blob("refs"), blob("signature")
    try:
        return not refs.returncode and not sig.returncode and _sig_ok(nid_key(nid), refs.stdout, sig.stdout)
    except ValueError:
        return False


def chain_signed_by(store, tip: str, base: str, nid: str) -> bool:
    """Whether every commit of signed refs from tip back to base (not included; all of them without one)
    carries nid's signature, not only the tip."""
    try:
        key = nid_key(nid)
    except ValueError:
        return False
    listed = subprocess.run(["git", "-C", str(store), "rev-list", tip, *(["--not", base] if base else [])],
                            capture_output=True, text=True)
    commits = listed.stdout.split()
    if listed.returncode or not commits:
        return not listed.returncode
    query = "".join(f"{c}:refs\n{c}:signature\n" for c in commits).encode()
    out = subprocess.run(["git", "-C", str(store), "cat-file", "--batch"], input=query, capture_output=True).stdout
    blobs, i = [], 0
    while i < len(out):
        end = out.index(b"\n", i)
        head = out[i:end].split()
        if len(head) != 3 or head[1] != b"blob":
            return False  # missing, or not a blob: not a commit of signed refs
        size = int(head[2])
        blobs.append(out[end + 1:end + 1 + size])
        i = end + 1 + size + 1
    return len(blobs) == 2 * len(commits) and all(_sig_ok(key, blobs[j], blobs[j + 1])
                                                  for j in range(0, len(blobs), 2))


def _git(where, *args, **kw):
    return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, **kw)


def _descends(store, a: str, b: str) -> bool | None:
    """Whether b is a or goes on from it; None when git can't say (an object missing, an error)."""
    if a == b:
        return True
    return {0: True, 1: False}.get(_git(store, "merge-base", "--is-ancestor", a, b).returncode)


HEAL_CASES = {
    "error": "couldn't be asked",
    "none": "holds nothing of this node",
    "same": "holds what this node holds",
    "behind": "holds an earlier point of this node's chain: nothing to heal",
    "absent": "holds a tip this storage doesn't have: this node's chain doesn't go through it",
    "ahead": "holds a later point of this node's chain",
    "forked": "holds another branch of this node's chain",
    "unknown": "git couldn't compare them",
}


def sigrefs_status(repo, fetch: bool = False) -> dict:
    """This node's signed refs here and at each seed of HEAL_SEEDS, and how they compare (HEAL_CASES). Only
    reads, unless fetch: then the seeds' tips are fetched into refs/legion-heal/ of the storage to compare them,
    and absent turns into ahead or forked (the caller removes those refs)."""
    nid = _cfg(repo, "legion.nid") or node_id(repo)
    store, ref = storage(repo), f"refs/namespaces/{nid}/refs/rad/sigrefs"
    mine = _git(store, "rev-parse", "-q", "--verify", ref).stdout.strip()
    seeds = []
    for n, url in enumerate(heal_urls(repo)):
        host = url.split("//", 1)[-1].split("/", 1)[0]
        t0 = time.time()
        try:  # 15 s: iris, down, cost every publish 40 s here
            p = _git(store, "ls-remote", url, ref, timeout=15)
        except subprocess.TimeoutExpired:
            p = None
        timed(f"asking {host}", t0)
        if p is None or p.returncode:
            seeds.append({"seed": host, "url": url, "tip": "", "case": "error"})
            continue
        tip = p.stdout.split()[0] if p.stdout.split() else ""
        if fetch and tip:
            try:
                _git(store, "fetch", "-q", "--no-tags", url, f"+{ref}:refs/legion-heal/{n}", timeout=120)
            except subprocess.TimeoutExpired:
                pass
        if not tip:
            case = "none"
        elif tip == mine:
            case = "same"
        elif _git(store, "cat-file", "-e", tip + "^{commit}").returncode:
            case = "absent"
        else:
            back = _descends(store, tip, mine) if mine else False
            fore = _descends(store, mine, tip) if mine else True
            case = "unknown" if back is None or fore is None else \
                "behind" if back else "ahead" if fore else "forked"
        seeds.append({"seed": host, "url": url, "tip": tip, "case": case})
    return {"nid": nid, "store": store, "ref": ref, "mine": mine,
            "signed": bool(mine) and signed_by(store, mine, nid), "seeds": seeds}


def status_text(st: dict) -> str:
    lines = [f"this node ({st['nid']}): {st['mine'][:8] or 'no signed refs'}"
             + ("" if not st["mine"] else ", signed by its key" if st["signed"] else ", NOT signed by its key")]
    for s in st["seeds"]:
        lines.append(f"  {s['seed']}: {s['tip'][:8] or '-'} {s['case']} ({HEAL_CASES[s['case']]})")
    return "\n".join(lines)


def heal(repo, seed: str | None = None) -> tuple[bool, str]:
    """Take up the network's chain of this node's signed refs where this storage's forked from it (see
    HEAL_SEEDS). Only when the seeds agree: one of them holds a tip every other seed's goes on to; only a chain
    whose every new commit this node's key signed; and only if the ref still holds what it held when the seeds
    were asked: another process that signed meanwhile wins. What it held is kept in the clone, under
    refs/legion-backup/, and unheal() puts it back. Whether it moved the ref, and why (not).

    `seed` (a host of HEAL_SEEDS): that seed's chain, whatever the others hold. instinct's `b` forked more than
    once, rosa and seed.radicle.at each kept another branch of its chain, and no heal could choose: nothing it
    published reached anyone. With one seed named, that one takes it from there; the other keeps its branch."""
    st = sigrefs_status(repo, fetch=True)
    if seed:
        named = [s for s in st["seeds"] if s["seed"] == seed]
        if not named:
            return False, f"{seed} isn't one of the seeds asked ({', '.join(s['seed'] for s in st['seeds'])})"
        st = dict(st, seeds=named)
    store, ref, mine = st["store"], st["ref"], st["mine"]
    try:
        cases = {s["case"] for s in st["seeds"]}
        tips = {s["tip"] for s in st["seeds"] if s["tip"]}
        if not tips:
            return False, "no seed holds anything of this node"
        if "unknown" in cases or "absent" in cases:
            return False, "a seed's chain couldn't be fetched or compared: left as it is"
        best = [t for t in tips if all(_descends(store, u, t) for u in tips)]
        if len(best) != 1:
            return False, "the seeds hold different branches of this node's chain: left as it is"
        best = best[0]
        if mine and _descends(store, best, mine):
            return False, "this node's chain goes on from what the seeds hold: nothing to heal"
        if not chain_signed_by(store, best, mine, st["nid"]):
            return False, f"the seeds' chain up to {best[:8]} isn't all signed by this node's key: left as it is"
        kept = ""
        if mine:
            kept = f"refs/legion-backup/sigrefs-{int(time.time())}-{mine[:12]}"
            if _git(repo, "fetch", "-q", "--no-tags", store, f"+{mine}:{kept}").returncode and \
                    _git(repo, "fetch", "-q", "--no-tags", store, f"+{ref}:{kept}").returncode:
                return False, "nowhere to keep what the ref holds: left as it is"
            if _git(repo, "rev-parse", "-q", "--verify", kept).stdout.strip() != mine:
                return False, "the ref moved while it was being kept: left as it is"
        # moved only from what it held: a publish that signed meanwhile keeps its own
        if _git(store, "update-ref", ref, best, mine or "0" * 40).returncode:
            return False, "the ref moved meanwhile (a publish signed): left as it is"
        _git(repo, "config", "legion.healed-to", best)
        _git(repo, "config", "legion.healed-backup", kept)
        host = next(s["seed"] for s in st["seeds"] if s["tip"] == best)
        return True, (f"this node's signed refs went on from {best[:8]}, what {host} holds of them"
                      + (f"; what they held ({mine[:8]}) is kept in {kept}" if kept else ""))
    finally:
        for r in _git(store, "for-each-ref", "--format=%(refname)", "refs/legion-heal/").stdout.split():
            _git(store, "update-ref", "-d", r)


def unheal(repo) -> tuple[bool, str]:
    """Put back what this node's signed refs held before the last heal, if they still hold what it left."""
    to, kept = _cfg(repo, "legion.healed-to"), _cfg(repo, "legion.healed-backup")
    if not to:
        return False, "no heal to undo here"
    if not kept:
        return False, "there were no signed refs before the heal: nothing to put back"
    back = _git(repo, "rev-parse", "-q", "--verify", kept).stdout.strip()
    if not back:
        return False, f"the copy {kept} isn't in this clone any more"
    nid = _cfg(repo, "legion.nid") or node_id(repo)
    store, ref = storage(repo), f"refs/namespaces/{nid}/refs/rad/sigrefs"
    now = _git(store, "rev-parse", "-q", "--verify", ref).stdout.strip()
    if now != to:
        return False, (f"the signed refs went on since the heal ({now[:8]}, not {to[:8]}): something signed on "
                       "top of them, left as they are")
    temp = "refs/legion-heal/undo"
    try:
        if _git(store, "fetch", "-q", "--no-tags", os.path.abspath(repo), f"+{kept}:{temp}").returncode:
            return False, "couldn't bring the copy back into the storage"
        if _git(store, "update-ref", ref, back, to).returncode:
            return False, "the ref moved meanwhile (a publish signed): left as it is"
    finally:
        _git(store, "update-ref", "-d", temp)
    _git(repo, "config", "--unset", "legion.healed-to")
    _git(repo, "config", "--unset", "legion.healed-backup")
    return True, f"this node's signed refs are back at {back[:8]}, what they held before the heal"


def heal_off() -> bool:
    return os.environ.get("LEGION_HEAL", "").lower() in ("off", "0", "no")


def heal_sigrefs(repo, force: bool = False) -> str | None:
    """heal(), at most once an hour unless forced: what it did, or None. LEGION_HEAL=off turns it off, forced
    or not."""
    if heal_off():
        return None
    if IS_WINDOWS and not native(repo):
        return None  # a storage inside WSL: not reachable from here, and no sandbox resets it
    last = float(_cfg(repo, "legion.healed") or 0)
    if not force and time.time() - last < HEAL_EVERY:
        return None
    subprocess.run(["git", "config", "legion.healed", str(time.time())], cwd=repo, capture_output=True)
    did, said = heal(repo)
    return said if did else None


def run_publish(repo) -> None:
    from .coordgit import publish_radicle
    from .identity import Identity
    global ANNOUNCE_INLINE
    ANNOUNCE_INLINE = True
    ident = Identity.load(repo=repo)
    if ident is None:
        return
    # A node the sandbox stopped can't announce, nor serve what it announces: pushed, the events stayed in its
    # storage. Only the network fetch started it, in a process of its own the sandbox killed too.
    t0 = time.time()
    try:
        healed = heal_sigrefs(repo)
        if healed:
            print(f"legion: {healed}: a storage rebuilt from an older copy had forked them, and the seeds dropped "
                  "what it published", file=sys.stderr, flush=True)
    except (RadError, OSError, subprocess.SubprocessError):
        pass
    timed("heal check of the signed refs", t0)
    t0 = time.time()
    try:
        if not node_running(repo):
            ensure_node(repo)
            wait_connected(repo)
    except RadError:
        pass
    timed("node up", t0)
    t0 = time.time()
    _rounds(repo, "publish", lambda: publish_radicle(repo, ident))
    timed("publish, all of it", t0)


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


# Each fetch into the node's storage leaves a pack of its own, and nothing compacts them: instinct's machine `e` had
# 980 packs (1.24 GB, its storage four times jose's with the same in it), a fetch from it had to look through all
# of them, and no seed finished one before giving up. After a network fetch, past MAX_PACKS, they're compacted
# in steps that each stay done: one index over all the packs (lookups are quick at once, however many there are),
# then batches of small packs merged into one, 40 MB at a time. Where a command waits for it (a sandbox), only the
# index: on `e` it took under a second and was enough for the seeds, while even 40 MB batches outlasted a
# command, and a whole repack of 1.2 GB, cut off, kept nothing.
MAX_PACKS = 64


def store_git(repo, store: str, *args: str, timeout: float) -> int:
    """git on the node's storage, where it lives: inside WSL through WSL's git. Its exit code; None-like 124 when
    out of time."""
    try:
        if IS_WINDOWS and not native(repo):
            return run("git", "-C", store, *args, repo=repo, check=False, timeout=timeout).returncode
        return subprocess.run(["git", "-C", store, *args], capture_output=True, timeout=timeout).returncode
    except (subprocess.TimeoutExpired, RadError):
        return 124


def pack_count(repo, store: str) -> int:
    if IS_WINDOWS and not native(repo):
        out = run("sh", "-c", 'ls "$1"/objects/pack/*.pack 2>/dev/null | wc -l', "sh", store, repo=repo, check=False,
                  timeout=60).stdout.strip()
        return int(out) if out.isdigit() else 0
    return len(list((Path(store) / "objects" / "pack").glob("*.pack")))


def unindexed_packs(repo, store: str) -> list[Path]:
    """The packs of the node's storage that lost their index: git can't see what they hold."""
    if IS_WINDOWS and not native(repo):
        return []  # inside WSL: git there deletes what it means to, nothing holds the files open
    return [p for p in (Path(store) / "objects" / "pack").glob("pack-*.pack") if not p.with_suffix(".idx").exists()]


def repair_packs(repo, store: str) -> int:
    """Index again the packs that lost their index (see deletes_safely). How many it did."""
    fixed = 0
    for p in unindexed_packs(repo, store):
        try:
            done = subprocess.run(["git", "-C", store, "index-pack", str(p)], capture_output=True, timeout=1800)
        except subprocess.TimeoutExpired:
            break
        fixed += done.returncode == 0
    return fixed


def deletes_safely(repo) -> bool:
    """Whether git may delete packs from the node's storage now. On Windows a file that's open can't be deleted,
    and a running node holds its packs open: in Erdős #902 jose's storage was compacted while its node ran, git
    deleted the old packs' indexes but not the packs, and 209 of 215 packs were left with no index. Every ref but
    a few pointed at objects git couldn't find, the node couldn't check the project's identity in anything it was
    sent, and it took nothing in for four days. There, packs go only while the node is stopped."""
    return not (IS_WINDOWS and native(repo)) or not node_running(repo)


def compact_storage(repo, seconds: float = 100, merge: bool = True) -> bool:
    """Repack the node's storage if it holds more than MAX_PACKS packs. True if it did. Inside WSL too: skipped
    there, its packs piled up like anywhere else, and the fetches from it grew slow enough for the seeds to give
    up. Packs that lost their index are indexed again first."""
    store = storage(repo)
    repair_packs(repo, store)
    if pack_count(repo, store) <= MAX_PACKS:
        return False
    end = time.time() + seconds
    steps = [["multi-pack-index", "write"]]
    if merge and deletes_safely(repo):
        steps += [["multi-pack-index", "repack", "--batch-size=40m"], ["multi-pack-index", "expire"]]
    for step in steps:
        if store_git(repo, store, *step, timeout=max(1.0, end - time.time())):
            return False  # what's done stays done: it goes on from there next time
    return True


GC_EVERY = 86400


def gc_storage(repo) -> bool:
    """Once a day, where a sync process stays: drop from the node's storage what no ref holds any more (dropped
    saves, judged patches) and pack the rest. Refs going doesn't free the disk by itself. True if it ran."""
    if time.time() - float(_cfg(repo, "legion.gced") or 0) < GC_EVERY or not deletes_safely(repo):
        return False
    subprocess.run(["git", "config", "legion.gced", str(time.time())], cwd=repo, capture_output=True)
    return store_git(repo, storage(repo), "gc", "--quiet", "--prune=2.weeks.ago", timeout=1800) == 0


def seed_everyone(repo) -> None:
    """Have the node replicate every namespace of the project (`rad seed --scope all`), once per clone. Only a
    project's own seed was told to: the others had whatever their `rad clone` or `rad init` left, and a node that
    follows only the delegates keeps nobody else's events, nor serves them."""
    project = rid(repo)
    if not project or _cfg(repo, "legion.scoped") == project:
        return
    try:
        if run("rad", "seed", project, "--scope", "all", repo=repo, check=False, timeout=60).returncode == 0:
            subprocess.run(["git", "config", "legion.scoped", project], cwd=repo, capture_output=True)
    except (RadError, OSError, subprocess.SubprocessError):
        pass


CONNECT_WAIT = 20  # seconds a node just started is given to connect to a peer


def wait_connected(repo, seconds: float = CONNECT_WAIT) -> bool:
    """Wait until the node just started has a session with some peer, up to `seconds`. Five seconds blind were
    too few on a slow network (the fetch then found nobody) and too many on a quick one."""
    end = time.time() + seconds
    while True:
        try:
            out = rad(repo, "node", "status", check=False, timeout=15)
            if "✓" in out:
                return True
        except (RadError, OSError, subprocess.SubprocessError):
            pass
        if time.time() >= end:
            return False
        time.sleep(1)


def run_network_sync(repo, quick: bool = False) -> bool:
    """What the background process does: make sure the node runs, and fetch from the network."""
    from . import tunnel
    started, ok = time.time(), False
    try:
        try:
            if not node_running(repo):  # stopped (a reboot, a suspended computer): start it
                if not quick and IS_WINDOWS and native(repo):
                    upkeep(repo, quick)  # nothing holds the packs open: the only time they can go (deletes_safely)
                ensure_node(repo)
                wait_connected(repo)
            seed_everyone(repo)
            tunnel.keep(repo)  # the direct tunnels to the other nodes, if this clone uses them: relaunched if they died
            ok = network_sync(repo, force=True, quick=quick)
        except RadError:
            ok = False
        if not ok:  # the node couldn't fetch (a broken storage, no peer answering): the seeds over HTTPS
            try:
                ok = fetch_from_seeds(repo)
            except (RadError, OSError, subprocess.SubprocessError):
                pass
    finally:
        # The fetch is over: said before the storage's upkeep, which took up to five minutes more and held every
        # fetch after it off meanwhile, the node seeing nothing new.
        _write_netsync(repo, {"started": started, "ended": time.time(), "running": False, "ok": ok})
    if ok and not quick:
        # What it brought is in the node's storage: the sync process copies it now, not at its next round,
        # which had copied what the storage held before this fetch began, one round late.
        from . import daemon
        if daemon.enabled():
            daemon.wake()
    upkeep(repo, quick)
    return ok


def upkeep(repo, quick: bool) -> None:
    """Compact the node's storage (and gc it, when nothing waits): one process at a time, the others skip it."""
    from .locks import held
    try:
        with held(_netsync_file(repo, "upkeep").with_suffix(".lock"), wait=0) as got:
            if not got:
                return
            compact_storage(repo, 20 if quick else 300, merge=not quick)
            if not quick:
                gc_storage(repo)
    except (RadError, OSError, subprocess.SubprocessError):
        pass


# Where no sync process lives (a sandbox kills what a command leaves running, the node included), the fetch kicked
# in the background died with the command, and the node lived only seconds: in Erdős #902 instinct's clones got
# nothing from the nodes they weren't connected to in those seconds for days, though they published. So there a
# command fetches itself, waiting: at most every SANDBOX_FETCH_EVERY seconds, and only when it has nothing of its
# own to publish (that goes first), so it stays within agents' tools' time limits.
SANDBOX_FETCH_EVERY = 180


def fetch_due(repo, every: float = SANDBOX_FETCH_EVERY) -> bool:
    """No network fetch finished in the last `every` seconds."""
    return time.time() - netsync_status(repo).get("ended", 0) >= every


def fetch_inline(repo) -> bool:
    """A network fetch this command waits for (see SANDBOX_FETCH_EVERY). True if it reached the network. From the
    seeds over HTTPS (see fetch_from_seeds): through the node it started the node, waited for it to connect and
    for `rad sync --fetch`, a minute in all, and Instinct's sandboxes, whose tools stop a command at 60 s, saw
    every claim fail while the fetch itself failed too."""
    started = time.time()
    ok = fetch_from_seeds(repo)
    _write_netsync(repo, {"started": started, "ended": time.time(), "running": False, "ok": ok})
    return ok


SEED_BUDGET = 15  # seconds a fetch from the seeds over HTTPS may take, all seeds together (with PUBLISH_WAIT, < 60)


def fetch_from_seeds(repo, budget: float = SEED_BUDGET) -> bool:
    """Every other node's namespace as the public seeds hold it (HEAL_SEEDS), over HTTPS and into the node's
    storage, where the next sync copies it into the clone as from any fetch: no node has to be up, connected, or
    hear of it by gossip. A sandbox's node lives seconds: in Erdős #902 instinct's machines got the nodes they met
    in those seconds and nobody else's, and for five days nothing from jose's node or opencode's, which the seeds
    had. A namespace is taken only as the node itself signed it (every new commit of its signed refs, see
    chain_signed_by), only where it goes on from what the storage has, and with exactly the refs it signed.
    This node's own namespace is never touched (heal_sigrefs is for that). True if a seed answered."""
    if IS_WINDOWS and not native(repo):
        return False  # inside WSL: its node fetches as ever
    end = time.time() + budget
    own = _cfg(repo, "legion.nid") or node_id(repo)
    store = storage(repo)
    for url in heal_urls(repo):
        if end - time.time() < 5:
            break
        try:
            p = _git(store, "ls-remote", url, timeout=max(5.0, min(20.0, end - time.time())))
        except subprocess.TimeoutExpired:
            continue
        if p.returncode:
            continue
        remote = {}
        for line in p.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            m = re.match(r"refs/namespaces/([^/]+)/(refs/.+)$", ref)
            if m and m.group(1) != own:
                remote.setdefault(m.group(1), {})[m.group(2)] = sha
        local = _storage_values(repo)
        sig = "refs/rad/sigrefs"
        changed = [n for n, refs in remote.items()
                   if refs.get(sig) and refs[sig] != local.get(f"refs/namespaces/{n}/{sig}")]
        if changed and end - time.time() > 3:
            take(repo, store, url, changed, remote, local, end)
        return True
    return False


def take(repo, store: str, url: str, nids: list[str], remote: dict, local: dict, end: float) -> list[str]:
    """Bring these namespaces from a seed into the storage (see fetch_from_seeds). The ones it took."""
    sig, temp, took = "refs/rad/sigrefs", "refs/legion-seed/", []
    try:
        try:  # one fetch for all of them, under a prefix of its own: nothing of the namespaces moves yet
            _git(store, "fetch", "-q", "--no-tags", "--no-write-fetch-head", url,
                 *[f"+refs/namespaces/{n}/*:{temp}{n}/*" for n in nids], timeout=max(3.0, end - time.time()))
        except subprocess.TimeoutExpired:
            return took
        for n in nids:
            new, old = remote[n][sig], local.get(f"refs/namespaces/{n}/{sig}")
            if _git(store, "rev-parse", "-q", "--verify", f"{temp}{n}/{sig}").stdout.strip() != new:
                continue  # it moved at the seed meanwhile, or didn't come: the next fetch
            if old and not _descends(store, old, new):
                continue  # behind what the storage has, or another branch: Radicle's own fetch decides
            if not chain_signed_by(store, new, old or "", n):
                continue
            listed = _git(store, "cat-file", "blob", f"{new}:refs")
            signed = dict(reversed(line.split(" ", 1)) for line in listed.stdout.splitlines() if " " in line)
            signed.pop("refs/rad/sigrefs-parent", None)  # signed, but not a ref: the chain's previous commit
            if listed.returncode or not signed:
                continue
            # bytes in: as text they'd go in with Windows' line ends, which git takes for part of the line
            check = subprocess.run(["git", "-C", store, "cat-file", "--batch-check"], capture_output=True,
                                   input="".join(f"{o}\n" for o in signed.values()).encode())
            if b"missing" in check.stdout:
                continue
            prefix = f"refs/namespaces/{n}/"
            lines = [f"update {prefix}{sig} {new} {old or '0' * 40}\n"]  # only from what it held
            lines += [f"update {prefix}{r} {o}\n" for r, o in signed.items()]
            lines += [f"delete {ref}\n" for ref in local if ref.startswith(prefix) and ref != prefix + sig
                      and ref[len(prefix):] not in signed]
            if not subprocess.run(["git", "-C", store, "update-ref", "--stdin"], capture_output=True,
                                  input="".join(lines).encode()).returncode:  # all of it, or nothing
                took.append(n)
    finally:
        for r in _git(store, "for-each-ref", "--format=%(refname)", temp).stdout.split():
            _git(store, "update-ref", "-d", r)
    return took


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
        # The events first: every node's coord, then the patches. In Erdős #902 a clone thousands of refs behind
        # (muse's, on a slow machine) fetched in name order, patches before peers, its first batch outlasted the
        # fetch's time limit on every round, and for four days no event reached it.
        specs = [f"+{src}:{dest}" for dest, src in sorted(wanted.items(), key=lambda kv: (
                 not kv[0].startswith("refs/legion/peers/"), kv[0]))
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
            if "timed out" in str(e) and len(specs) == 1:
                raise
            if len(specs) > 1:  # a batch too big for the time limit, or one ref in it that fails: halves
                _fetch(repo, specs[:len(specs) // 2], held)
                _fetch(repo, specs[len(specs) // 2:], held)
            else:
                src = specs[0].lstrip("+").split(":", 1)[0]
                with open(_unfetchable_file(repo), "a", encoding="utf-8") as f:
                    f.write(f"{src} {held.get(src, '')}\n")
            return


def _judged_file(repo) -> Path:
    from .coordgit import common_dir
    return common_dir(repo) / "legion-judged"


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
# Set by the process a command waits for (run_publish): its push announces too, as pushes did before the
# announcement went to a process of its own, which sandboxes that kill background processes killed as well.
ANNOUNCE_INLINE = False


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
    if ANNOUNCE_INLINE and own:
        # Where the node lives only as long as the command (a sandbox), this node's own events go first and alone,
        # and it waits until a seed has fetched them: instinct's b and e (400 patches to push, storages of 0.5 and
        # 1.7 GB) were cut off before any seed fetched what they had pushed, and for days their events reached
        # nobody. The seeds pass them on among themselves.
        # Pushed without the helper's own announcement, which waits for the seeds with no bound of its own: on
        # muse's machine it never came back, the publish died at its time limit, and nothing went out for hours.
        # The announcement that follows has one.
        t0 = time.time()
        _push(repo, own, strict=True)
        timed(f"push of this node's own refs ({len(own)})", t0)
        t0 = time.time()
        announce_waiting(repo)
        timed("announcement, waiting for a seed", t0)
        own = []
    try:
        # Everything in one push when it goes through: Radicle's helper spends ten seconds and more on each
        # before it sends anything, and a publish made four. But not when there's a lot besides this node's own
        # events (the first publish after an update deletes hundreds of refs): where a sandbox kills the push
        # when the command returns, the big one never finished, and with it went this node's events.
        if len(rest) > BIG_PUBLISH:
            raise RadError("too much for one push")
        t0 = time.time()
        try:
            _push(repo, own + rest + main, strict=True)
            timed(f"push of the rest ({len(own + rest + main)} refs)", t0)
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
        t0 = time.time()
        _push(repo, own, strict=True)
        for i in range(0, len(rest), 200):  # keeps the command line short with many patches
            _push(repo, rest[i:i + 200], strict=False)
        publish_main(repo)
        timed(f"push of the rest, in batches ({len(rest)} refs)", t0)
    subprocess.run(["git", "config", "legion.published", f"{mark} {time.time()}"], cwd=repo, capture_output=True)
    if not ANNOUNCE_INLINE:
        kick_announce(repo)
    elif rest or main:
        t0 = time.time()
        announce_waiting(repo)  # the patches and main too, as far as a bounded wait goes
        timed("announcement of the rest", t0)


def _push(repo, specs: list[str], strict: bool, sync: bool = False) -> None:
    """Push these refspecs (forced where they start with +) to the node; with `sync`, Radicle's helper also
    announces them and waits for the seeds (see ANNOUNCE_INLINE). Not strict: a batch that fails is split in two
    until what fails is one ref, which is left out (another node re-offers it)."""
    if not specs:
        return
    from .locks import held
    for attempt in range(3):
        try:
            with held(push_lock(repo), wait=PUSH_LOCK_WAIT):
                rgit(repo, "push", "-q", *([] if sync else ["-o", "no-sync"]), "rad", *specs, timeout=PUSH_TIMEOUT)
            return
        except RadError as e:
            if any(s in str(e) for s in RACED) and attempt < 2:  # another clone pushed to the same node meanwhile
                time.sleep(2)
                continue
            if strict or "timed out" in str(e):
                raise
            if len(specs) > 1:
                _push(repo, specs[:len(specs) // 2], strict)
                _push(repo, specs[len(specs) // 2:], strict)
            else:
                print(f"legion: left {specs[0]} out of this push, which the node refused; another node re-offers "
                      "it", file=sys.stderr)
            return


# What a push says when another one, from another clone sharing this node, moved its refs meanwhile.
RACED = ("cannot lock ref", "old reference value does not match")
PUSH_LOCK_WAIT = 180  # seconds a push waits for another to the same node before going anyway (as before the lock)


def push_lock(repo) -> Path:
    """One push at a time to a node per project, whichever clone and LEGION_HOME it comes from. Agents with homes
    of their own (~/.legion-claude, -2, -3, -4 on jose's machine) each had a sync process, all pushing to the one
    node: their pushes refused each other ("old reference value does not match") and timed out after ten minutes
    over and over, and each time that clone's events waited. In line they wait seconds for each other instead."""
    import hashlib
    key = hashlib.sha1((rid(repo) or str(Path(repo).resolve())).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"legion-push-{key}.lock"


def published_mark(repo) -> str:
    """What makes a publish worth its time, as one hash: this node's own latest event (a patch of its own comes
    with one), the official main, and the patches it holds, the others' it fetched among them, which it passes on
    as soon as they arrive. Not the coord, which every sync merges with the others' events."""
    import hashlib
    out = subprocess.run(["git", "for-each-ref", "--format=%(objectname) %(refname)", "refs/legion/own",
                          "refs/legion/main", "refs/legion/patches/"], cwd=repo, capture_output=True).stdout
    # And the patches this clone holds, others' among them: a node passes on what it receives (that's the gossip),
    # and it did only every REPUBLISH_EVERY, ten minutes; now at its next round. publish_specs still pushes only
    # what the node lacks.
    return hashlib.sha1(out).hexdigest()
