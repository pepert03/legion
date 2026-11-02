"""The network commands: Radicle (its node, seeds, tunnels), peers, the git server, and this machine's sync
process. They carry the project; they don't coordinate it. `legion-net` runs them on their own, and `legion`
still has them, for the agents and scripts that call them there.
"""

import argparse
import re
import socket
import subprocess
import sys
import unicodedata

from . import coordgit
from .ctx import Fail, need_identity, repo_root
from .identity import Identity


def radicle_ready(alias: str, home: str | None) -> str:
    """Install Radicle if missing, create its key and start the node. Everything an agent can do alone."""
    from . import radicle
    wsl = radicle.IS_WINDOWS and not radicle.native()
    if wsl:
        try:
            radicle.prepare_wsl()
        except (radicle.RadError, FileNotFoundError, subprocess.SubprocessError) as e:
            raise Fail(f"Radicle runs inside WSL on Windows, and WSL isn't ready: {e}. A human may have to run "
                       f"`wsl --install -d {radicle.distro()}` as administrator (and reboot), then run this again.")
    if not radicle.available(home):
        where = f"WSL ({radicle.distro()})" if wsl else "this machine"
        print(f"Installing Radicle in {where} with its official installer…", flush=True)
        try:
            radicle.install()
        except (radicle.RadError, FileNotFoundError) as e:
            hint = (" On Windows, Radicle runs inside WSL: `wsl --install -d Ubuntu-24.04` (a human may have to "
                    "approve it), then run this again." if wsl else "")
            raise Fail(f"couldn't install Radicle: {e}.{hint}")
    nid = radicle.ensure_identity(alias, home)
    try:
        before = radicle.bridge_connections()
        note = radicle.route_through_proxy(home)
        if note:
            print(f"Note: {note}.", flush=True)
        radicle.ensure_node(home=home)
        if radicle.node_proxy(home) == f"{radicle.BRIDGE[0]}:{radicle.BRIDGE[1]}":
            print("Checking that the node really goes out through the proxy…", flush=True)
            try:
                radicle.check_proxy_in_use(home, before)
            except radicle.RadError:  # a node left running from before may not have read the setting
                radicle.restart_node(home)
                before = radicle.bridge_connections()
                radicle.ensure_node(home=home)
                radicle.check_proxy_in_use(home, before)
            print("It does.", flush=True)
    except radicle.RadError as e:
        raise Fail(str(e))
    return nid

