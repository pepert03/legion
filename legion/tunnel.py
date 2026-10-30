"""Direct tunnels between the agents' Radicle nodes, through NAT, with dumbpipe.

Nodes behind NAT only reach each other through Radicle's public seeds, which pass what one node published on to
the others when they get to it: in Erdős #902 an event took 3 minutes to reach one machine and 9 to another.
dumbpipe (iroh) opens a QUIC connection between two machines by hole punching, or through n0's free public
relays when that fails, with no account and no open port, and forwards a TCP port over it. So:

- a node that exposes itself runs `dumbpipe listen-tcp` in front of its Radicle port, and publishes its ticket
  (how to reach it) in a node.tunnel event;
- a node that connects runs `dumbpipe connect-tcp` for each ticket it uses, which gives it a local port leading
  to the other node, and tells its own node to connect there (NID@127.0.0.1:PORT). Once connected, the two
  nodes gossip directly: what one pushes, the other hears of and fetches at once.

What a clone does is its mode (`legion radicle tunnel MODE`, in git config legion.tunnel):
- hub: expose this node as the project's hub (only an integrator's hub counts as one);
- client: connect to the hubs, and fetch from them first;
- mesh: expose this node, and connect to the hubs and to up to PEERS other active nodes;
- off: nothing.

The tunnels are processes of their own, which the background network fetch (radicle.run_network_sync, which the
machine's sync process or the commands start at most every minute) checks and relaunches: where background
processes are killed every half hour (agents' sandboxes), the next command brings them back. dumbpipe's key stays
in the node's folder, so a node's ticket is the same after a restart, and its event goes out once.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import radicle
from .radicle import IS_WINDOWS, RadError

MODES = ("hub", "client", "mesh", "off")
PEERS = int(os.environ.get("LEGION_TUNNEL_PEERS", 8))  # at most this many other nodes, besides the hubs
ACTIVE = 12 * 3600  # a node whose agent did nothing for this long isn't connected to (hubs always are)
PORTS = (41000, 42000)  # the local ports of the tunnels to other nodes: a hash of the NID picks one
TICKET_RE = re.compile(r"dumbpipe connect-tcp (\w+)")


def binary() -> str | None:
    found = shutil.which("dumbpipe")
    if found:
        return found
    cargo = Path.home() / ".cargo" / "bin" / ("dumbpipe.exe" if IS_WINDOWS else "dumbpipe")
    return str(cargo) if cargo.exists() else None


def mode(repo) -> str:
    m = (radicle._cfg(repo, "legion.tunnel") or "off").lower()
    return m if m in MODES else "off"


def rad_home(repo) -> Path:
    return Path(radicle._cfg(repo, "legion.radhome") or radicle.rad(repo, "path", timeout=30).splitlines()[-1].strip())


# --- what runs on this node: in its folder, shared by the clones that use the node ------------------------


def _file(home: Path, name: str) -> Path:
    return Path(home) / "node" / name


def load(home: Path) -> dict:
    """{key: dumbpipe's secret, listen: {pid, port, ticket}, peers: {nid: {pid, port, ticket}}, announced}"""
    try:
        return json.loads(_file(home, "legion-tunnels.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(home: Path, st: dict) -> None:
    p = _file(home, "legion-tunnels.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1), encoding="utf-8")


def alive(pid) -> bool:
    """Whether this dumbpipe still runs: the number may belong to another program by now."""
    if not pid:
        return False
    if IS_WINDOWS:
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                text=True).stdout
        return "dumbpipe" in listed.lower()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return b"dumbpipe" in Path(f"/proc/{pid}/cmdline").read_bytes()  # empty for a zombie
    except OSError:
        return "dumbpipe" in subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True,
                                            text=True).stdout


def kill(pid) -> None:
    if not alive(pid):
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


def _taken(port: int) -> bool:
    with socket.socket() as s:
        if IS_WINDOWS:  # otherwise Windows lets a second socket bind a port another one listens on
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _free_port(start: int, end: int) -> int:
    for port in [*range(start, end), *range(PORTS[0], start)]:
        if not _taken(port):
            return port
    raise RadError(f"no free local port between {PORTS[0]} and {end} for a tunnel")


def _port_for(home: Path, nid: str) -> int:
    h = int(hashlib.sha1(f"{home} {nid}".encode()).hexdigest(), 16)
    return PORTS[0] + h % (PORTS[1] - PORTS[0])


# --- exposing this node -----------------------------------------------------------------------------------


def ensure_listening(repo, home: Path) -> int:
    """The port this node listens on, for its tunnel. A node set up as usual doesn't listen at all: it's given
    one on 127.0.0.1 (nothing outside this machine reaches it but the tunnel), and restarted to take it."""
    path = Path(home) / "config.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    for addr in c["node"].get("listen") or []:
        return int(str(addr).rsplit(":", 1)[1])
    port = 8776 if not _taken(8776) else _free_port(8777, 8876)
    c["node"]["listen"] = [f"127.0.0.1:{port}"]
    path.write_text(json.dumps(c, indent=2), encoding="utf-8")
    rhome = radicle._cfg(repo, "legion.radhome")
    if radicle.node_running(repo, rhome):
        radicle.restart_node(rhome)
    radicle.ensure_node(repo, rhome)
    return port


def _wait_ticket(log: Path, proc, wait: float = 30) -> str:
    """The ticket dumbpipe prints on starting; with -v, the short one (its ID and relay) comes last."""
    end, first = time.time() + wait, None
    while time.time() < end:
        try:
            found = TICKET_RE.findall(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            found = []
        if len(found) >= 2 or (found and first and time.time() - first > 2):
            return found[-1]
        if found and not first:
            first = time.time()
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    tail = log.read_text(encoding="utf-8", errors="replace")[-400:] if log.exists() else ""
    raise RadError(f"dumbpipe gave no ticket for this node in {wait:.0f} s: {tail.strip()}")


def expose(repo, home: Path) -> str:
    """Run this node's tunnel, if it isn't running; returns its ticket."""
    port = ensure_listening(repo, home)
    st = load(home)
    me = st.get("listen") or {}
    if alive(me.get("pid")) and me.get("port") == port and me.get("ticket"):
        return me["ticket"]
    kill(me.get("pid"))
    key = st.get("key") or secrets.token_hex(32)  # kept, so the ticket stays the same from one start to the next
    log = _file(home, "legion-tunnel.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")  # the ticket is read from it
    proc = radicle.spawn_detached([binary(), "listen-tcp", "--host", f"127.0.0.1:{port}", "-v"], str(home), log=log,
                                  env={**os.environ, "IROH_SECRET": key})
    ticket = _wait_ticket(log, proc)
    st = load(home)
    st.update(key=key, listen={"pid": proc.pid, "port": port, "ticket": ticket, "started": time.time()})
    save(home, st)
    return ticket


def announce(repo, home: Path, st, me: str, ticket: str, hub: bool) -> bool:
    """Publish this node's ticket, unless the project already has it. True if an event went out."""
    from . import coordgit, daemon, locks
    from . import events as E
    from .identity import Identity
    last = st.tunnels.get(me)
    if last and last.get("ticket") == ticket and bool(last.get("hub")) == hub:
        return False
    node = load(home)
    sent = node.get("announced") or {}
    if sent.get("ticket") == ticket and sent.get("hub") == hub and time.time() - sent.get("at", 0) < 3600:
        return False  # another clone on this node announced it a moment ago: it's on its way
    ident = Identity.load(repo=repo)
    if ident is None:
        return False
    ev = E.create(ident, "node.tunnel", st.max_lamport + 1, nid=me, ticket=ticket, hub=hub)
    with locks.held(daemon.repo_lock(repo)):
        coordgit.append(repo, ident, ev)
    node["announced"] = {"ticket": ticket, "hub": hub, "at": time.time()}
    save(home, node)
    if daemon.alive():
        daemon.wake()
    else:
        radicle.kick_publish(repo)
    return True


# --- connecting to the others -----------------------------------------------------------------------------


def is_hub(st, ev: dict) -> bool:
    return bool(ev.get("hub")) and ev["author"] in st.integrators


def choose(st, me: str, mode_: str, now: float) -> list[str]:
    """The nodes to connect to: the hubs, and in a mesh up to PEERS active others. Each node picks its own
    order (a hash of both IDs), so the connections spread over every node instead of piling on the same few."""
    hubs, others = [], []
    for nid, ev in st.tunnels.items():
        if nid == me:
            continue
        if is_hub(st, ev):
            hubs.append(nid)
        elif now - max(st.last_seen.get(ev["author"], 0), ev["ts"]) <= ACTIVE:
            others.append(nid)
    if mode_ != "mesh":
        return sorted(hubs)
    others.sort(key=lambda nid: hashlib.sha1(f"{me} {nid}".encode()).hexdigest())
    return sorted(hubs) + others[:PEERS]


def connect(home: Path, nid: str, ticket: str) -> int:
    """Run the tunnel to that node, if it isn't running; returns its local port."""
    st = load(home)
    t = st.get("peers", {}).get(nid) or {}
    if alive(t.get("pid")) and t.get("ticket") == ticket:
        return t["port"]
    kill(t.get("pid"))
    start = t.get("port") or _port_for(home, nid)
    port = start if not _taken(start) else _free_port(start + 1, PORTS[1])
    log = _log(home, nid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")  # read by reached(): only this run's attempts
    proc = radicle.spawn_detached([binary(), "connect-tcp", "--addr", f"127.0.0.1:{port}", ticket], str(home),
                                  log=log, env={**os.environ, "RUST_LOG": "dumbpipe=info", "NO_COLOR": "1"})
    end = time.time() + 15
    while not _taken(port) and proc.poll() is None and time.time() < end:
        time.sleep(0.2)
    if proc.poll() is not None or not _taken(port):
        kill(proc.pid)
        raise RadError(f"dumbpipe didn't open the tunnel to {nid[-8:]}; see {log}")
    st = load(home)
    st.setdefault("peers", {})[nid] = {"pid": proc.pid, "port": port, "ticket": ticket, "started": time.time()}
    save(home, st)
    return port


def _log(home: Path, nid: str) -> Path:
    return _file(home, f"legion-tunnel-{nid[-8:]}.log")


ATTEMPT_RE = re.compile(r"^(\S+Z)\s.*got tcp connection", re.M)
FAILED_RE = re.compile(r"error handling connection")


def reached(home: Path, nid: str, now: float, wait: float = 60) -> bool:
    """Whether the node's latest attempt through this tunnel got to the other end: dumbpipe logs each connection
    it forwards, and an error when it can't reach the other node. An attempt `wait` seconds old with no error
    after it went through."""
    from datetime import datetime
    try:
        text = radicle.clean(_log(home, nid).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    attempts = list(ATTEMPT_RE.finditer(text))
    if not attempts or FAILED_RE.search(text, attempts[-1].end()):
        return False
    try:
        at = datetime.fromisoformat(attempts[-1].group(1).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return now - at > wait


def disconnect(home: Path, nid: str) -> None:
    st = load(home)
    t = st.get("peers", {}).pop(nid, None)
    if t:
        kill(t.get("pid"))
        save(home, st)


def connected(repo) -> set[str]:
    """The nodes this node has a session with now."""
    out = radicle.rad(repo, "node", "status", check=False, timeout=30)
    return {m for line in out.splitlines() if "✓" in line for m in re.findall(r"z6Mk\w+", line)}


def link(repo, nid: str, port: int, hub: bool, sessions: set[str]) -> str | None:
    """Have this node connect to that one through its tunnel; a hub is also kept as the preferred seed."""
    addr = f"{nid}@127.0.0.1:{port}"
    if hub and radicle._cfg(repo, "legion.seed") != addr:
        radicle.prefer_seed(repo, addr, radicle._cfg(repo, "legion.radhome"), restart=False)
    if nid in sessions:
        return None
    p = radicle.run("rad", "node", "connect", addr, "--timeout", "15s", repo=repo, check=False, timeout=30)
    return None if p.returncode == 0 else radicle.clean(p.stderr or p.stdout).strip()[-200:]


# Radicle built for Windows doesn't reconnect by itself to a node it lost: when a tunnel dies and comes back, the
# node's next attempt opens the connection and never sends its handshake, and `rad node connect` takes the node
# as already connected. Freshly started, it connects at once. So a Windows node that has gone STUCK seconds
# without a session to a node whose tunnel is up, and whose last attempt reached the other end, is restarted, at
# most every RESTART_EVERY seconds. A node that is down (agents' sandboxes kill theirs every half hour) fails
# every attempt instead, and restarting this one wouldn't bring it back.
STUCK = 90
RESTART_EVERY = 300


def unstick(repo, home: Path, ports: dict[str, int], sessions: set[str]) -> bool:
    now, st = time.time(), load(home)
    since = {nid: t for nid, t in (st.get("unlinked") or {}).items() if nid in ports and nid not in sessions}
    for nid in ports:
        if nid not in sessions:
            since.setdefault(nid, now)
    st["unlinked"] = since
    stuck = (IS_WINDOWS and radicle.native(repo) and now - st.get("restarted", 0) > RESTART_EVERY
             and any(now - t > STUCK and reached(home, nid, now) for nid, t in since.items()))
    if stuck:
        st.update(restarted=now, unlinked={})
    save(home, st)
    if stuck:
        rhome = radicle._cfg(repo, "legion.radhome")
        radicle.restart_node(rhome)
        radicle.ensure_node(repo, rhome)
    return stuck


# --- keeping it all up ------------------------------------------------------------------------------------


def keep(repo, strict: bool = False) -> list[str]:
    """Make this node's tunnels what the clone's mode asks for: start what isn't running, publish the ticket,
    connect. Returns notes on what it did; errors go into them too (raised if `strict`), and into
    .git/legion-tunnel.json, which `legion radicle tunnel status` shows."""
    from . import coordgit, locks
    m = mode(repo)
    if m == "off":
        return []
    notes = []
    try:
        if not binary():
            raise RadError("dumbpipe isn't installed (`cargo install dumbpipe`, or a binary from "
                           "github.com/n0-computer/dumbpipe/releases on the PATH)")
        if IS_WINDOWS and not radicle.native(repo):
            raise RadError("Radicle runs in WSL here: the tunnels need Radicle and dumbpipe on the same side "
                           "(Radicle built for Windows, legion.radnative)")
        home = rad_home(repo)
        with locks.held(_file(home, "legion-tunnels.lock"), wait=90):
            me = radicle.node_id(repo)
            st = coordgit.state(repo)
            if m in ("hub", "mesh"):
                ticket = expose(repo, home)
                if announce(repo, home, st, me, ticket, hub=m == "hub"):
                    notes.append("published this node's ticket")
            wanted = choose(st, me, m, time.time())
            sessions = connected(repo) if wanted else set()
            ports = {}
            for nid in wanted:
                ev = st.tunnels[nid]
                try:
                    ports[nid] = connect(home, nid, ev["ticket"])
                    why = link(repo, nid, ports[nid], is_hub(st, ev), sessions)
                    if why:
                        notes.append(f"{nid[-6:]}: the node didn't connect yet ({why})")
                except RadError as e:
                    notes.append(f"{nid[-6:]}: {e}")
            if unstick(repo, home, ports, sessions):
                notes.append("restarted the node: it hadn't reconnected through a tunnel that came back")
                for nid, port in ports.items():
                    link(repo, nid, port, is_hub(st, st.tunnels[nid]), set())
            # Tunnels to nodes gone quiet, or (in a mesh, which picks them) no longer picked: closed.
            keep_ = set(wanted) | (set(choose(st, me, "mesh", time.time())) if m != "mesh" else set())
            for nid in list(load(home).get("peers", {})):
                if nid not in keep_:
                    disconnect(home, nid)
    except Exception as e:  # noqa: BLE001 (a tunnel's trouble mustn't stop the network fetch it runs in)
        notes.append(f"error: {e}")
        if strict:
            _status(repo, m, notes)
            raise RadError(str(e)) from e
    _status(repo, m, notes)
    return notes


def _status(repo, m: str, notes: list[str]) -> None:
    radicle._write_netsync(repo, {"at": time.time(), "mode": m, "notes": notes}, "tunnel")


def off(repo) -> None:
    """Close this node's tunnels and forget the hubs as seeds."""
    subprocess.run(["git", "config", "legion.tunnel", "off"], cwd=repo, capture_output=True)
    home = rad_home(repo)
    st = load(home)
    kill((st.get("listen") or {}).get("pid"))
    for t in (st.get("peers") or {}).values():
        kill(t.get("pid"))
    st.pop("listen", None)
    st["peers"] = {}
    save(home, st)
    path = home / "config.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    local = "@127.0.0.1:"
    c["node"]["connect"] = [s for s in c["node"].get("connect") or [] if local not in s]
    c["preferredSeeds"] = [s for s in c.get("preferredSeeds") or [] if local not in s]
    path.write_text(json.dumps(c, indent=2), encoding="utf-8")
    if local in (radicle._cfg(repo, "legion.seed") or ""):
        subprocess.run(["git", "config", "--unset", "legion.seed"], cwd=repo, capture_output=True)


def status(repo) -> list[str]:
    from . import coordgit
    m = mode(repo)
    lines = [f"Mode: {m}. dumbpipe: {binary() or 'not installed'}."]
    last = radicle.netsync_status(repo, "tunnel")
    if last:
        ago = time.time() - last.get("at", 0)
        lines.append(f"Last checked {ago:.0f} s ago" + (": " + "; ".join(last["notes"]) if last.get("notes") else "."))
    try:
        home = rad_home(repo)
        me = radicle.node_id(repo)
    except RadError as e:
        return lines + [str(e)]
    node = load(home)
    mine = node.get("listen") or {}
    if mine:
        lines.append(f"This node ({me[-6:]}) is exposed on port {mine.get('port')}: "
                     f"{'running' if alive(mine.get('pid')) else 'not running'}, ticket {mine.get('ticket', '')[:24]}…")
    st = coordgit.state(repo)
    sessions = connected(repo) if st.tunnels else set()
    names = st.names
    for nid, ev in sorted(st.tunnels.items()):
        t = (node.get("peers") or {}).get(nid)
        what = "this node" if nid == me else (
            ("tunnel up" if t and alive(t.get("pid")) else "tunnel down" if t else "no tunnel")
            + (", connected" if nid in sessions else ""))
        seen = time.time() - max(st.last_seen.get(ev["author"], 0), ev["ts"])
        lines.append(f"  {nid[-6:]}{' (hub)' if is_hub(st, ev) else ''} from {names.get(ev['author'], '?')}, "
                     f"active {seen / 3600:.1f} h ago: {what}" + (f", port {t['port']}" if t else ""))
    if not st.tunnels:
        lines.append("No node of this project has published a tunnel yet.")
    return lines
