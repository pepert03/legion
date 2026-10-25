import subprocess
import threading

from legion import coordgit
from legion import events as E
from legion import state as S
from legion.identity import Identity


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def setup(tmp_path, n):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    clones = []
    for i in range(n):
        c = tmp_path / f"c{i}"
        git(tmp_path, "clone", "-q", str(origin), str(c))
        clones.append(c)
    return origin, clones


def test_concurrent_appends_merge_without_conflicts(tmp_path):
    origin, clones = setup(tmp_path, 4)
    boss = Identity.generate("boss")
    coordgit.append(clones[0], boss, E.create(boss, "project.init", 1, project="p"))
    coordgit.sync(clones[0], boss)

    people = [Identity.generate(f"p{i}") for i in range(4)]

    def worker(repo, ident):
        coordgit.sync(repo, ident)
        for k in range(5):
            st = S.build(coordgit.read_events(repo))
            ev = E.create(ident, "task.created", st.max_lamport + 1, key=f"{ident.name}-{k}",
                          title="x", zone=["x"], accept="true")
            coordgit.append(repo, ident, ev)
            coordgit.sync(repo, ident)

    threads = [threading.Thread(target=worker, args=(c, p)) for c, p in zip(clones, people)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for c, p in zip(clones, people):
        coordgit.sync(c, p)

    events = coordgit.read_events(origin)
    assert len(events) == 1 + 4 * 5
    # Every clone ends up with exactly the same events.
    ids = {frozenset(e["id"] for e in coordgit.read_events(c)) for c in clones}
    assert len(ids) == 1


def test_working_tree_is_untouched(tmp_path):
    origin, (c,) = setup(tmp_path, 1)
    (c / "file.txt").write_text("hello")
    ident = Identity.generate("x")
    coordgit.append(c, ident, E.create(ident, "project.init", 1, project="p"))
    coordgit.sync(c, ident)
    out = subprocess.run(["git", "status", "--porcelain"], cwd=c, capture_output=True, text=True).stdout
    assert out.strip() == "?? file.txt"
