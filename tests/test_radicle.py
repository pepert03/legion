"""Radicle transport, against a fake Radicle storage: a bare repository with one namespace per node."""

import subprocess

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


def test_a_converted_radicle_home_is_refused():
    with pytest.raises(radicle.RadError, match="MSYS_NO_PATHCONV"):
        radicle.run("true", home="C:/Program Files/Git/home/legion/.radicle")


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
