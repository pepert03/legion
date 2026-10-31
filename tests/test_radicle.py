"""Radicle transport, against a fake Radicle storage: a bare repository with one namespace per node."""

import subprocess
from pathlib import Path

import pytest

from legion import coordgit
from legion import events as E
from legion import radicle
from legion.identity import Identity


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def network(tmp_path, monkeypatch):
    """Two nodes sharing one storage, the way two Radicle nodes see each other once they've replicated."""
    store = tmp_path / "storage"
    git(tmp_path, "init", "-q", "--bare", str(store))
    nodes = {}
    for name, nid in (("z6MkAlice", "z6MkAlice"), ("z6MkBob", "z6MkBob"), ("bob-2", "z6MkBob")):
        r = tmp_path / name
        git(tmp_path, "init", "-q", "-b", "main", str(r))
        git(r, "config", "user.name", nid)
        git(r, "config", "user.email", f"{nid}@x")
        radicle.enable(r, "rad:zTest")
        git(r, "config", "legion.nid", nid)
        nodes[name] = r

    def run(*args, repo=None, home=None, timeout=60, check=True, cwd=None):
        p = subprocess.run(list(args), cwd=cwd, capture_output=True, encoding="utf-8", timeout=timeout)
        if check and p.returncode:
            raise radicle.RadError(p.stderr)
        return p

    def publish(repo, who):  # what the rad remote does: the pushed refs land in the node's namespace
        nid = git(repo, "config", "legion.nid")
        for spec in radicle.publish_specs(repo, who):
            src, dest = spec.split(":")
            git(repo, "push", "-q", "--force", str(store), f"{src}:refs/namespaces/{nid}/{dest}")

    monkeypatch.setattr(radicle, "IS_WINDOWS", False)
    monkeypatch.setattr(radicle, "run", run)
    monkeypatch.setattr(radicle, "storage", lambda repo: str(store))
    monkeypatch.setattr(radicle, "network_sync", lambda repo, force=False: True)
    monkeypatch.setattr(radicle, "kick_network_sync", lambda repo: True)
    monkeypatch.setattr(radicle, "publish", publish)
    return nodes


def test_events_travel_through_the_storage(network):
    alice, bob = network["z6MkAlice"], network["z6MkBob"]
    boss = Identity.generate("boss")
    coordgit.append(alice, boss, E.create(boss, "project.init", 1, project="p"))
    coordgit.sync(alice, boss)
    coordgit.sync(bob, boss)
    assert [p.split("-")[1] for p in coordgit.peers(bob)] == ["kAlice"]  # <identity>-<end of node id>
    assert [e["type"] for e in coordgit.read_events(bob)] == ["project.init"]


def test_a_patch_republished_by_several_nodes_is_fetched_once(network):
    alice, bob = network["z6MkAlice"], network["z6MkBob"]
    boss = Identity.generate("boss")
    coordgit.append(bob, boss, E.create(boss, "project.init", 1, project="p"))
    (bob / "f.txt").write_text("x")
    git(bob, "add", "f.txt")
    git(bob, "commit", "-qm", "patch")
    git(bob, "update-ref", "refs/legion/patches/t-1/bob-1", "HEAD")
    coordgit.sync(bob, boss)
    coordgit.sync(alice, boss)  # alice now gossips bob's patch from her own namespace too
    coordgit.sync(bob, boss)  # would fail if both namespaces mapped onto the same local ref
    assert git(alice, "rev-parse", "refs/legion/patches/t-1/bob-1") == git(bob, "rev-parse", "HEAD")


def test_a_new_main_goes_out_with_the_command_that_published_it(network):
    """The push runs before the state says main moved: the sync pushes again, or a founder's `main publish`
    with no command after it would leave the commit on its own computer."""
    from legion.ctx import Ctx
    alice, bob = network["z6MkAlice"], network["z6MkBob"]
    boss = Identity.generate("boss")
    (alice / "f.txt").write_text("x")
    git(alice, "add", "f.txt")
    git(alice, "commit", "-qm", "main")
    tip = git(alice, "rev-parse", "HEAD")
    coordgit.append(alice, boss, E.create(boss, "project.init", 1, project="p", integrators=[boss.author]))
    coordgit.append(alice, boss, E.create(boss, "main.updated", 2, commit=tip))
    c = object.__new__(Ctx)
    c.repo, c.ident, c.unreachable = alice, boss, []
    c.sync()
    coordgit.sync(bob, boss)
    assert coordgit.state(bob).main_tip == tip
    assert coordgit.has_commit(bob, tip)  # bob can start from it without waiting for alice's next command


