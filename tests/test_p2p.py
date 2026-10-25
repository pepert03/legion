import subprocess

from legion import coordgit
from legion import events as E
from legion import state as S
from legion.identity import Identity


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def ring(tmp_path, n):
    """n repositories where each one only reads from its two neighbours."""
    nodes = []
    for i in range(n):
        r = tmp_path / f"n{i}"
        git(tmp_path, "init", "-q", "-b", "main", str(r))
        git(r, "config", "user.name", f"n{i}")
        git(r, "config", "user.email", f"n{i}@x")
        nodes.append(r)
    for i, r in enumerate(nodes):
        for j in {(i + 1) % n, (i - 1) % n}:
            coordgit.add_peer(r, f"n{j}", str(nodes[j]))
    return nodes


def rounds(nodes, ident, k):
    for _ in range(k):
        for r in nodes:
            coordgit.sync(r, ident)


def test_events_reach_nodes_that_are_not_directly_connected(tmp_path):
    nodes = ring(tmp_path, 6)
    boss = Identity.generate("boss")
    coordgit.append(nodes[0], boss, E.create(boss, "project.init", 1, project="p"))
    far = nodes[3]  # three hops from node 0
    assert "n0" not in coordgit.peers(far)
    rounds(nodes, boss, 3)
    assert [e["type"] for e in coordgit.read_events(far)] == ["project.init"]


def test_concurrent_events_converge(tmp_path):
    nodes = ring(tmp_path, 4)
    people = [Identity.generate(f"p{i}") for i in range(4)]
    boss = people[0]
    coordgit.append(nodes[0], boss, E.create(boss, "project.init", 1, project="p"))
    rounds(nodes, boss, 2)
    for r, who in zip(nodes, people):  # everyone writes at once, without seeing each other
        coordgit.append(r, who, E.create(who, "attempt.reported", 2, task="x", outcome="failure", notes=who.name))
    rounds(nodes, boss, 3)
    ids = {frozenset(e["id"] for e in coordgit.read_events(r)) for r in nodes}
    assert len(ids) == 1 and len(next(iter(ids))) == 5


def test_patch_commits_travel_by_gossip(tmp_path):
    nodes = ring(tmp_path, 4)
    me = Identity.generate("me")
    (nodes[2] / "f.txt").write_text("hello")
    git(nodes[2], "add", "f.txt")
    git(nodes[2], "commit", "-qm", "patch")
    commit = git(nodes[2], "rev-parse", "HEAD")
    branch = coordgit.patch_branch(nodes[2], "t-1", me.short, 1)
    assert branch.startswith(coordgit.PATCHES)
    coordgit.publish_patch(nodes[2], branch)
    rounds(nodes, me, 2)
    assert coordgit.has_commit(nodes[0], commit)  # node 0 doesn't read directly from node 2


def test_offline_peer_does_not_block(tmp_path):
    nodes = ring(tmp_path, 3)
    coordgit.add_peer(nodes[0], "offline", str(tmp_path / "does-not-exist"))
    ident = Identity.generate("x")
    coordgit.append(nodes[1], ident, E.create(ident, "project.init", 1, project="p"))
    failed = coordgit.sync(nodes[0], ident)
    assert failed == ["offline"]
    assert S.build(coordgit.read_events(nodes[0])).project is not None


def test_main_only_moves_to_signed_tip(tmp_path):
    a = tmp_path / "a"
    git(tmp_path, "init", "-q", "-b", "main", str(a))
    git(a, "config", "user.name", "a")
    git(a, "config", "user.email", "a@x")
    git(a, "commit", "-q", "--allow-empty", "-m", "one")
    tip = git(a, "rev-parse", "HEAD")
    coordgit.update_main(a, "0" * 40)  # a commit we don't have doesn't move main
    assert coordgit.rev(a, coordgit.MAIN) is None
    coordgit.update_main(a, tip)
    assert coordgit.rev(a, coordgit.MAIN) == tip
