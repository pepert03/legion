"""Radicle as legion's transport.

Every repository pushes its legion refs to its Radicle node, which keeps them in the node's namespace and
replicates them through seeds, so nodes behind NAT or on different networks reach each other without
opening ports. Inside the namespace, each legion identity publishes under its own prefix
(refs/legion/pub/<identity>/coord), so several agents on one computer can share one node without
overwriting each other. To sync, legion reads every publisher from the local Radicle storage and merges
the coord branches, as in plain p2p.

Radicle only ships for Linux and macOS. On Windows it runs inside WSL, and legion calls it through
`wsl.exe`; the repository and the agents stay on Windows.
"""

import json
import os
import re
import socket
import subprocess
import sys
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


# --- networks that only let HTTP(S) out ---------------------------------------------------------

BRIDGE = ("127.0.0.1", 1080)
# Radicle's public seeds. Behind a proxy the node is told to always connect to them: one that has been
# failing to reach its bootstrap nodes (before the proxy was set) otherwise waits a long while to retry.
PUBLIC_SEEDS = ["z6Mkmqogy2qEM2ummccUthFEaaHvyYmYBYh3dbe9W4ebScxo@rosa.radicle.network:8776",
                "z6MkrLMMsiPWUcNPHcRajuMi9mDfYckSoJyPwwnknocNYPm7@iris.radicle.network:8776"]


def _config_path(home: str | None) -> str:
    return f"{home or rad(None, 'path', timeout=30).splitlines()[-1].strip()}/config.json"


def node_proxy(home: str | None = None) -> str | None:
    try:
        return json.loads(run("cat", _config_path(home), home=home, timeout=30).stdout)["node"].get("proxy")
    except (RadError, ValueError, KeyError):
        return None


def set_node_proxy(home: str | None, address: str | None) -> None:
    path = _config_path(home)
    c = json.loads(run("cat", path, home=home, timeout=30).stdout)
    c["node"]["proxy"] = address
    if address and c["node"].get("network", "main") == "main":
        connect = c["node"].get("connect") or []
        c["node"]["connect"] = connect + [s for s in PUBLIC_SEEDS if s not in connect]
    Path(path).write_text(json.dumps(c, indent=2), encoding="utf-8")


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


def restart_node(home: str | None = None) -> None:
    """Stop the node and make sure it's gone, so the next start reads the config again."""
    run("rad", "node", "stop", home=home, check=False, timeout=60)
    for _ in range(30):
        if not node_running(home=home):
            return
        time.sleep(0.5)
    raise RadError("the Radicle node didn't stop, so it can't pick up the proxy setting; stop it "
                   "(`rad node stop`, or kill radicle-node) and run this again")


def node_log(home: str | None = None, lines: int = 400) -> list[str]:
    path = f"{home or rad(None, 'path', timeout=30).splitlines()[-1].strip()}/node/node.log"
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
        network = json.loads(run("cat", _config_path(home), home=home, timeout=30).stdout)["node"].get("network")
    except (RadError, ValueError, KeyError):
        network = None
    if network not in (None, "main"):
        return []
    return [arg for s in PUBLIC_SEEDS for arg in ("--seed", s.split("@")[0])]


def storage(repo) -> str:
    home = rad(repo, "path", timeout=30).splitlines()[-1].strip()
    return f"{home}/storage/{rid(repo)[4:]}"


def _storage_refs(repo) -> list[str]:
    return run("git", "--git-dir", storage(repo), "for-each-ref", "--format=%(refname)", "refs/namespaces/",
               repo=repo, timeout=60).stdout.splitlines()


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
    p = run("rad", "sync", "--fetch", rid(repo), repo=repo, check=False, timeout=45)
    if p.returncode and "seeds" in clean(p.stderr + p.stdout):  # an empty routing table: ask the public seeds
        p = run("rad", "sync", "--fetch", rid(repo), *seed_args(_cfg(repo, "legion.radhome")), repo=repo,
                check=False, timeout=45)
    ok = p.returncode == 0
    subprocess.run(["git", "config", "legion.radsynced", str(time.time())], cwd=repo, capture_output=True)
    return ok


def fetch_all(repo) -> list[str]:
    """Copy every publisher's legion refs from the Radicle storage into this repository."""
    tidy(repo)  # the fetch runs in WSL too: packed, it reads a few files instead of thousands
    refs = _storage_refs(repo)
    pubs = publishers(repo, refs)
    if pubs:
        specs = [f"+{prefix}*:refs/legion/peers/{name}/*" for name, prefix in pubs.items()]
        # Every node re-publishes the patches it has seen (that's the gossip), so one patch can sit in
        # several namespaces. Patches are immutable, so take each one from the first namespace that has it.
        chosen, gone = {}, judged(repo)
        for ref in refs:
            m = re.match(r"refs/namespaces/[^/]+/(refs/legion/patches/.+)$", ref)
            if m and m.group(1) not in chosen and m.group(1) not in gone:
                chosen[m.group(1)] = ref
        specs += [f"+{src}:{dest}" for dest, src in sorted(chosen.items())]
        for attempt in range(3):
            try:
                rgit(repo, "fetch", "-q", "--no-tags", storage(repo), *specs, timeout=120)
                break
            except RadError as e:  # another legion process (a viewer) fetching into the same refs right now
                if "cannot lock ref" not in str(e) or attempt == 2:
                    raise
                time.sleep(1)
    # Peers that aren't published any more (a repository that moved to per-identity refs): forget them.
    local = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/legion/peers/"], cwd=repo,
                           capture_output=True, text=True).stdout.splitlines()
    for ref in local:
        if ref.split("/")[3] not in pubs:
            subprocess.run(["git", "update-ref", "-d", ref], cwd=repo, capture_output=True)
    return list(pubs)


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


def publish_specs(repo, who: str) -> list[str]:
    """The refspecs that push this repository's legion refs under its identity's own prefix."""
    def listed(prefix):
        return subprocess.run(["git", "for-each-ref", "--format=%(refname)", prefix], cwd=repo,
                              capture_output=True, text=True).stdout.split()

    specs = [f"refs/legion/pub/*:refs/legion/pub/{who}/*"] if listed("refs/legion/pub/") else []
    # Patches by name, not with a pattern: judged ones are deleted as they go, and on Windows the git in WSL
    # can still see a just-deleted ref file under /mnt/c, expand the pattern to it and fail the whole push.
    gone = judged(repo)
    return specs + [f"{ref}:{ref}" for ref in listed("refs/legion/patches/") if ref not in gone]


def publish_main(repo) -> None:
    """Move the repository's ordinary `main` branch, the one Radicle's web and `rad clone` show, to the
    official main. Without --force: it only moves forward, so a repository with an older view (another agent
    sharing this node, say) can't drag it back, and a refused push is no error."""
    tip = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/legion/main"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    shown = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/remotes/rad/main"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    if tip and tip != shown:
        rgit(repo, "push", "-q", "rad", "refs/legion/main:refs/heads/main", check=False, timeout=120)


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


def publish(repo, who: str) -> None:
    """Push this repository's legion refs to the node, under `who` (its identity's key, 16 hex characters);
    the node announces them to the network."""
    tidy(repo)
    specs = publish_specs(repo, who)
    for i in range(0, len(specs), 200):  # keeps the command line short with many patches
        for attempt in range(3):
            try:
                rgit(repo, "push", "-q", "--force", "rad", *specs[i:i + 200], timeout=120)
                break
            except RadError as e:  # another clone on this computer pushing to the same node right now
                if "cannot lock ref" not in str(e) or attempt == 2:
                    raise
                time.sleep(2)
    publish_main(repo)
