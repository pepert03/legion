"""Tunnels between Radicle nodes (tunnel.py), with dumbpipe and the node faked: no network, no processes."""

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from legion import coordgit
from legion import events as E
from legion import radicle, reach, tunnel
from legion import state as S
from legion.identity import Identity, bind

ME = "z6Mk" + "m" * 44
HUB = "z6Mk" + "h" * 44
PEER = "z6Mk" + "p" * 44


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def tunnel_ev(ident, lamport, nid, ticket, hub=False, ts=None):
    ev = E.create(ident, "node.tunnel", lamport, nid=nid, ticket=ticket, hub=hub)
    if ts is not None:  # an old event: signed again with its time
        ev["ts"] = ts
        body = E.canonical(E._body(ev))
        ev["id"] = hashlib.sha256(body).hexdigest()[:16]
        ev["sig"] = ident.sign(body)
    return ev


def test_a_tunnel_event_names_a_node_and_a_ticket():
    boss = Identity.generate("boss")
    good = tunnel_ev(boss, 2, HUB, "endpointabc" * 4, hub=True)
    bad_nid = E.create(boss, "node.tunnel", 3, nid="nope", ticket="endpointabc" * 4)
    bad_ticket = E.create(boss, "node.tunnel", 4, nid=PEER, ticket="has spaces; rm -rf")
    st = S.build([E.create(boss, "project.init", 1, project="p"), good, bad_nid, bad_ticket])
    assert list(st.tunnels) == [HUB]
    assert len(st.ignored) == 2
    assert tunnel.is_hub(st, st.tunnels[HUB])


def test_only_an_integrators_hub_is_a_hub():
    boss, agent = Identity.generate("boss"), Identity.generate("agent")
    st = S.build([E.create(boss, "project.init", 1, project="p"),
                  tunnel_ev(agent, 2, HUB, "endpointabc" * 4, hub=True)])
    assert not tunnel.is_hub(st, st.tunnels[HUB])
    assert tunnel.choose(st, ME, "client", time.time()) == []  # not a hub, so a client doesn't connect
    assert tunnel.choose(st, ME, "mesh", time.time()) == [HUB]  # a peer like any other in a mesh


def test_a_mesh_takes_the_hubs_and_a_capped_spread_of_active_nodes(monkeypatch):
    monkeypatch.setattr(tunnel, "PEERS", 3)
    boss = Identity.generate("boss")
    now = time.time()
    evs = [E.create(boss, "project.init", 1, project="p"), tunnel_ev(boss, 2, HUB, "hubticket" * 4, hub=True)]
    nids = [f"z6Mk{i:02d}" + "x" * 42 for i in range(6)]
    for i, nid in enumerate(nids):
        evs.append(tunnel_ev(Identity.generate(f"a{i}"), 3 + i, nid, f"ticket{i}" * 4))
    quiet = Identity.generate("quiet")
    evs.append(tunnel_ev(quiet, 20, PEER, "quietticket" * 4, ts=now - tunnel.ACTIVE - 60))
    st = S.build(evs)
    picked = tunnel.choose(st, ME, "mesh", now)
    assert picked[0] == HUB and len(picked) == 4 and PEER not in picked
    assert set(picked[1:]) <= set(nids)
    assert picked == tunnel.choose(st, ME, "mesh", now)  # the same every time
    others = tunnel.choose(st, nids[0], "mesh", now)  # another node spreads its own way
    assert nids[0] not in others
    assert tunnel.choose(st, ME, "client", now) == [HUB]
    assert tunnel.choose(st, HUB, "hub", now) == []  # a hub doesn't connect to itself