def cmd_radicle(args):
    from . import radicle
    home = args.rad_home
    try:
        if args.action == "doctor":
            from . import netcheck
            for line in netcheck.doctor():
                print(line)
            return
        if args.action == "setup":
            ident = Identity.load()
            nid = radicle_ready(args.alias or (ident.name if ident else socket.gethostname()), home)
            print(f"Radicle is ready. Node {nid}.")
            status = radicle.rad(None, "node", "status", home=home, check=False)
            seeds = [l for l in status.splitlines() if "✓" in l and ":8776" in l]
            print(f"Connected to {len(seeds)} seed(s)." if seeds else "Not connected to any seed yet (give it a minute).")
            return
        repo = repo_root()
        ident = need_identity(repo)
        if args.action == "seed":
            if not args.target or ":" not in args.target:
                raise Fail("give the address the others reach this machine at: `legion radicle seed HOST:PORT` "
                           "(e.g. seed.example.org:8776; open that port)")
            seed = radicle.make_seed(repo, args.target, home)
            print(f"This node is now the project's seed, at {seed}. On every other node, in its clone, run:\n\n"
                  f"  python -m legion radicle prefer {seed}\n\nKeep this machine on: the others fetch from it first.")
            return
        if args.action == "tunnel":
            cmd_tunnel(repo, args.target or "status")
            return
        if args.action == "announce":
            cmd_announce(repo, [a for a in [args.target, *args.more] if a])
            return
        if args.action == "prefer":
            if not args.target:
                raise Fail("give the seed: `legion radicle prefer NID@HOST:PORT` (`legion radicle seed` prints it)")
            radicle.prefer_seed(repo, args.target, home)
            print(f"This node keeps a connection to {args.target} and fetches from it first.")
            return
        if args.action == "init":
            st = coordgit.state(repo)
            if not st.project:
                raise Fail("run `legion init` first; this publishes an existing legion project on Radicle")
            if ident.author not in st.integrators:
                raise Fail("only an integrator publishes the project on Radicle")
            radicle_ready(ident.name, home)
            if home:  # the Radicle commands below read it from the repository's config
                coordgit.git(repo, "config", "legion.radhome", home)
            title = st.project.get("project")
            # Radicle wants a plain name: "Erdős #902: Schütte tournaments" -> "erdos-902-schutte-tournaments".
            plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
            name = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "legion-project"
            rid = radicle.init_repo(repo, name, f"legion project: {title}")
            radicle.enable(repo, rid, home)
            coordgit.sync(repo, ident)
            print(f"Published on Radicle as {rid}. Public: Radicle's public seeds will carry it.")
            print(f"Others join with:\n\n  python -m legion join {rid} --name THEIR_NAME\n")
        else:
            rid = radicle.rid(repo)
            if not rid:
                raise Fail("this repository doesn't use Radicle (`legion radicle init` or `legion join rad:…`)")
            pubs = radicle.publishers(repo)
            print(f"Repository {rid} · this node {radicle.node_id(repo)}")
            print(f"Agents' repositories that have published legion state: {len(pubs)}, "
                  f"from {len(radicle.namespaces(repo))} node(s)")
            print(radicle.rad(repo, "node", "status", check=False))
    except radicle.RadError as e:
        raise Fail(str(e))

def cmd_announce(repo, given: list[str]) -> None:
    """Announce a node: NID@HOST:PORT for each address (several for one node: public, Tailscale, local), or nothing
    for this clone's own node at the addresses it sees (and those in git config legion.address)."""
    from . import radicle, tunnel
    if not radicle.rid(repo):
        raise Fail("this repository doesn't use Radicle (`legion radicle init` or `legion join rad:…`)")
    nodes: dict[str, list[str]] = {}
    if not given:
        nodes[radicle.node_id(repo)] = tunnel.own_addresses(repo)
    for a in given:
        nid, at, addr = a.partition("@")
        if not at or not re.fullmatch(r"z6Mk\w{40,60}", nid):
            raise Fail(f"{a}: give each address as NID@HOST:PORT, like z6Mk…@100.112.231.58:8776")
        nodes.setdefault(nid, []).append(addr)
    for nid, addrs in nodes.items():
        if not addrs:
            raise Fail(f"no address of {nid[-6:]} that others could reach was found")
        try:
            ev = tunnel.announce_node(repo, nid, addrs)
        except radicle.RadError as e:
            raise Fail(str(e))
        shown = ev.get("addrs", []) + ev.get("tailnet", []) + [x["addr"] for x in ev.get("lan", [])]
        print(f"Announced node {nid[-6:]} at {', '.join(shown)} (event {ev['id'][:8]}): every node connects to it "
              "once the event reaches it, if it can reach one of these.", flush=True)
    try:
        coordgit.publish_radicle(repo, Identity.load(repo=repo))
    except coordgit.GitError as e:
        print(f"legion: couldn't push it yet ({str(e).strip()[-200:]}); it goes out with your next command")

