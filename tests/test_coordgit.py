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


def repo_with_log(tmp_path):
    r = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    boss = Identity.generate("boss")
    coordgit.append(r, boss, E.create(boss, "project.init", 1, project="p", integrators=[boss.author]))
    return r, boss


def test_the_cache_follows_a_growing_log(tmp_path):
    r, boss = repo_with_log(tmp_path)
    assert coordgit.state(r).project["project"] == "p"
    for i in range(2, 6):
        coordgit.append(r, boss, E.create(boss, "task.created", i, key=f"t{i}", title="t", zone=["a"], accept="true"))
        assert len(coordgit.state(r).tasks) == i - 1  # only the new file is read, the rest comes from the cache


def test_a_tampered_event_is_checked_again_and_dropped(tmp_path):
    """The cache trusts blob ids, not event ids: changing an event's bytes makes it a new blob."""
    r, boss = repo_with_log(tmp_path)
    ev = E.create(boss, "task.created", 2, key="real", title="t", zone=["a"], accept="true")
    coordgit.append(r, boss, ev)
    assert [t.key for t in coordgit.state(r).tasks.values()] == ["real"]  # now cached as checked
    forged = dict(ev, key="forged")  # same id and signature, different content
    files = coordgit.list_files(r, coordgit.rev(r, coordgit.REF))
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=r, input=E.encode(forged),
                          capture_output=True, check=True).stdout.decode().strip()
    files[E.filename(ev)] = blob
    tip = coordgit.rev(r, coordgit.REF)
    commit = coordgit._commit(r, files, [tip], "forge", coordgit.author_env(boss))
    git(r, "update-ref", coordgit.REF, commit)
    st = coordgit.state(r)
    assert not st.tasks
    assert [why for _, why in st.ignored] == ["the id does not match the content"]


def test_the_state_is_kept_between_commands_and_extended_with_what_is_new(tmp_path):
    """Every command rebuilt the state from every event, and a project's commands grew slower with its history.
    Kept per coord commit, it's extended with the new events when they come after the last it applied, and
    rebuilt whole otherwise: either way the same as building it from scratch."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    boss, ana = Identity.generate("boss"), Identity.generate("ana")

    def same(st):
        full = S.build(coordgit.read_events(repo))
        return ([e["id"] for e in st.events], [e["id"] for e, _ in st.ignored], sorted(st.tasks)) == \
            ([e["id"] for e in full.events], [e["id"] for e, _ in full.ignored], sorted(full.tasks))
    coordgit.append(repo, boss, E.create(boss, "project.init", 1, project="p"))
    coordgit.append(repo, boss, E.create(boss, "task.created", 2, key="a", title="A", zone=["a"], accept="true"))
    first = coordgit.state(repo)
    assert same(first) and coordgit._state_file(repo).exists()
    first.tasks.clear()                                                   # a caller's copy is its own
    assert len(coordgit.state(repo).tasks) == 1
    coordgit.append(repo, ana, E.create(ana, "task.claimed", 3, task=S.task_id(coordgit.read_events(repo)[1]),
                                        ttl=600))
    coordgit._STATE.clear()                                               # another process: from the file
    assert same(coordgit.state(repo))
    coordgit.append(repo, boss, E.create(boss, "task.created", 2, key="b", title="B", zone=["b"], accept="true"))
    assert same(coordgit.state(repo)) and len(coordgit.state(repo).tasks) == 2  # ordered among the old ones
    ev = E.create(boss, "task.created", 9, key="c", title="C", zone=["c"], accept="true")
    trial = coordgit.state(repo, extra=[ev])
    assert len(trial.tasks) == 3 and len(coordgit.state(repo).tasks) == 2    # a trial leaves the kept one