def test_the_short_ticket_is_read_from_dumbpipes_output(tmp_path):
    log = tmp_path / "log"
    log.write_text("Forwarding incoming requests to '127.0.0.1:8776'.\nTo connect, use e.g.:\n"
                   "dumbpipe connect-tcp endpointlongticketaaaa\nor:\ndumbpipe connect-tcp endpointshort\n")

    class Running:
        def poll(self):
            return None
    assert tunnel._wait_ticket(log, Running(), wait=2) == "endpointshort"

    class Dead:
        def poll(self):
            return 1
    log.write_text("error: something\n")
    with pytest.raises(radicle.RadError, match="no ticket"):
        tunnel._wait_ticket(log, Dead(), wait=2)


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A clone of a project on Radicle whose node and dumbpipe are fakes that record what was asked of them."""
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "legion-home"))
    home = tmp_path / "radhome"
    (home / "node").mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"node": {"listen": [], "connect": []}, "preferredSeeds": []}))
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.name", "x")
    git(repo, "config", "user.email", "x@x")
    radicle.enable(repo, "rad:zTest")
    git(repo, "config", "legion.radhome", str(home))
    me = Identity.generate("me")
    me.save()
    bind(repo, me)
    boss = Identity.generate("boss")
    coordgit.append(repo, boss, E.create(boss, "project.init", 1, project="p"))

    rec = {"spawned": [], "connects": [], "restarts": 0, "dead": set(), "sessions": set(), "bound": {},
           "canaries": [], "local": {"public": [], "lan": [("192.168.1.10", "192.168.1.0/24")]}, "mapped": None,
           "mappings": 0, "answer": set()}

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    def spawn(cmd, cwd, log=None, env=None):
        pid = 1000 + len(rec["spawned"])
        rec["spawned"].append((pid, cmd, env))
        if cmd[1] == "-c":  # the canary
            rec["canaries"].append(pid)
        elif cmd[1] == "listen-tcp":
            log.write_text(f"dumbpipe connect-tcp endpointfull{pid}\nor:\ndumbpipe connect-tcp endpointshort{env['IROH_SECRET'][:16]}\n")
        else:
            rec["bound"][pid] = int(cmd[3].rsplit(":", 1)[1])
        return Proc(pid)

    def run(*args, repo=None, home=None, timeout=60, check=True, cwd=None):
        if args[:3] == ("rad", "node", "connect"):
            rec["connects"].append(args[3])
        return subprocess.CompletedProcess(args, 0, "", "")

    def restart(home=None):
        rec["restarts"] += 1

    monkeypatch.setattr(tunnel, "binary", lambda: "dumbpipe")
    monkeypatch.setattr(tunnel, "rad_home", lambda repo: home)
    monkeypatch.setattr(tunnel, "alive", lambda pid: bool(pid) and pid not in rec["dead"])
    monkeypatch.setattr(tunnel, "kill", lambda pid: rec["dead"].add(pid))
    monkeypatch.setattr(tunnel, "connected", lambda repo: set(rec["sessions"]))
    monkeypatch.setattr(tunnel, "_taken", lambda port, host="127.0.0.1": port in {p for pid, p in rec["bound"].items()
                                                                if pid not in rec["dead"]})
    monkeypatch.setattr(radicle, "IS_WINDOWS", False)
    monkeypatch.setattr(tunnel, "IS_WINDOWS", False)
    monkeypatch.setattr(radicle, "spawn_detached", spawn)
    monkeypatch.setattr(radicle, "run", run)
    monkeypatch.setattr(radicle, "read_file", lambda path, home=None, repo=None: Path(path).read_text())
    monkeypatch.setattr(radicle, "write_file", lambda path, text, repo=None: Path(path).write_text(text))
    monkeypatch.setattr(radicle, "node_id", lambda repo=None, home=None: ME)
    monkeypatch.setattr(radicle, "node_running", lambda repo=None, home=None: True)
    monkeypatch.setattr(radicle, "restart_node", restart)
    monkeypatch.setattr(radicle, "ensure_node", lambda repo=None, home=None: None)
    monkeypatch.setattr(radicle, "kick_publish", lambda repo: None)
    monkeypatch.setattr(radicle, "pid_alive", lambda pid, what="legion": pid not in rec["dead"])

    def mapped(lan_ip, port):
        rec["mappings"] += 1
        return rec["mapped"]
    monkeypatch.setattr(reach, "local_addresses", lambda: rec["local"])
    monkeypatch.setattr(reach, "mapped", mapped)
    monkeypatch.setattr(reach, "probe", lambda addrs: {a: a in rec["answer"] for a in addrs})
    return {"repo": repo, "home": home, "rec": rec, "boss": boss, "me": me}


def test_off_does_nothing(node):
    assert tunnel.keep(node["repo"]) == []
    assert node["rec"]["spawned"] == []


def test_a_hub_listens_publishes_its_ticket_once_and_keeps_its_key(node):
    repo, home, rec = node["repo"], node["home"], node["rec"]
    git(repo, "config", "legion.tunnel", "hub")
    notes = tunnel.keep(repo, strict=True)
    assert notes == ["published this node's ticket"]
    config = json.loads((home / "config.json").read_text())
    assert config["node"]["listen"] == ["127.0.0.1:8776"]  # it didn't listen: now it does, on this machine only
    assert rec["restarts"] == 1
    (pid, cmd, env), = rec["spawned"]
    assert cmd[:4] == ["dumbpipe", "listen-tcp", "--host", "127.0.0.1:8776"]
    key = env["IROH_SECRET"]
    st = coordgit.state(repo)
    ev = st.tunnels[ME]
    assert ev["ticket"] == f"endpointshort{key[:16]}" and ev["hub"] is True  # the short ticket
    assert ev["author"] == node["me"].author

    assert tunnel.keep(repo) == []  # running, and the project has its ticket: nothing to do
    assert len(rec["spawned"]) == 1 and rec["restarts"] == 1

    rec["dead"].add(pid)  # a sandbox killed it
    assert tunnel.keep(repo) == []
    (_, _, env2) = rec["spawned"][1]
    assert env2["IROH_SECRET"] == key  # same key, same ticket: no new event
    assert len(coordgit.state(repo).events) == 2


def test_a_client_tunnels_to_the_hub_and_prefers_it(node):
    repo, home, rec, boss = node["repo"], node["home"], node["rec"], node["boss"]
    coordgit.append(repo, boss, tunnel_ev(boss, 2, HUB, "hubticket" * 4, hub=True))
    git(repo, "config", "legion.tunnel", "client")
    assert tunnel.keep(repo, strict=True) == []
    (pid, cmd, _), = rec["spawned"]
    assert cmd[:3] == ["dumbpipe", "connect-tcp", "--addr"] and cmd[4] == "hubticket" * 4
    port = int(cmd[3].rsplit(":", 1)[1])
    assert tunnel.PORTS[0] <= port < tunnel.PORTS[1]
    addr = f"{HUB}@127.0.0.1:{port}"
    assert rec["connects"] == [addr]
    config = json.loads((home / "config.json").read_text())
    assert config["preferredSeeds"] == [addr] and config["node"]["connect"] == [addr]
    assert git(repo, "config", "legion.seed") == addr
    assert config["node"]["listen"] == []  # a client doesn't expose itself
    assert rec["restarts"] == 0  # nor restarts the node: it connects it by hand

    rec["sessions"].add(HUB)
    tunnel.keep(repo)
    assert len(rec["spawned"]) == 1 and rec["connects"] == [addr]  # up and connected: nothing to do

    rec["dead"].add(pid)
    rec["sessions"].clear()
    tunnel.keep(repo)
    (_, cmd2, _) = rec["spawned"][1]
    assert cmd2[3] == f"127.0.0.1:{port}"  # back on the same port, so the preferred seed still holds
    assert rec["connects"] == [addr, addr]


def test_a_mesh_connects_to_others_and_drops_those_gone_quiet(node, monkeypatch):
    repo, home, rec = node["repo"], node["home"], node["rec"]
    other = Identity.generate("other")
    coordgit.append(repo, other, tunnel_ev(other, 2, PEER, "peerticket" * 4))
    git(repo, "config", "legion.tunnel", "mesh")
    notes = tunnel.keep(repo, strict=True)
    assert notes == ["published this node's ticket"]
    kinds = [cmd[1] for _, cmd, _ in rec["spawned"]]
    assert kinds == ["listen-tcp", "connect-tcp"]
    assert rec["connects"][0].startswith(PEER + "@127.0.0.1:")
    assert "legion.seed" not in subprocess.run(["git", "config", "-l"], cwd=repo, capture_output=True,
                                               text=True).stdout  # a peer isn't a hub
    assert PEER in tunnel.load(home)["peers"]

    later = time.time() + tunnel.ACTIVE + 3600
    monkeypatch.setattr(tunnel.time, "time", lambda: later)
    tunnel.keep(repo)
    assert tunnel.load(home)["peers"] == {}  # its agent went quiet: the tunnel is closed
    peer_pid = rec["spawned"][1][0]
    assert peer_pid in rec["dead"]


def test_off_closes_the_tunnels_and_forgets_the_hub(node):
    repo, home, rec, boss = node["repo"], node["home"], node["rec"], node["boss"]
    coordgit.append(repo, boss, tunnel_ev(boss, 2, HUB, "hubticket" * 4, hub=True))
    git(repo, "config", "legion.tunnel", "mesh")
    tunnel.keep(repo, strict=True)
    tunnel.off(repo)
    assert {pid for pid, _, _ in rec["spawned"]} <= rec["dead"]
    config = json.loads((home / "config.json").read_text())
    assert config["preferredSeeds"] == [] and config["node"]["connect"] == []
    assert subprocess.run(["git", "config", "legion.seed"], cwd=repo).returncode == 1
    assert tunnel.mode(repo) == "off"


def test_without_dumbpipe_it_says_how_to_get_it(node, monkeypatch):
    git(node["repo"], "config", "legion.tunnel", "mesh")
    monkeypatch.setattr(tunnel, "binary", lambda: None)
    notes = tunnel.keep(node["repo"])
    assert "cargo install dumbpipe" in notes[0]
    assert "cargo install dumbpipe" in radicle.netsync_status(node["repo"], "tunnel")["notes"][0]
    with pytest.raises(radicle.RadError):
        tunnel.keep(node["repo"], strict=True)


def test_a_windows_node_that_doesnt_reconnect_through_its_tunnel_is_restarted(node, monkeypatch):
    """Radicle built for Windows, having lost a node, never completes its next connection to it: restarted, it
    connects at once. Only after STUCK seconds without a session, if its last attempt reached the other end, and
    not again for RESTART_EVERY."""
    repo, home, rec, boss = node["repo"], node["home"], node["rec"], node["boss"]
    coordgit.append(repo, boss, tunnel_ev(boss, 2, HUB, "hubticket" * 4, hub=True))
    git(repo, "config", "legion.tunnel", "client")
    monkeypatch.setattr(tunnel, "IS_WINDOWS", True)
    monkeypatch.setattr(radicle, "native", lambda repo=None: True)
    clock = [time.time()]
    monkeypatch.setattr(tunnel.time, "time", lambda: clock[0])
    tunnel.keep(repo, strict=True)
    assert rec["restarts"] == 0 and len(rec["connects"]) == 1
    attempt(home, clock[0] + 1)  # the node tries, and dumbpipe gets through
    clock[0] += 60
    tunnel.keep(repo)
    assert rec["restarts"] == 0  # not long enough yet
    clock[0] += 60
    notes = tunnel.keep(repo)
    assert rec["restarts"] == 1 and "restarted the node" in notes[0]
    assert rec["connects"][-1] == rec["connects"][0]  # connected again after the restart
    clock[0] += 120
    tunnel.keep(repo)
    assert rec["restarts"] == 1  # not again so soon
    rec["sessions"].add(HUB)
    clock[0] += tunnel.RESTART_EVERY
    tunnel.keep(repo)
    assert rec["restarts"] == 1  # connected: nothing to do


def attempt(home, at, failed=False):
    """What dumbpipe logs when the node connects through the tunnel to HUB (and when it can't reach it)."""
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(tunnel._log(home, HUB), "a", encoding="utf-8") as f:
        f.write(f"\x1b[2m{stamp}\x1b[0m \x1b[32m INFO\x1b[0m dumbpipe: got tcp connection from 127.0.0.1:50000\n")
        if failed:
            f.write(f"{stamp}  WARN dumbpipe: error handling connection: error connecting to 3a4b\n")


def test_a_windows_node_isnt_restarted_for_a_node_that_is_down(node, monkeypatch):
    """Agents' sandboxes kill their nodes every half hour: while one is down, every attempt fails."""
    repo, home, rec, boss = node["repo"], node["home"], node["rec"], node["boss"]
    coordgit.append(repo, boss, tunnel_ev(boss, 2, HUB, "hubticket" * 4, hub=True))
    git(repo, "config", "legion.tunnel", "client")
    monkeypatch.setattr(tunnel, "IS_WINDOWS", True)
    monkeypatch.setattr(radicle, "native", lambda repo=None: True)
    clock = [time.time()]
    monkeypatch.setattr(tunnel.time, "time", lambda: clock[0])
    tunnel.keep(repo, strict=True)
    for _ in range(5):
        attempt(home, clock[0] + 1, failed=True)
        clock[0] += 120
        tunnel.keep(repo)
    assert rec["restarts"] == 0
    attempt(home, clock[0] - 100)  # back up: the attempt went through, and still no session
    clock[0] += 60
    tunnel.keep(repo)
    assert rec["restarts"] == 1


# --- auto and outbound -------------------------------------------------------------------------------------------

LAN_PEER = {"addr": "192.168.1.20:8776", "net": "192.168.1.0/24"}


def config(home):
    return json.loads((home / "config.json").read_text())


def peer_ev(ident, lamport, nid=PEER, ticket="peerticket" * 4, addrs=None, lan=None):
    payload = {"ticket": ticket} if ticket else {}
    payload.update({k: v for k, v in (("addrs", addrs), ("lan", lan)) if v})
    return E.create(ident, "node.tunnel", lamport, nid=nid, hub=False, **payload)


def test_auto_listens_everywhere_and_publishes_its_local_address_and_its_tunnel(node):
    repo, home, rec = node["repo"], node["home"], node["rec"]
    git(repo, "config", "legion.tunnel", "auto")
    assert tunnel.keep(repo, strict=True) == ["published how to reach this node"]
    assert config(home)["node"]["listen"] == ["0.0.0.0:8776"]  # the local network reaches it, not only the tunnel
    assert rec["restarts"] == 1
    ev = coordgit.state(repo).tunnels[ME]
    assert ev["lan"] == [{"addr": "192.168.1.10:8776", "net": "192.168.1.0/24"}]
    assert ev["ticket"].startswith("endpointshort") and "addrs" not in ev
    assert rec["mappings"] == 1  # it asked the router; this one maps nothing
    assert len(rec["canaries"]) == 1
    assert tunnel.keep(repo) == []  # nothing changed: no event, no restart, the canary still lives
    assert rec["restarts"] == 1 and len(rec["canaries"]) == 1


def test_auto_publishes_a_port_the_router_mapped(node):
    repo, home, rec = node["repo"], node["home"], node["rec"]
    rec["mapped"] = "81.0.0.1:8776"
    git(repo, "config", "legion.tunnel", "auto")
    tunnel.keep(repo, strict=True)
    assert coordgit.state(repo).tunnels[ME]["addrs"] == ["81.0.0.1:8776"]
    assert config(home)["node"]["externalAddresses"] == ["81.0.0.1:8776"]  # Radicle announces it too


def test_auto_asks_no_router_when_told_not_to(node):
    repo, rec = node["repo"], node["rec"]
    rec["mapped"] = "81.0.0.1:8776"
    git(repo, "config", "legion.tunnel", "auto")
    git(repo, "config", "legion.upnp", "false")
    tunnel.keep(repo, strict=True)
    assert rec["mappings"] == 0 and "addrs" not in coordgit.state(repo).tunnels[ME]


def test_auto_reaches_a_node_on_its_network_directly_and_its_node_remembers_it(node):
    repo, home, rec = node["repo"], node["home"], node["rec"]
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2, lan=[LAN_PEER]))
    rec["answer"].add(LAN_PEER["addr"])
    git(repo, "config", "legion.tunnel", "auto")
    tunnel.keep(repo, strict=True)
    assert rec["connects"] == [f"{PEER}@192.168.1.20:8776"]
    assert not [cmd for _, cmd, _ in rec["spawned"] if cmd[1] == "connect-tcp"]  # no tunnel needed
    assert f"{PEER}@192.168.1.20:8776" in config(home)["node"]["connect"]  # it connects there whenever it starts


