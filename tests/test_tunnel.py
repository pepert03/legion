"""Tunnels between Radicle nodes (tunnel.py), with dumbpipe and the node faked: no network, no processes."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from legion import coordgit
from legion import events as E
from legion import radicle, tunnel
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

    rec = {"spawned": [], "connects": [], "restarts": 0, "dead": set(), "sessions": set(), "bound": {}}

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    def spawn(cmd, cwd, log=None, env=None):
        pid = 1000 + len(rec["spawned"])
        rec["spawned"].append((pid, cmd, env))
        if cmd[1] == "listen-tcp":
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
    monkeypatch.setattr(tunnel, "_taken", lambda port: port in {p for pid, p in rec["bound"].items()
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
    connects at once. Only after STUCK seconds without a session, and not again for RESTART_EVERY."""
    repo, rec, boss = node["repo"], node["rec"], node["boss"]
    coordgit.append(repo, boss, tunnel_ev(boss, 2, HUB, "hubticket" * 4, hub=True))
    git(repo, "config", "legion.tunnel", "client")
    monkeypatch.setattr(tunnel, "IS_WINDOWS", True)
    monkeypatch.setattr(radicle, "native", lambda repo=None: True)
    clock = [time.time()]
    monkeypatch.setattr(tunnel.time, "time", lambda: clock[0])
    tunnel.keep(repo, strict=True)
    assert rec["restarts"] == 0 and len(rec["connects"]) == 1
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