def cmd_tunnel(repo, what: str) -> None:
    from . import radicle, tunnel
    if what == "status":
        for line in tunnel.status(repo):
            print(line)
        return
    if what not in tunnel.MODES:
        raise Fail(f"`legion radicle tunnel` takes one of {', '.join(tunnel.MODES)} or status")
    if not radicle.rid(repo):
        raise Fail("this repository doesn't use Radicle (`legion radicle init` or `legion join rad:…`)")
    if what == "off":
        tunnel.off(repo)
        print("This node's tunnels are closed.")
        return
    if not tunnel.binary() and what not in ("auto", "outbound"):
        raise Fail("the tunnels need dumbpipe: `cargo install dumbpipe`, or put a binary from "
                   "https://github.com/n0-computer/dumbpipe/releases on the PATH")
    coordgit.git(repo, "config", "legion.tunnel", what)
    print({"auto": "Finding out how this node can be reached (the first time, the node may restart: on Linux to "
                   "listen on every interface; on Windows and macOS on 127.0.0.1, unless legion.lan is true)…",
           "outbound": "Connecting out to the nodes that can be reached…"}.get(
        what, "Setting up the tunnels (dumbpipe; the first time, the node may restart to listen on 127.0.0.1)…"),
          flush=True)
    for note in tunnel.keep(repo, strict=True):
        print(f"  {note}")
    print({"hub": "This node is the project's hub: the others reach it through its tunnel once the event "
                  "reaches them (only an integrator's hub counts). Keep this machine on.",
           "client": "This node connects to the project's hubs through their tunnels, and fetches from them first.",
           "mesh": "This node is exposed through its tunnel, and connects to the hubs and to up to "
                   f"{tunnel.PEERS} other active nodes.",
           "auto": "This node publishes how it can be reached (from the internet, from its local network, through "
                   "its tunnel), and connects to the hubs and to up to "
                   f"{tunnel.PEERS} other active nodes the most direct way. If its background processes turn out "
                   "not to outlive the commands that start them, it only connects out."
                   + ("" if tunnel.binary() else " Without dumbpipe there are no tunnels: only direct connections."),
           "outbound": "This node exposes nothing, and connects out to the nodes that published an address it "
                       "reaches. Its node remembers them, and connects to them whenever it starts."}[what])
    print("The tunnels are checked and relaunched with every network fetch. `legion radicle tunnel status` "
          "shows them.")

def cmd_peer(args):
    repo = repo_root()
    if args.action == "add":
        if not args.name or not args.url:
            raise Fail("usage: python -m legion peer add NAME URL")
        coordgit.add_peer(repo, args.name, args.url)
        ident = need_identity(repo)
        failed = coordgit.sync(repo, ident)
        state = "no answer for now" if args.name in failed else "synced"
        print(f"Peer {args.name} added ({args.url}): {state}.")
    elif args.action == "remove":
        coordgit.remove_peer(repo, args.name)
        print(f"Peer {args.name} removed.")
    else:
        names = coordgit.peers(repo)
        if not names:
            print("You have no peers: this repository uses origin as its common point.")
        for n in names:
            url = coordgit.run(repo, "remote", "get-url", n).stdout.decode().strip()
            print(f"{n:<16} {url}")

def cmd_serve(args):
    """Serve this repository read-only so peers can read it (git daemon, git:// protocol)."""
    repo = repo_root()
    # Without --export-all, git daemon only serves repositories that have this file, so folders
    # sitting next to this one are not exposed.
    (repo / ".git" / "git-daemon-export-ok").touch()
    host = args.host or socket.gethostname()
    url = f"git://{host}:{args.port}/{repo.name}" if args.port != 9418 else f"git://{host}/{repo.name}"
    print(f"Serving {repo} read-only.\nYour peers add you with:\n\n"
          f"  python -m legion peer add {need_identity(repo).name} {url}\n\n"
          "Nobody can write to your repository: every peer only reads. No encryption or authentication,\n"
          "so use it on a network you trust (your LAN or Tailscale). Ctrl+C to stop.", flush=True)
    cmd = ["git", "daemon", "--reuseaddr", "--informative-errors", f"--port={args.port}",
           f"--base-path={repo.parent}", f"--listen={args.listen}"]
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0

def cmd_netsync(args):
    """The background network fetch every command starts (see radicle.kick_network_sync); not for agents."""
    from . import radicle
    if args.announce:
        radicle.run_announce(repo_root())
        return 0
    if args.publish:
        radicle.run_publish(repo_root())
        return 0
    return 0 if radicle.run_network_sync(repo_root()) else 1