def test_auto_takes_the_tunnel_where_the_network_isolates_its_clients(node):
    repo, rec = node["repo"], node["rec"]
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2, lan=[LAN_PEER]))
    git(repo, "config", "legion.tunnel", "auto")
    tunnel.keep(repo, strict=True)  # nothing answers at its local address
    assert [cmd[1] for _, cmd, _ in rec["spawned"] if cmd[1] != "-c"] == ["listen-tcp", "connect-tcp"]
    assert rec["connects"][0].startswith(f"{PEER}@127.0.0.1:")


def test_a_direct_address_that_never_leads_to_the_node_is_left_for_the_tunnel(node, monkeypatch):
    """Another device at that address (the same private network in another house), or a firewall that opens the
    connection and nothing more."""
    repo, home, rec = node["repo"], node["home"], node["rec"]
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2, lan=[LAN_PEER]))
    rec["answer"].add(LAN_PEER["addr"])
    git(repo, "config", "legion.tunnel", "auto")
    clock = [time.time()]
    monkeypatch.setattr(tunnel.time, "time", lambda: clock[0])
    tunnel.keep(repo, strict=True)
    assert rec["connects"] == [f"{PEER}@192.168.1.20:8776"]
    clock[0] += tunnel.DIRECT_STUCK + 30
    tunnel.keep(repo)
    assert rec["connects"][-1].startswith(f"{PEER}@127.0.0.1:")
    assert f"{PEER}@192.168.1.20:8776" not in config(home)["node"]["connect"]
    clock[0] += 60
    tunnel.keep(repo)
    assert len([c for c in rec["connects"] if "192.168" in c]) == 1  # not tried again so soon