def test_host_path_is_unchanged_off_windows(monkeypatch):
    monkeypatch.setattr(radicle, "IS_WINDOWS", False)
    assert radicle.host_path("/home/x/repo") == "/home/x/repo"


def test_a_converted_radicle_home_is_refused(monkeypatch):
    monkeypatch.setattr(radicle, "_native", {})
    monkeypatch.setenv("LEGION_RAD_NATIVE", "0")
    with pytest.raises(radicle.RadError, match="MSYS_NO_PATHCONV"):
        radicle.run("true", home="C:/Program Files/Git/home/legion/.radicle")


@pytest.fixture
def windows(tmp_path, monkeypatch):
    """A Windows machine as legion sees it, with a fake Radicle build in ~/.radicle/bin, and every command it
    would run recorded instead."""
    calls = []
    monkeypatch.setattr(radicle, "IS_WINDOWS", True)
    monkeypatch.setattr(radicle, "_native", {})
    monkeypatch.delenv("LEGION_RAD_NATIVE", raising=False)
    monkeypatch.setattr(radicle, "NATIVE_BIN", tmp_path / "bin")
    monkeypatch.setattr(radicle, "_cfg", lambda repo, key: None)
    monkeypatch.setattr(radicle.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(radicle.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    return calls


def test_on_windows_a_native_build_runs_without_wsl(windows, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rad.exe").write_text("")
    assert radicle.native()
    radicle.run("rad", "self", home=str(tmp_path / "home"), cwd=tmp_path)
    cmd, kw = windows[-1]
    assert cmd == ["rad", "self"] and kw["cwd"] == tmp_path
    assert kw["env"]["RAD_HOME"] == str(tmp_path / "home") and kw["env"]["RAD_PASSPHRASE"] == ""
    assert kw["env"]["PATH"].startswith(str(tmp_path / "bin"))
    repo = tmp_path / "repo"
    assert radicle.host_path(repo) == str(repo)  # no /mnt/c: Radicle sees the path as it is
    radicle.rgit(repo, "push", "rad")
    assert windows[-1][0] == ["git", "-C", str(repo), "push", "rad"]


def test_on_windows_without_a_native_build_radicle_runs_in_wsl(windows, tmp_path):
    assert not radicle.native()
    radicle.run("rad", "self", home="/home/legion/.radicle")
    cmd, kw = windows[-1]
    assert cmd[:3] == ["wsl.exe", "-d", radicle.DEFAULT_DISTRO] and cmd[-2:] == ["rad", "self"]
    assert kw.get("env") is None


def test_the_setting_beats_what_is_on_the_path(windows, tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rad.exe").write_text("")
    monkeypatch.setattr(radicle, "_cfg", lambda repo, key: "false" if key == "legion.radnative" else None)
    assert not radicle.native("repo")  # a project that keeps WSL even with a Windows build around
    monkeypatch.setenv("LEGION_RAD_NATIVE", "1")
    monkeypatch.setattr(radicle, "_native", {})
    assert radicle.native("repo")


def test_a_project_set_up_with_wsl_stays_there_when_a_windows_build_appears(windows, tmp_path, monkeypatch):
    """Its node ID and its storage are the WSL node's: a native node would be another node altogether."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rad.exe").write_text("")
    monkeypatch.setattr(radicle, "_cfg", lambda repo, key: radicle.DEFAULT_DISTRO
                        if repo == "old" and key == "legion.wsl" else None)
    assert not radicle.native("old")
    assert radicle.native("new")


def test_a_native_node_config_is_read_and_written_as_a_file(windows, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rad.exe").write_text("")
    (tmp_path / "config.json").write_text('{"node": {"network": "main"}}', encoding="utf-8")
    radicle.configure_node(str(tmp_path), listen="0.0.0.0:8776")
    assert '"0.0.0.0:8776"' in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert not windows  # neither cat nor wsl.exe
    with pytest.raises(radicle.RadError, match="absolute"):
        radicle.run("rad", "self", home="relative/home")


def test_a_native_node_that_wont_stop_is_killed(windows, tmp_path, monkeypatch):
    """`rad node stop` on Windows waited for a node that logged its shutdown and never exited."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rad.exe").write_text("")
    home = tmp_path / "home"
    (home / "node").mkdir(parents=True)
    killed = []

    def run(*args, **kw):
        if args[:3] == ("rad", "node", "start"):
            return subprocess.CompletedProcess(args, 0, "✓ Node started. (Process ID: (4242))\n", "")
        if args[:3] == ("rad", "node", "stop"):
            raise radicle.RadError("`rad node stop` timed out after 20s")
        return subprocess.CompletedProcess(args, 0, "", "")

    def proc(cmd, **kw):
        if cmd[0] == "taskkill":
            killed.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, '"radicle-node.exe","4242"' if cmd[0] == "tasklist" else "", "")

    running = iter([False, True])
    monkeypatch.setattr(radicle, "run", run)
    monkeypatch.setattr(radicle, "node_running", lambda repo=None, home=None: next(running, not killed))
    monkeypatch.setattr(radicle.subprocess, "run", proc)
    monkeypatch.setattr(radicle.time, "sleep", lambda s: None)
    radicle.ensure_node(home=str(home))
    assert (home / "node" / "legion-node.pid").read_text() == "4242"
    radicle.restart_node(str(home))
    assert killed == ["4242"]


def test_two_repositories_on_one_node_dont_overwrite_each_other(network):
    """Two agents on one computer share its Radicle node, but each publishes under its own identity."""
    bob, bob2, alice = network["z6MkBob"], network["bob-2"], network["z6MkAlice"]
    boss, b1, b2 = Identity.generate("boss"), Identity.generate("bob#1"), Identity.generate("bob#2")
    coordgit.append(alice, boss, E.create(boss, "project.init", 1, project="p"))
    coordgit.sync(alice, boss)
    coordgit.sync(bob, b1)
    coordgit.sync(bob2, b2)
    coordgit.append(bob, b1, E.create(b1, "task.heartbeat", 2, task="x"))
    coordgit.append(bob2, b2, E.create(b2, "task.heartbeat", 2, task="y"))
    coordgit.sync(bob, b1)
    coordgit.sync(bob2, b2)  # with one prefix per node, this push would hide bob's event
    coordgit.sync(alice, boss)
    assert {e.get("task") for e in coordgit.read_events(alice)} == {None, "x", "y"}
    assert len(radicle.publishers(alice)) == 3


def test_radicle_main_follows_the_official_main_and_never_goes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(radicle, "IS_WINDOWS", False)
    monkeypatch.setattr(radicle, "run", lambda *a, repo=None, home=None, timeout=60, check=True, cwd=None:
                        subprocess.run(list(a), cwd=cwd, capture_output=True, encoding="utf-8"))
    seed = tmp_path / "seed.git"
    git(tmp_path, "init", "-q", "--bare", str(seed))
    git(seed, "config", "receive.advertisePushOptions", "true")  # as Radicle's remote takes -o no-sync
    r = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    git(r, "config", "user.name", "x")
    git(r, "config", "user.email", "x@x")
    git(r, "remote", "add", "rad", str(seed))
    for n in (1, 2):
        (r / "f.txt").write_text(str(n))
        git(r, "add", "f.txt")
        git(r, "commit", "-qm", str(n))
    first, second = git(r, "rev-parse", "HEAD~1"), git(r, "rev-parse", "HEAD")
    git(r, "update-ref", "refs/legion/main", second)
    radicle.publish_main(r)
    assert git(seed, "rev-parse", "refs/heads/main") == second
    git(r, "update-ref", "refs/legion/main", first)  # a repository with an older view of main
    radicle.publish_main(r)  # refused quietly
    assert git(seed, "rev-parse", "refs/heads/main") == second
    assert radicle.main_spec(r) == []                # and not offered again until it moves


def test_judged_patches_are_never_fetched_or_published_again(tmp_path):
    import subprocess
    from legion import coordgit, radicle
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    for name in ("t-1/a-1", "t-2/b-1"):
        subprocess.run(["git", "-C", str(repo), "update-ref", f"refs/legion/patches/{name}", "HEAD"], check=True)
    coordgit.prune_patches(repo, {"refs/legion/patches/t-1/a-1"})
    assert radicle.judged(repo) == {"refs/legion/patches/t-1/a-1"}
    assert radicle.publish_specs(repo, "me") == ["refs/legion/patches/t-2/b-1:refs/legion/patches/t-2/b-1"]
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/legion/patches/t-1/a-1", "HEAD"], check=True)
    assert radicle.publish_specs(repo, "me") == ["refs/legion/patches/t-2/b-1:refs/legion/patches/t-2/b-1"]


def test_tidy_packs_loose_objects_and_refs(tmp_path, monkeypatch):
    import subprocess
    from legion import radicle
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for i in range(6):
        (repo / f"f{i}").write_text(str(i))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", str(i)],
                       check=True)
    monkeypatch.setattr(radicle, "LOOSE_OBJECTS", 5)
    radicle.tidy(repo)
    loose = subprocess.run(["git", "-C", str(repo), "count-objects"], capture_output=True, text=True).stdout.split()[0]
    assert loose == "0" and not list((repo / ".git" / "refs" / "heads").iterdir())


def test_many_refspecs_are_fetched_in_batches_a_command_line_can_hold():
    specs = [f"+refs/legion/patches/{i:040}:refs/legion/patches/{i:040}" for i in range(2000)]
    groups = radicle.batches(specs)
    assert len(groups) > 1 and sum(groups, []) == specs
    assert all(sum(len(s) + 1 for s in g) <= radicle.MAX_ARGS for g in groups)


def test_a_sync_pushes_only_the_patches_the_node_lacks(network):
    """Every patch pushed again on every sync took minutes from /mnt/c: only the new ones go."""
    bob = network["z6MkBob"]
    boss = Identity.generate("boss")
    coordgit.append(bob, boss, E.create(boss, "project.init", 1, project="p"))
    git(bob, "commit", "-q", "--allow-empty", "-m", "one")
    git(bob, "update-ref", "refs/legion/patches/t-1/bob-1", "HEAD")
    assert "refs/legion/patches/t-1/bob-1:refs/legion/patches/t-1/bob-1" in radicle.publish_specs(bob, "b")
    radicle.publish(bob, "b")
    git(bob, "commit", "-q", "--allow-empty", "-m", "two")
    git(bob, "update-ref", "refs/legion/patches/t-1/bob-2", "HEAD")
    assert radicle.publish_specs(bob, "b") == ["refs/legion/pub/*:refs/legion/pub/b/*",
                                               "refs/legion/patches/t-1/bob-2:refs/legion/patches/t-1/bob-2"]


def test_a_fetch_brings_only_what_changed(network, monkeypatch):
    """Fetching every ref again on every command took minutes from /mnt/c, even with nothing new."""
    alice, bob = network["z6MkAlice"], network["z6MkBob"]
    boss = Identity.generate("boss")
    coordgit.append(bob, boss, E.create(boss, "project.init", 1, project="p"))
    coordgit.sync(bob, boss)
    fetches = []
    real = radicle.rgit
    monkeypatch.setattr(radicle, "rgit", lambda repo, *args, **kw: fetches.append(args[0]) or real(repo, *args, **kw))
    radicle.fetch_all(alice)
    assert fetches.count("fetch") == 1      # bob's coord, new here
    fetches.clear()
    radicle.fetch_all(alice)
    assert fetches.count("fetch") == 0      # nothing changed: nothing fetched


def test_a_command_starts_the_network_fetch_in_the_background_and_doesnt_wait(tmp_path, monkeypatch):
    """`rad sync --fetch` took 45 s, twice when it failed, on every command: now it runs in a process of its own,
    once a minute at most, and the next command reads how it went."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    started = []
    monkeypatch.setattr(radicle, "spawn_detached", lambda cmd, cwd, log=None: started.append(cmd))
    assert radicle.kick_network_sync(repo) and len(started) == 1 and started[0][-1] == "netsync"
    assert radicle.kick_network_sync(repo) and len(started) == 1        # one is running: no second one
    monkeypatch.setattr(radicle, "node_running", lambda repo=None, home=None: True)
    monkeypatch.setattr(radicle, "network_sync", lambda repo, force=False: False)
    assert radicle.run_network_sync(repo) is False                     # what the background process does
    assert radicle.kick_network_sync(repo) is False and len(started) == 1  # it failed just now: say so


def test_the_node_drops_what_this_clone_dropped(tmp_path, monkeypatch):
    """Every node kept every save and judged patch it ever published: 24,000 refs, and eleven seconds of each push
    in Radicle's helper going through them."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    git(repo, "update-ref", "refs/legion/patches/t-1/a-2", "HEAD")
    coordgit.prune_patches(repo, {"refs/legion/patches/t-1/a-1"})
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(radicle, "node_patches", lambda repo: {"refs/legion/patches/t-1/a-1": head})
    assert radicle.publish_specs(repo, "me") == ["refs/legion/patches/t-1/a-2:refs/legion/patches/t-1/a-2",
                                                 ":refs/legion/patches/t-1/a-1"]


def test_a_push_doesnt_wait_for_the_seeds_and_one_process_announces(tmp_path, monkeypatch):
    """A push that synced waited ten seconds for the seeds' answer, twice per publish."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    pushed, started, announced = [], [], []
    monkeypatch.setattr(radicle, "rgit", lambda repo, *args, **kw: pushed.append(args))
    radicle._push(repo, ["+a:a"], strict=True)
    assert pushed == [("push", "-q", "-o", "no-sync", "rad", "+a:a")]
    monkeypatch.setattr(radicle, "spawn_detached", lambda cmd, cwd, log=None: started.append(cmd))
    radicle.kick_announce(repo)
    radicle.kick_announce(repo)                       # published again while it runs: it goes again, no second one
    assert started == [[radicle.sys.executable, "-m", "legion", "netsync", "--announce"]]

    monkeypatch.setattr(radicle, "run", lambda *args, **kw: announced.append(args))
    monkeypatch.setattr(radicle, "rid", lambda repo: "rad:z1")
    radicle.run_announce(repo)                        # what that process does: two rounds, the second for the kick
    assert len(announced) == 2 and announced[0][:3] == ("rad", "sync", "--announce")
    assert not radicle.netsync_status(repo, "announce")["running"] and len(started) == 1


def test_a_publish_is_one_push_and_splits_only_when_that_fails(tmp_path, monkeypatch):
    """Radicle's helper spends ten seconds and more on each push before it sends anything: a publish made four."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    pushed, refuse = [], []

    def rgit(repo, *args, **kw):
        pushed.append(args[5:])
        if len(args[5:]) > 1 and refuse:
            raise radicle.RadError("missing necessary objects")
    monkeypatch.setattr(radicle, "rgit", rgit)
    monkeypatch.setattr(radicle, "publish_specs", lambda repo, who: ["refs/legion/pub/*:refs/legion/pub/b/*",
                                                                    "p:p", ":q"])
    monkeypatch.setattr(radicle, "main_spec", lambda repo: ["refs/legion/main:refs/heads/main"])
    monkeypatch.setattr(radicle, "kick_announce", lambda repo: None)
    radicle.publish(repo, "b")
    assert pushed == [("+refs/legion/pub/*:refs/legion/pub/b/*", "+p:p", ":q", "refs/legion/main:refs/heads/main")]
    pushed.clear()
    refuse.append(1)
    git(repo, "config", "--unset", "legion.published")
    radicle.publish(repo, "b")                    # one patch it can't push: its own events still go out alone
    assert pushed[1] == ("+refs/legion/pub/*:refs/legion/pub/b/*",) and ("+p:p",) in pushed and (":q",) in pushed
    refused = radicle.RadError(" * [new reference]  refs/legion/pub/coord -> refs/legion/pub/b/coord\n"
                               " ! [rejected]       refs/legion/main -> main (non-fast-forward)\n")
    assert radicle.only_main_refused(refused)    # the node has a newer main: the rest went through, no second try
    assert not radicle.only_main_refused(radicle.RadError(
        str(refused) + " ! [remote rejected] p -> p (missing necessary objects)\n"))


def test_a_publish_with_nothing_new_pushes_nothing(tmp_path, monkeypatch):
    """Read-only commands paid 10 s to push refs the node already had."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    boss = Identity.generate("boss")
    coordgit.append(repo, boss, E.create(boss, "project.init", 1, project="p"))
    pushes = []
    monkeypatch.setattr(radicle, "rgit", lambda repo, *args, **kw: pushes.append(args[0]))
    monkeypatch.setattr(radicle, "publish_main", lambda repo: None)
    monkeypatch.setattr(radicle, "node_patches", lambda repo: {})
    monkeypatch.setattr(radicle, "kick_announce", lambda repo: None)
    radicle.publish(repo, "b")
    radicle.publish(repo, "b")
    assert pushes == ["push"]                                          # the second had nothing new
    git(repo, "update-ref", "refs/legion/pub/coord", git(repo, "commit-tree", "-m", "merged",
                                                            git(repo, "rev-parse", coordgit.REF + "^{tree}")))
    radicle.publish(repo, "b")
    assert pushes == ["push"]                                          # others' events merged in: nor that
    coordgit.append(repo, boss, E.create(boss, "agent.joined", 2))     # an event of its own: that goes out
    radicle.publish(repo, "b")
    assert pushes == ["push", "push"]


def test_one_process_per_machine_syncs_every_clone_so_commands_dont(network, tmp_path, monkeypatch):
    """Each command copied the storage's refs (6 s through WSL) and pushed (10-20 s), every agent on its own:
    now this machine's sync process does it for all its clones, and a command only reads and writes locally."""
    from legion import daemon, identity
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEGION_NO_DAEMON")
    alice, bob = network["z6MkAlice"], network["z6MkBob"]
    boss, ana = Identity.generate("boss"), Identity.generate("ana")
    identity.bind(alice, boss)
    identity.bind(bob, ana)
    started = []
    monkeypatch.setattr(daemon, "start", lambda: started.append(1))
    assert not daemon.serving(alice) and started == [1]          # not running: this command syncs, and starts it
    daemon.register(bob)
    coordgit.append(alice, boss, E.create(boss, "project.init", 1, project="p"))
    log = []
    daemon.run(rounds=2, log=log.append)
    assert [e["type"] for e in coordgit.read_events(bob)] == ["project.init"] and not log[1:]
    assert daemon.serving(bob) and started == [1]                # its beat is fresh: commands leave it the sync
    from legion.locks import lock_file, unlock_file
    with open(daemon.folder() / "lock", "a+b") as running:      # a daemon alive on this machine holds it
        assert lock_file(running)
        n = len(log)
        assert daemon.run(rounds=1, log=log.append) == 0 and len(log) == n  # a second one makes way
        unlock_file(running)


def test_a_node_prefers_the_projects_own_seed_and_fetches_from_it_first(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    radicle.enable(repo, "rad:zTest")
    config = {"node": {"connect": ["z6MkOther@x.org:8776"]}}
    calls = []
    monkeypatch.setattr(radicle, "_config_path", lambda home: "config.json")
    monkeypatch.setattr(radicle, "_edit_config", lambda home, change: change(config))
    monkeypatch.setattr(radicle, "restart_node", lambda home=None: None)
    monkeypatch.setattr(radicle, "ensure_node", lambda repo=None, home=None: None)
    seed = "z6MkSeed1234@seed.example.org:8776"
    with pytest.raises(radicle.RadError):
        radicle.prefer_seed(repo, "seed.example.org")
    radicle.prefer_seed(repo, seed)
    assert config["node"]["connect"][0] == seed and config["preferredSeeds"] == [seed]

    def run(*args, repo=None, home=None, timeout=60, check=True, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(radicle, "run", run)
    assert radicle.network_sync(repo, force=True)
    assert calls[0][:5] == ("rad", "sync", "--fetch", "rad:zTest", "--seed") and calls[0][5] == "z6MkSeed1234"


def test_a_command_with_no_sync_process_leaves_the_push_to_one_of_its_own(tmp_path, monkeypatch):
    """Where sandboxes kill background processes, every command pushed by itself: 20 s of CPU, and cut off."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    started = []
    monkeypatch.undo()                                                  # the real kick, not conftest's
    monkeypatch.setattr(radicle, "spawn_detached", lambda cmd, cwd, log=None: started.append(cmd[-1]))
    radicle.kick_publish(repo)
    radicle.kick_publish(repo)                                          # one runs: it goes again, no second one
    assert started == ["--publish"] and radicle.netsync_status(repo, "publish")["again"]


def test_a_command_doesnt_wait_long_for_a_clone_another_process_syncs(network, monkeypatch, capsys):
    """A sync process of an older legion held the clone's lock over pushes of minutes, and commands waited two
    minutes for it, past their tools' limit."""
    from legion import ctx, daemon, identity, locks
    alice = network["z6MkAlice"]
    boss = Identity.generate("boss")
    identity.bind(alice, boss)
    coordgit.append(alice, boss, E.create(boss, "project.init", 1, project="p"))
    monkeypatch.chdir(alice)
    monkeypatch.setattr(ctx, "LOCK_WAIT", 0.5)
    synced = []
    monkeypatch.setattr(coordgit, "sync", lambda *a, **kw: synced.append(1) or [])
    with locks.held(daemon.repo_lock(alice)):                          # someone else syncs it right now
        c = ctx.Ctx()
    assert synced == [] and c.st.project["project"] == "p" and "being synced by another process" in capsys.readouterr().err
    ctx.Ctx()
    assert synced == [1]                                                 # free again: it syncs

def test_lock_files_a_node_left_in_its_storage_are_removed(network, monkeypatch):
    """A packed-refs.lock left by a node cut off mid-write made every fetch that deleted a ref fail: two hours
    without seeing anything new."""
    import os
    from legion import daemon
    alice, store = network["z6MkAlice"], radicle.storage(network["z6MkAlice"])
    old, new, gc = Path(store) / "packed-refs.lock", Path(store) / "refs" / "x.lock", Path(store) / "gc.log.lock"
    for f in (old, new, gc):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    held = Path(store) / "refs" / "held.lock"
    held.write_text("")
    for f in (old, gc, held):
        os.utime(f, (0, 0))                                             # left long ago; `new` is in use
    monkeypatch.setattr(daemon, "open_files", lambda: {str(held)})       # a paused writer still holds this one
    monkeypatch.setenv("LEGION_LOCK_CLEANUP", "0")
    assert daemon.clear_stale_locks(alice, log=lambda m: None) == []    # turned off
    monkeypatch.delenv("LEGION_LOCK_CLEANUP")
    assert daemon.clear_stale_locks(alice, log=lambda m: None) == [str(old)]
    assert not old.exists() and new.exists() and gc.exists() and held.exists()
    os.utime(new, (0, 0))
    assert daemon.clear_stale_locks(alice, log=lambda m: None) == []    # looked at lately: not again so soon

def test_a_background_process_the_sandbox_killed_is_started_again_at_once(tmp_path, monkeypatch):
    """Sandboxes kill background processes and leave "running" behind: a publish was taken for under way ten
    minutes after it died, and the node that the network fetch restarts stayed stopped meanwhile."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    started = []

    class Proc:
        pid = 4242
    monkeypatch.setattr(radicle, "spawn_detached", lambda cmd, cwd, log=None, env=None: started.append(cmd[-1]) or Proc())
    monkeypatch.undo()                                                  # the real kick, not conftest's
    monkeypatch.setattr(radicle, "spawn_detached", lambda cmd, cwd, log=None, env=None: started.append(cmd[-1]) or Proc())
    alive = {4242: True}
    monkeypatch.setattr(radicle, "pid_alive", lambda pid: alive.get(pid, True))
    radicle.kick_publish(repo)
    radicle.kick_publish(repo)                                          # alive: it goes again, no second one
    assert started == ["--publish"]
    alive[4242] = False                                                 # killed
    radicle.kick_publish(repo)
    assert started == ["--publish", "--publish"]
    radicle.kick_network_sync(repo)
    alive[4242] = False
    radicle.kick_network_sync(repo)                                     # within the minute, but it died
    assert started.count("netsync") == 2

def test_a_command_with_events_of_its_own_waits_for_their_push(tmp_path, monkeypatch):
    """Where a sandbox kills what runs in the background once the command returns, the push left to a process of
    its own died with it, and nothing the agent recorded went out."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    alive = {7: True}
    monkeypatch.setattr(radicle, "pid_alive", lambda pid: alive[pid])
    monkeypatch.setattr(radicle.time, "sleep", lambda s: alive.update({7: False}))   # it finishes meanwhile
    radicle._write_netsync(repo, {"started": radicle.time.time(), "running": True, "pid": 7}, "publish")
    assert radicle.wait_published(repo, 45)
    alive[7] = True
    monkeypatch.setattr(radicle.time, "sleep", lambda s: None)
    assert not radicle.wait_published(repo, 0)                          # still pushing: the command goes on
