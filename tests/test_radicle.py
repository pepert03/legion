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
    for nid in ("z6MkAlice", "z6MkBob"):
        r = tmp_path / nid
        git(tmp_path, "init", "-q", "-b", "main", str(r))
        git(r, "config", "user.name", nid)
        git(r, "config", "user.email", f"{nid}@x")
        radicle.enable(r, "rad:zTest")
        git(r, "config", "legion.nid", nid)
        nodes[nid] = r

    def run(*args, repo=None, home=None, timeout=60, check=True, cwd=None):
        p = subprocess.run(list(args), cwd=cwd, capture_output=True, encoding="utf-8", timeout=timeout)
        if check and p.returncode:
            raise radicle.RadError(p.stderr)
        return p

    def publish(repo):  # what the rad remote does: the node's refs land in its own namespace
        nid = git(repo, "config", "legion.nid")
        for prefix in ("refs/legion/pub/", "refs/legion/patches/"):
            if git(repo, "for-each-ref", "--count=1", prefix):
                git(repo, "push", "-q", "--force", str(store), f"{prefix}*:refs/namespaces/{nid}/{prefix}*")

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
    assert coordgit.peers(bob) == ["z6MkAlice"]
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


def test_host_path_is_unchanged_off_windows(monkeypatch):
    monkeypatch.setattr(radicle, "IS_WINDOWS", False)
    assert radicle.host_path("/home/x/repo") == "/home/x/repo"


def test_a_converted_radicle_home_is_refused():
    with pytest.raises(radicle.RadError, match="MSYS_NO_PATHCONV"):
        radicle.run("true", home="C:/Program Files/Git/home/legion/.radicle")