def test_auto_only_connects_out_where_processes_dont_outlive_the_command(node):
    """Agents' sandboxes kill every background process when the command returns: the canary dies each time."""
    repo, home, rec = node["repo"], node["home"], node["rec"]
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2, lan=[LAN_PEER]))
    rec["answer"].add(LAN_PEER["addr"])
    git(repo, "config", "legion.tunnel", "auto")
    tunnel.keep(repo, strict=True)
    assert "ticket" in coordgit.state(repo).tunnels[ME]
    for _ in range(2):  # the sandbox kills everything; the next command looks
        rec["dead"].update(pid for pid, _, _ in rec["spawned"])
        notes = tunnel.keep(repo)
    assert notes == ["withdrew this node's addresses and ticket: it only connects out"]
    ev = coordgit.state(repo).tunnels[ME]
    assert not (ev.get("ticket") or ev.get("addrs") or ev.get("lan"))
    assert "listen" not in tunnel.load(home)
    assert rec["connects"][-1] == f"{PEER}@192.168.1.20:8776"  # it still reaches the others directly
    assert not tunnel.choose(coordgit.state(repo), PEER, "mesh", time.time())  # nobody tries to reach it


def test_outbound_exposes_nothing(node):
    repo, home, rec = node["repo"], node["home"], node["rec"]
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2, ticket=None, addrs=["81.0.0.2:8776"]))
    rec["answer"].add("81.0.0.2:8776")
    git(repo, "config", "legion.tunnel", "outbound")
    assert tunnel.keep(repo, strict=True) == []
    assert config(home)["node"]["listen"] == [] and rec["restarts"] == 0
    assert ME not in coordgit.state(repo).tunnels  # nothing to withdraw, so no event
    assert rec["connects"] == [f"{PEER}@81.0.0.2:8776"]
    assert rec["spawned"] == []  # no process of its own: told it lives in a sandbox, it needs no canary