def cmd_daemon(args):
    """This machine's sync process (see daemon.py): commands start it; --status says how it's doing."""
    from . import daemon
    if not args.status:
        return daemon.run()
    d = daemon.folder()
    repos = [e.read_text(encoding="utf-8").strip() for e in sorted((d / "repos").glob("*"))] if (d / "repos").exists() else []
    log = (d / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-8:] if (d / "log.txt").exists() else []
    print(("The sync process is running" if daemon.alive() else "The sync process isn't running (the next command "
           "starts it)") + (f"; it syncs {len(repos)} clone(s):\n  " + "\n  ".join(repos) if repos else ".")
          + ("\nLatest lines of its log:\n  " + "\n  ".join(log) if log else ""))
    return 0


def add_parsers(sub) -> None:
    s = sub.add_parser("radicle", help="Radicle as the transport: across networks, no server, no open ports")
    s.add_argument("action", choices=["setup", "init", "status", "doctor", "seed", "prefer", "tunnel", "announce"],
                   help="setup: install Radicle and start the node; init: publish this project; status; "
                        "doctor: can this machine reach the seeds, directly or through its HTTP proxy?; "
                        "seed HOST:PORT: make this always-on node the project's own seed; "
                        "prefer NID@HOST:PORT: keep a connection to that seed and fetch from it first; "
                        "tunnel hub|client|mesh|auto|outbound|off|status: direct connections between the nodes "
                        "(hub: be the project's hub; client: connect to the hub; mesh: connect to the hub and to "
                        "the other active nodes, through dumbpipe tunnels; auto: each node works out how it can be "
                        "reached, by address on the internet or the local network or by tunnel; outbound: only "
                        "connect out; auto is the default); "
                        "announce [NID@HOST:PORT ...]: tell every node where a node can be reached (this one, at "
                        "the addresses it sees, without any)")
    s.add_argument("target", nargs="?", help="for seed: HOST:PORT the others reach; for prefer: NID@HOST:PORT; "
                                             "for tunnel: the mode, or status; for announce: NID@HOST:PORT")
    s.add_argument("more", nargs="*", help="for announce: more NID@HOST:PORT (HOST:PORT/PREFIX on a local network)")
    s.add_argument("--alias", help="node alias (default: your legion name)")
    s.add_argument("--rad-home", help="Radicle home to use (for running several nodes on one machine)")
    s.set_defaults(func=cmd_radicle)

    s = sub.add_parser("peer", help="git peers to sync with (p2p mode, no origin)")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("name", nargs="?")
    s.add_argument("url", nargs="?")
    s.set_defaults(func=cmd_peer)

    s = sub.add_parser("serve", help="serve this repository read-only to your peers (git daemon)")
    s.add_argument("--port", type=int, default=9418)
    s.add_argument("--listen", default="0.0.0.0", help="address to listen on (default: all)")
    s.add_argument("--host", help="name or IP your peers see you by (default: this machine's)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("netsync", help=argparse.SUPPRESS)  # the background network fetch: not for agents
    s.add_argument("--announce", action="store_true")  # or the announcement of what this node published
    s.add_argument("--publish", action="store_true")  # or the push of a command with no sync process to do it
    s.set_defaults(func=cmd_netsync)

    s = sub.add_parser("daemon", help="this machine's sync process (commands start it by themselves)")
    s.add_argument("--status", action="store_true", help="whether it runs, which clones it syncs, its latest log")
    s.set_defaults(func=cmd_daemon)


def main(argv=None) -> int:
    from .cli import main as legion_main
    p = argparse.ArgumentParser(prog="legion-net", description="The network under a legion project: Radicle, "
                                "peers, the git server, the sync process.")
    add_parsers(p.add_subparsers(dest="cmd", required=True))
    args = p.parse_args(argv)
    return legion_main([args.cmd, *(argv if argv is not None else sys.argv[1:])[1:]])