def test_a_tunnel_event_with_addresses():
    boss = Identity.generate("boss")
    good = E.create(boss, "node.tunnel", 2, nid=PEER, hub=False, addrs=["81.0.0.2:8776", "[2a01:4f8::1]:8776"],
                    lan=[LAN_PEER])
    withdrawn = E.create(boss, "node.tunnel", 3, nid=HUB, hub=False)
    private_as_public = E.create(boss, "node.tunnel", 4, nid=ME, hub=False, addrs=["192.168.1.2:8776"])
    public_as_local = E.create(boss, "node.tunnel", 5, nid=ME, hub=False,
                               lan=[{"addr": "81.0.0.2:8776", "net": "81.0.0.0/24"}])
    outside_its_net = E.create(boss, "node.tunnel", 6, nid=ME, hub=False,
                               lan=[{"addr": "192.168.1.20:8776", "net": "10.0.0.0/8"}])
    st = S.build([E.create(boss, "project.init", 1, project="p"), good, withdrawn, private_as_public,
                  public_as_local, outside_its_net])
    assert sorted(st.tunnels) == sorted([PEER, HUB]) and len(st.ignored) == 3
    assert tunnel.candidates(st.tunnels[PEER], [{"addr": "192.168.1.10:8776", "net": "192.168.1.0/24"}]) == \
        ["81.0.0.2:8776", "[2a01:4f8::1]:8776", "192.168.1.20:8776"]


def test_the_addresses_this_machine_goes_out_from(monkeypatch):
    sources = {reach.socket.AF_INET: "192.168.86.201", reach.socket.AF_INET6: None}
    monkeypatch.setattr(reach, "_source", lambda family, target: sources[family])
    monkeypatch.setattr(reach, "prefixes", lambda: {"192.168.86.201": 24})
    assert reach.local_addresses() == {"public": [], "lan": [("192.168.86.201", "192.168.86.0/24")]}
    sources.update({reach.socket.AF_INET: "100.64.3.4", reach.socket.AF_INET6: "2a01:4f8::1"})  # CGNAT; IPv6
    assert reach.local_addresses() == {"public": ["2a01:4f8::1"], "lan": []}


def test_nat_pmp_maps_only_behind_a_router_whose_own_address_is_public(monkeypatch):
    import struct

    class Router:
        external = "81.0.0.1"

        def __init__(self, *a):
            self.answers = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def sendto(self, data, addr):
            if data == b"\x00\x00":
                self.answers.append(struct.pack("!BBHI", 0, 128, 0, 1) + reach.socket.inet_aton(self.external))
            else:
                _, _, _, internal, external, lifetime = struct.unpack("!BBHHHI", data)
                self.answers.append(struct.pack("!BBHIHHI", 0, 130, 0, 1, internal, external, lifetime))

        def recv(self, n):
            return self.answers.pop(0)
    monkeypatch.setattr(reach.socket, "socket", Router)
    assert reach.natpmp_map("192.168.86.1", 8776) == "81.0.0.1:8776"
    Router.external = "192.168.0.29"  # this PC's: a router behind another NAT
    assert reach.natpmp_map("192.168.86.1", 8776) is None


SEED = "z6Mk" + "s" * 44


def on_windows(monkeypatch):
    monkeypatch.setattr(tunnel, "IS_WINDOWS", True)
    monkeypatch.setattr(radicle, "native", lambda repo=None: True)


def test_on_windows_auto_listens_on_this_machine_only_unless_told(node, monkeypatch):
    """A program listening on the network makes Windows ask the person; one who said no was asked again by a test."""
    repo, home, rec = node["repo"], node["home"], node["rec"]
    on_windows(monkeypatch)
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2, lan=[LAN_PEER]))
    rec["answer"].add(LAN_PEER["addr"])
    git(repo, "config", "legion.tunnel", "auto")
    tunnel.keep(repo, strict=True)
    assert config(home)["node"]["listen"] == ["127.0.0.1:8776"]  # for its tunnel only
    ev = coordgit.state(repo).tunnels[ME]
    assert ev["ticket"] and "lan" not in ev and "addrs" not in ev  # nothing the network could reach
    assert rec["mappings"] == 0  # nor a port on the router leading nowhere
    assert rec["connects"] == [f"{PEER}@192.168.1.20:8776"]  # going out is no listening: it still does
    assert any("listens on this machine only" in line for line in tunnel.status(repo))

    git(repo, "config", "legion.lan", "true")
    tunnel.keep(repo)
    assert config(home)["node"]["listen"] == ["0.0.0.0:8776"]
    assert coordgit.state(repo).tunnels[ME]["lan"] == [{"addr": "192.168.1.10:8776", "net": "192.168.1.0/24"}]


def test_with_a_seed_of_the_projects_own_auto_needs_no_tunnels(node, monkeypatch):
    """instinct's seed: every node reaches it, so every node reaches the others through it."""
    repo, home, rec = node["repo"], node["home"], node["rec"]
    c = config(home)
    c["preferredSeeds"] = [f"{SEED}@100.112.231.58:8776"]
    (home / "config.json").write_text(json.dumps(c))
    other = Identity.generate("other")
    coordgit.append(repo, other, peer_ev(other, 2))  # reachable by tunnel only
    rec["sessions"].add(SEED)
    git(repo, "config", "legion.tunnel", "auto")
    clock = [time.time()]
    monkeypatch.setattr(tunnel.time, "time", lambda: clock[0])
    tunnel.keep(repo, strict=True)
    assert [cmd[1] for _, cmd, _ in rec["spawned"]] == ["-c"]  # the canary, and no dumbpipe
    assert "ticket" not in coordgit.state(repo).tunnels[ME]
    assert any("no tunnels needed" in line for line in tunnel.status(repo))

    rec["sessions"].clear()  # the seed is away
    clock[0] += 60
    tunnel.keep(repo)
    assert [cmd[1] for _, cmd, _ in rec["spawned"]] == ["-c"]  # not for a minute
    clock[0] += tunnel.SEED_GRACE
    tunnel.keep(repo)
    assert [cmd[1] for _, cmd, _ in rec["spawned"]][1:] == ["listen-tcp", "connect-tcp"]
    assert coordgit.state(repo).tunnels[ME]["ticket"]


def test_a_hub_reached_by_tunnel_is_no_seed_of_the_projects_own(node):
    repo, home = node["repo"], node["home"]
    c = config(home)
    c["preferredSeeds"] = [f"{HUB}@127.0.0.1:41000", f"{SEED}@100.112.231.58:8776"]
    (home / "config.json").write_text(json.dumps(c))
    git(repo, "config", "legion.seed", f"{PEER}@37.222.165.89:8776")
    assert tunnel.preferred_seeds(repo, home) == {SEED, PEER}
