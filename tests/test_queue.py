"""The integrator's merge queue and its persistent review folders, on a real repository."""

import subprocess
import json
import time

import pytest

from legion import coordgit, identity, integrator
from legion import events as E
from legion import state as S
from legion.ctx import Ctx
from legion.identity import Identity

CHECK = '''import pathlib, sys
here = pathlib.Path(__file__).parent
if sys.argv[1] == "--reads":  # what `python check.py X` reads: X's file and this script
    print(sys.argv[2].split()[-1] + ".txt"); print("check.py"); sys.exit(0)
build = here / "build"  # git-ignored: what a check leaves for the next one
build.mkdir(exist_ok=True)
runs = build / "runs"
runs.write_text(str(int(runs.read_text()) + 1) if runs.exists() else "1")
text = (here / (sys.argv[1] + ".txt")).read_text().strip()
if text == "crash":  # what a build that runs out of memory on Windows says
    print("error: Lean exited with code 3221226505")
    sys.exit(1)
if text == "slow":
    import time
    time.sleep(30)
sys.exit(0 if text in ("ok", "slow") else 1)
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    return make_project(tmp_path, monkeypatch)


def make_project(tmp_path, monkeypatch):
    """A p2p repository whose integrator is `boss`, with tasks a and b and a patch for each."""
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "home"))
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "check.py").write_text(CHECK)
    (repo / ".gitignore").write_text("build/\n")
    for k in "ab":
        (repo / f"{k}.txt").write_text("todo\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "start")
    coordgit.add_peer(repo, "offline", str(tmp_path / "nobody"))  # p2p, with nobody to reach
    boss, agent = Identity.generate("boss"), Identity.generate("ana#1")
    others = [Identity.generate("luis#1"), Identity.generate("marta#1")]  # integrators too: trusted verifiers
    identity.bind(repo, boss)
    lamport = iter(range(1, 100))

    def add(who, type, **payload):
        ev = E.create(who, type, next(lamport), **payload)
        coordgit.append(repo, who, ev)
        return ev

    add(boss, "project.init", project="p", integrators=[boss.author] + [o.author for o in others], runners=["python"])
    add(boss, "main.updated", commit=git(repo, "rev-parse", "HEAD"))
    tids = {k: S.task_id(add(boss, "task.created", key=k, title=k, zone=[f"{k}.txt"], accept=f"python check.py {k}"))
            for k in "ab"}

    def patch(key, text):
        git(repo, "checkout", "-q", "--detach", "main")
        (repo / f"{key}.txt").write_text(text)
        git(repo, "-c", "user.name=ana", "-c", "user.email=a@a", "commit", "-qam", key)
        branch = coordgit.patch_branch(repo, tids[key], agent.short, 1)
        coordgit.publish_patch(repo, branch)
        add(agent, "patch.submitted", task=tids[key], commit=git(repo, "rev-parse", "HEAD"), branch=branch)
        git(repo, "checkout", "-q", "main")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(integrator, "REVIEWERS", 2)
    make_project.people = {"boss": boss, "luis": others[0], "marta": others[1]}
    make_project.add = add
    return repo, tids, patch


def verdicts(tids):
    st = Ctx(sync=False).st
    return {k: [p.verdict for p in st.patches_for(t)] for k, t in tids.items()}


def test_two_patches_are_checked_at_once_and_both_go_in(project):
    repo, tids, patch = project
    patch("a", "ok\n")
    patch("b", "ok\n")
    log = []
    integrator.integrate_once(Ctx(), 60, log=log.append)
    assert verdicts(tids) == {"a": ["accepted"], "b": ["accepted"]}
    main = Ctx(sync=False).st.main_tip
    assert {git(repo, "show", f"{main}:{k}.txt") for k in "ab"} == {"ok"}  # b went in on top of a
    reviewers = integrator.common_dir(repo) / "legion-review"
    assert [(reviewers / n / "build" / "runs").read_text() for n in "01"] == ["1", "1"]  # one each, at once


def test_a_failing_patch_is_rejected_and_the_next_one_is_checked_again(project):
    repo, tids, patch = project
    patch("a", "broken\n")
    patch("b", "ok\n")
    log = []
    integrator.integrate_once(Ctx(), 60, log=log.append)
    assert verdicts(tids) == {"a": ["rejected"], "b": ["accepted"]}
    main = Ctx(sync=False).st.main_tip
    assert git(repo, "show", f"{main}:a.txt") == "todo" and git(repo, "show", f"{main}:b.txt") == "ok"


def test_a_review_folder_keeps_its_builds_and_nothing_else(project):
    repo, tids, patch = project
    head = git(repo, "rev-parse", "HEAD")
    dest = integrator.common_dir(repo) / "legion-review" / "0"
    integrator.checkout(repo, head, dest)
    (dest / "build").mkdir()
    (dest / "build" / "cached").write_text("kept")
    (dest / "stray.txt").write_text("left by a check")
    (dest / "a.txt").write_text("changed by a check")
    integrator.checkout(repo, head, dest)
    assert (dest / "build" / "cached").read_text() == "kept"
    assert not (dest / "stray.txt").exists() and (dest / "a.txt").read_text().strip() == "todo"


CONTRACT = '''import pathlib, unittest
ROOT = pathlib.Path(__file__).resolve().parents[2]
class Contract(unittest.TestCase):
    def test_statement(self):
        self.assertEqual((ROOT / "a.txt").read_text().strip(), "proved")
'''


def test_a_contract_decides_the_patch_and_no_patch_can_change_it(project):
    """A check that passes on anything (an empty file) no longer lets a patch in: its contract decides."""
    repo, tids, _ = project
    boss, ana = make_project.people["boss"], Identity.generate("ana#1")
    accept = "python -m unittest discover -s legion_contracts/pinned -p test_*.py"
    tid = S.task_id(make_project.add(boss, "task.created", key="pinned", title="p", zone=["a.txt"], accept=accept,
                                     contract={"test_pinned.py": CONTRACT}))

    def send(files, n):
        git(repo, "checkout", "-q", "--detach", "main")
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=ana", "-c", "user.email=a@a", "commit", "-qm", f"try {n}")
        branch = coordgit.patch_branch(repo, tid, ana.short, n)
        coordgit.publish_patch(repo, branch)
        make_project.add(ana, "patch.submitted", task=tid, commit=git(repo, "rev-parse", "HEAD"), branch=branch)
        git(repo, "checkout", "-q", "main")

    weak = "import unittest\nclass T(unittest.TestCase):\n    def test(self): pass\n"
    send({"a.txt": "nothing\n"}, 1)                                                   # fails the contract
    send({"a.txt": "nothing\n", "legion_contracts/pinned/test_pinned.py": weak}, 2)   # brings its own contract
    integrator.integrate_once(Ctx(), 60, log=lambda *_: None)
    st = Ctx(sync=False).st
    assert [(p.verdict, p.code) for p in st.patches_for(tid)] == [("rejected", "tests"), ("rejected", "zone")]
    send({"a.txt": "proved\n"}, 3)
    integrator.integrate_once(Ctx(), 60, log=lambda *_: None)
    assert [p.verdict for p in Ctx(sync=False).st.patches_for(tid)][-1] == "accepted"
    assert "legion_contracts" not in git(repo, "ls-tree", "-r", "--name-only", Ctx(sync=False).st.main_tip)


def test_a_check_that_runs_no_test_fails_and_quotes_mean_the_same_everywhere(tmp_path):
    """On Windows `-p 'test_*.py'` reached unittest with its quotes and matched nothing: the check passed having
    checked nothing. Single quotes now work there too, and a run of zero tests is a failure."""
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "test_c.py").write_text("import unittest\nclass T(unittest.TestCase):\n"
                                              "    def test(self): self.fail('the contract ran')\n")
    for pattern in ("'test_*.py'", "test_*.py", '"test_*.py"'):
        code, out = integrator.run_cmd(f"python -m unittest discover -s c -p {pattern}", tmp_path, 60)
        assert code == 1 and "the contract ran" in out
    code, out = integrator.run_cmd("python -m unittest discover -s c -p nothing_*.py", tmp_path, 60)
    assert code == 5 and "no test ran" in out


def as_(repo, who):
    identity.bind(repo, make_project.people[who])
    return Ctx()


def runs(repo, reviewer):
    f = integrator.common_dir(repo) / "legion-review" / reviewer / "build" / "runs"
    return int(f.read_text()) if f.exists() else 0


def test_a_patch_verified_by_a_quorum_goes_in_without_a_check(project):
    repo, tids, patch = project
    patch("a", "ok\n")
    for who in ("luis", "marta"):
        job, outcome = integrator.verify_one(as_(repo, who), 60)
        assert (job.task.key, outcome) == ("a", "pass")
    assert integrator.verify_one(as_(repo, "luis"), 60) is None  # already verified on that tree
    c = as_(repo, "boss")
    c.emit("project.policy", spot_check=0)
    integrator.integrate_once(c, 60, log=[].append)
    st = Ctx(sync=False).st
    accepted = st.patches_for(tids["a"], "accepted")
    assert accepted and runs(repo, "0") == 0 and runs(repo, "verify") == 2  # the integrator ran nothing
    ev = next(e for e in st.events if e["type"] == "patch.accepted")
    assert len(ev["verified_by"]) == 2


def test_verifications_of_a_patch_behind_another_still_match(project):
    repo, tids, patch = project
    patch("a", "ok\n")
    patch("b", "ok\n")
    for who in ("luis", "marta"):  # each checks a first, then b on top of a
        assert integrator.verify_one(as_(repo, who), 60)[0].task.key == "a"
        assert integrator.verify_one(as_(repo, who), 60)[0].task.key == "b"
    c = as_(repo, "boss")
    c.emit("project.policy", spot_check=0)
    integrator.integrate_once(c, 60, log=[].append)
    assert verdicts(tids) == {"a": ["accepted"], "b": ["accepted"]} and runs(repo, "0") == 0


def test_a_spot_check_runs_anyway_and_a_false_pass_costs_trust(project):
    from legion import trust
    repo, tids, patch = project
    patch("a", "broken\n")
    c = as_(repo, "luis")
    job = integrator.plan(c, c.st.patches_for(tids["a"])[0])
    for who in ("luis", "marta"):  # two verifiers that lie: they sign a pass without checking
        as_(repo, who).emit("patch.verified", task=tids["a"], patch=job.patch.id, base_tree=job.base_tree,
                            tree=job.tree, outcome="pass")
    c = as_(repo, "boss")
    c.emit("project.policy", spot_check=1)  # every verified patch is checked anyway
    integrator.integrate_once(c, 60, log=[].append)
    st = Ctx(sync=False).st
    assert verdicts(tids)["a"] == ["rejected"] and runs(repo, "0") == 1
    by_agent, _ = trust.compute(st, 0)
    assert by_agent[make_project.people["luis"].author].counts == {"false_pass": 1}


def test_a_standby_integrator_takes_a_subtree(project, monkeypatch):
    repo, tids, patch = project
    monkeypatch.setattr(integrator, "REVIEWERS", 1)  # the lead's next round is one patch: the other subtree is free
    patch("a", "ok\n")
    patch("b", "ok\n")
    boss = as_(repo, "boss")
    # Leases of an hour: on a loaded machine a 300 s one can run out before the boss's round, which then takes it
    assert integrator.hold_lease(boss, 3600)[0]
    luis = as_(repo, "luis")
    assert not integrator.hold_lease(luis, 3600)[0]
    taken = integrator.hold_shards(luis, 3600, 1)
    assert len(taken) == 1
    mine = Ctx(sync=False).st.tasks[taken[0]].key
    integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)
    other = "b" if mine == "a" else "a"
    assert verdicts(tids)[other] == ["accepted"] and verdicts(tids)[mine] == [None]  # the boss left it alone
    integrator.integrate_once(as_(repo, "luis"), 60, log=[].append)
    assert verdicts(tids)[mine] == ["accepted"]
    assert integrator.hold_shards(as_(repo, "luis"), 300, 1) == []  # nothing left there: handed back


def test_a_verified_patch_goes_in_before_a_sketch_moves_main(project):
    repo, tids, patch = project
    patch("a", "ok\n")
    for who in ("luis", "marta"):
        integrator.verify_one(as_(repo, who), 60)
    c = as_(repo, "boss")
    c.emit("project.policy", spot_check=0)
    assert integrator.accept_verified(c, [].append, False, lambda tid: True) == 1  # before anything moves main
    assert verdicts(tids)["a"] == ["accepted"] and runs(repo, "0") == 0


def test_keeping_the_lease_renews_early_retakes_a_lapsed_one_and_syncs_only_then(project, monkeypatch):
    repo, tids, patch = project
    c = as_(repo, "boss")
    assert integrator.hold_lease(c, 300)[0]
    syncs = []
    monkeypatch.setattr(c, "sync", lambda: syncs.append(1) or c.reload())
    integrator.keep_leases(c, 300)
    assert syncs == []  # fresh lease: nothing to publish, no sync
    real = integrator.time.time
    monkeypatch.setattr(integrator.time, "time", lambda: real() + 120)  # past a third of the lease
    integrator.keep_leases(c, 300)
    assert syncs == [1] and [e["type"] for e in c.st.events][-1] == "integrator.renew"
    monkeypatch.setattr(integrator.time, "time", lambda: real() + 1000)  # it lapsed during a long check
    integrator.keep_leases(c, 300)
    assert [e["type"] for e in c.st.events][-1] == "integrator.lease"


def test_a_check_out_of_time_is_retried_then_rejected_as_a_timeout_not_failing_tests(project):
    repo, tids, patch = project
    patch("a", "slow\n")
    integrator.TIMED_OUT.clear()
    log = []
    integrator.integrate_once(as_(repo, "boss"), 2, log=log.append)
    assert verdicts(tids)["a"] == [None] and any("will try again" in m for m in log)
    integrator.integrate_once(as_(repo, "boss"), 2, log=log.append)
    p = Ctx(sync=False).st.patches_for(tids["a"])[0]
    assert (p.verdict, p.code) == ("rejected", "timeout")


def test_a_patch_built_on_a_dependency_in_review_goes_in_after_it(project):
    repo, tids, patch = project
    c3 = S.task_id(make_project.add(make_project.people["boss"], "task.created", key="c", title="c", zone=["c.txt"],
                                    accept="python check.py c", deps=[tids["a"]]))
    (repo / "c.txt").write_text("todo\n")
    git(repo, "add", "c.txt")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "c")
    make_project.add(make_project.people["boss"], "main.updated", commit=git(repo, "rev-parse", "HEAD"))
    patch("a", "ok\n")
    st = Ctx(sync=False).st
    assert st.status(c3, 0) == "open"
    pa = st.review_patches([tids["a"]])[0]
    git(repo, "checkout", "-q", "--detach", pa.commit)           # what `start` does: build on a's patch
    (repo / "c.txt").write_text("ok\n")
    git(repo, "-c", "user.name=ana", "-c", "user.email=a@a", "commit", "-qam", "c on top of a")
    branch = coordgit.patch_branch(repo, c3, "ana", 1)
    coordgit.publish_patch(repo, branch)
    make_project.add(make_project.people["boss"], "patch.submitted", task=c3, commit=git(repo, "rev-parse", "HEAD"),
                     branch=branch)
    git(repo, "checkout", "-q", "main")
    integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)
    st = Ctx(sync=False).st
    assert [p.verdict for p in st.patches_for(tids["a"])] == ["accepted"]
    assert [(p.verdict, p.code) for p in st.patches_for(c3)] == [("accepted", None)]  # a's files aren't held against it


def open_mode(repo, **policy):
    c = as_(repo, "boss")
    c.emit("project.policy", mode="open", quorum=1, **policy)
    c.sync()


def test_open_mode_one_owner_verifies_and_another_finalizes(project):
    repo, tids, patch = project
    open_mode(repo)
    patch("a", "ok\n")
    job, outcome = integrator.verify_one(as_(repo, "luis"), 60)
    assert (job.task.key, outcome) == ("a", "pass")
    assert integrator.finalize(as_(repo, "marta"), [].append) == 1
    st = Ctx(sync=False).st
    p = st.patches_for(tids["a"])[0]
    assert p.verdict == "accepted" and p.verified_by and runs(repo, "0") == 0  # nobody checked it twice
    assert git(repo, "show", f"{st.main_tip}:a.txt") == "ok"
    assert git(repo, "log", "-1", "--format=%an %ad", "--date=iso", st.main_tip).startswith("legion 2000-01-01")


def test_open_mode_a_verification_survives_main_moving_elsewhere(project):
    repo, tids, patch = project
    open_mode(repo, reads="python check.py --reads")
    patch("a", "ok\n")
    patch("b", "ok\n")
    luis = as_(repo, "luis")
    jb = integrator.plan(luis, luis.st.patches_for(tids["b"])[0])
    integrator.sign(luis, jb, 60)                                   # b verified on today's main
    v = Ctx(sync=False).st.verifications[jb.patch.id][0]
    assert set(v["reads"]) == {"b.txt", "check.py"}
    integrator.verify_one(as_(repo, "marta"), 60)                   # a verified too
    assert integrator.finalize(as_(repo, "boss"), [].append) == 2    # a goes in, main moves, b still counts
    assert verdicts(tids) == {"a": ["accepted"], "b": ["accepted"]}


def test_open_mode_audits_catch_a_false_pass_and_revert_it(project):
    repo, tids, patch = project
    open_mode(repo, audit=1)
    patch("a", "broken\n")
    luis = as_(repo, "luis")
    job = integrator.plan(luis, luis.st.patches_for(tids["a"])[0])
    luis.emit("patch.verified", task=tids["a"], patch=job.patch.id, base_tree=job.base_tree, tree=job.tree,
              outcome="pass", reads=["*"])                          # a lie: it never ran the check
    luis.sync()
    integrator.finalize(as_(repo, "marta"), [].append)
    assert verdicts(tids)["a"] == ["accepted"]
    for who in ("boss", "marta"):                                    # two audits by other owners
        job, outcome = integrator.verify_one(as_(repo, who), 60)
        assert outcome == "fail"
    log = []
    integrator.finalize(as_(repo, "boss"), log.append)
    st = Ctx(sync=False).st
    assert st.patches_for(tids["a"])[0].verdict == "reverted" and any("REVERTED" in m for m in log)
    assert git(repo, "show", f"{st.main_tip}:a.txt") == "todo"
    assert make_project.people["luis"].author in st.falsified and not st.trusted(make_project.people["luis"].author) \
        or make_project.people["luis"].author in st.integrators  # a founder stays trusted, but is marked


def test_verify_skips_checks_that_wouldnt_count(project):
    """Three agents built the same Lean patch at once, one of them by the author's own owner: only one of those
    checks could count. verify leaves alone what's already being checked enough, and its own owner's patches."""
    repo, tids, patch = project
    open_mode(repo)
    patch("a", "ok\n")
    patch("b", "ok\n")
    luis = as_(repo, "luis")
    pa = luis.st.patches_for(tids["a"])[0]
    luis.emit("patch.checking", task=tids["a"], patch=pa.id)  # luis is building a's patch right now
    luis.sync()
    job, outcome = integrator.verify_one(as_(repo, "marta"), 60)
    assert (job.task.key, outcome) == ("b", "pass")  # a is in hands enough: marta takes b
    identity.bind(repo, Identity.generate("ana#2"))  # the patches' author (ana#1) has another agent here
    why = {}
    assert integrator.verify_one(Ctx(), 60, why=why) is None
    assert why["by another agent of your owner: your verification wouldn't count"] >= 1


def test_a_check_announced_again_no_longer_holds_the_others_off(project):
    """Announcing a patch again means the first check didn't finish (out of time, or it broke on that machine):
    in Erdős #902 one agent kept doing that on lower-constant, and nobody else checked it for forty minutes."""
    repo, tids, patch = project
    open_mode(repo)
    patch("a", "ok\n")
    luis = as_(repo, "luis")
    pa = luis.st.patches_for(tids["a"])[0]
    luis.emit("patch.checking", task=tids["a"], patch=pa.id)
    luis.sync()
    assert integrator.checks_under_way(Ctx(sync=False).st, time.time(), "marta") == {pa.id: {"luis"}}
    luis = as_(repo, "luis")
    luis.emit("patch.checking", task=tids["a"], patch=pa.id)       # its check didn't finish: it tries again
    luis.sync()
    assert integrator.checks_under_way(Ctx(sync=False).st, time.time(), "marta") == {}
    job, outcome = integrator.verify_one(as_(repo, "marta"), 60)
    assert (job.task.key, outcome) == ("a", "pass")                 # marta checks it meanwhile


def test_an_old_patch_is_audited_once_by_each_node(project):
    """Patches accepted before verdicts recorded their tree: `verify` audited the same one again and again,
    minutes of checking each time, for an event the state then refused."""
    repo, tids, patch = project
    patch("a", "ok\n")
    boss = as_(repo, "boss")
    job = integrator.plan(boss, boss.st.patches_for(tids["a"])[0])
    boss.emit("patch.accepted", patch=job.patch.id, task=tids["a"], merge_commit=job.merged, base=job.head)
    boss.sync()
    open_mode(repo, audit=1)
    assert Ctx(sync=False).st.patches_for(tids["a"])[0].tree is None
    job, outcome = integrator.verify_one(as_(repo, "marta"), 60)
    assert (job.task.key, outcome) == ("a", "pass")
    marta = as_(repo, "marta")
    assert integrator.audit_job(marta, time.time()) is None


def test_a_claim_is_short_and_working_on_the_task_renews_it(project):
    """An agent that stops frees its task within the hour; one that keeps working never has to think of it."""
    from legion.cli import CLAIM_TTL, main
    repo, tids, patch = project
    tid = tids["a"]
    as_(repo, "luis")
    main(["claim", tid])
    claim = lambda: next(c for c in Ctx(sync=False).st.claims[tid] if c.name == "luis#1")  # noqa: E731
    first = claim()
    assert first.ttl == CLAIM_TTL == 30 * 60
    main(["note", tid, "trying the obvious route first"])
    assert claim().last_beat > first.last_beat


def test_saved_work_lets_the_next_agent_carry_on(project, capsys):
    """An agent that stops halfway saves its branch; whoever takes the task next sees it and starts from it,
    instead of the work staying in a clone nobody else can read."""
    from legion.cli import main
    repo, tids, patch = project
    tid = tids["a"]

    def commit(text):
        (repo / "a.txt").write_text(text)
        git(repo, "-c", "user.name=l", "-c", "user.email=l@l", "commit", "-qam", text.strip())

    as_(repo, "luis")
    main(["claim", tid])
    main(["start", tid])
    commit("stage 1\n")
    main(["save", tid, "--notes", "stage 1 compiles; stage 2 left"])
    commit("stage 2, broken\n")
    capsys.readouterr()
    main(["release", tid, "--notes", "stage 2 is broken"])
    assert "nobody else can see" in capsys.readouterr().out  # the second commit wasn't saved
    git(repo, "checkout", "-q", "main")
    as_(repo, "marta")
    main(["claim", tid])
    shown = capsys.readouterr().out
    save = Ctx(sync=False).st.saves(tid)[0]
    assert "Work in progress on this task is saved" in shown and save["id"][:8] in shown and "stage 2 left" in shown
    main(["start", tid, "--from", save["id"][:8]])
    assert (repo / "a.txt").read_text() == "stage 1\n" and "luis" in capsys.readouterr().out


def test_a_save_saved_again_drops_its_ref_and_can_still_be_built_on(project, capsys):
    """Every save stayed published for good: 450 of the 670 patch refs each node held in Erdős #902."""
    from legion.cli import main
    repo, tids, patch = project
    tid = tids["a"]
    as_(repo, "luis")
    main(["claim", tid])
    main(["start", tid])
    for text in ("stage 1\n", "stage 2\n"):
        (repo / "a.txt").write_text(text)
        git(repo, "-c", "user.name=l", "-c", "user.email=l@l", "commit", "-qam", text.strip())
        main(["save", tid])
    first, second = Ctx(sync=False).st.saved[tid]
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/legion/patches/").split()
    assert first["branch"] not in refs and second["branch"] in refs
    git(repo, "checkout", "-q", "main")
    as_(repo, "marta")
    main(["claim", tid])
    main(["start", tid, "--from", first["id"][:8]])                  # its commit is still there, in the later save
    assert (repo / "a.txt").read_text() == "stage 1\n"


def test_saved_work_on_another_task_brings_only_this_zone(project, capsys):
    """Work saved on a broad task (upper-constant) that a narrower task (one of its files) now asks for: whoever
    takes the narrow one is told, and `start --from` brings just its files, so the patch stays in its zone."""
    from legion.cli import main
    repo, tids, patch = project
    big = S.task_id(make_project.add(make_project.people["boss"], "task.created", key="big", title="both",
                                     zone=["a.txt", "b.txt"], accept="python check.py a"))
    as_(repo, "luis")
    main(["claim", big])
    main(["start", big])
    for k in "ab":
        (repo / f"{k}.txt").write_text(f"{k} from luis\n")
    git(repo, "-c", "user.name=l", "-c", "user.email=l@l", "commit", "-qam", "both files")
    main(["save", big, "--notes", "b.txt is finished"])
    git(repo, "checkout", "-q", "main")
    as_(repo, "marta")
    capsys.readouterr()
    main(["claim", tids["b"]])
    save = Ctx(sync=False).st.saves(big)[0]
    shown = capsys.readouterr().out
    assert "Saved work on other tasks" in shown and f"[{save['id'][:8]}] luis#1 on big: b.txt" in shown
    main(["start", tids["b"], "--from", save["id"][:8]])
    assert (repo / "b.txt").read_text() == "b from luis\n" and (repo / "a.txt").read_text().strip() == "todo"
    assert git(repo, "diff", "--name-only", "legion/main", "HEAD") == "b.txt"


def test_a_task_proposed_and_voted_from_the_command_line(project, capsys):
    from legion.cli import main
    repo, tids, patch = project
    open_mode(repo)
    as_(repo, "luis")
    main(["propose", "task", "--key", "voted", "--title", "Born of a vote", "--zone", "v.txt",
          "--accept", "python check.py v"])
    pid = next(iter(Ctx(sync=False).st.proposals))
    assert Ctx(sync=False).st.proposals[pid]["status"] == "open"
    as_(repo, "marta")
    main(["vote", pid[:8], "yes"])
    assert Ctx(sync=False).st.proposals[pid]["status"] == "open"       # 3 active owners: at least 3 yes
    as_(repo, "boss")
    main(["vote", pid[:8], "yes"])
    st = Ctx(sync=False).st
    assert st.proposals[pid]["status"] == "passed" and st.find_task("voted").title == "Born of a vote"
    assert "passed" in capsys.readouterr().out
    # With a contract, after the task above: voters read the contract in full before they vote.
    (repo.parent / "test_next.py").write_text(CONTRACT)
    main(["propose", "task", "--key", "next", "--title", "Pinned", "--zone", "n.txt", "--deps", "voted",
          "--accept", "python -m unittest discover -s legion_contracts/next -p test_*.py",
          "--contract", str(repo.parent / "test_next.py")])
    pid = next(p for p in Ctx(sync=False).st.proposals if Ctx(sync=False).st.proposals[p]["status"] == "open")
    main(["proposals", "--show", pid[:8], "--no-sync"])
    shown = capsys.readouterr().out
    assert "contract: test_next.py" in shown and "legion_contracts/next/test_next.py" in shown
    assert 'assertEqual((ROOT / "a.txt")' in shown
    for who in ("marta", "luis"):
        as_(repo, who)
        main(["vote", pid[:8], "yes"])
    st = Ctx(sync=False).st
    assert st.find_task("next").deps == [st.find_task("voted").id]
    main(["start", "next"])  # the agent works with the contract in its clone, and git leaves it out of commits
    assert (repo / "legion_contracts" / "next" / "test_next.py").read_text() == CONTRACT
    assert git(repo, "status", "--porcelain") == ""
    # A subtask by vote: the key carries its parent's, and it hangs under it once voted in.
    main(["propose", "task", "--parent", "voted", "--key", "part", "--title", "Part", "--zone", "v.txt",
          "--accept", "python check.py v"])
    pid = max(Ctx(sync=False).st.proposals, key=lambda p: Ctx(sync=False).st.proposals[p]["ev"]["lamport"])
    for who in ("marta", "boss"):
        as_(repo, who)
        main(["vote", pid[:8], "yes"])
    st = Ctx(sync=False).st
    assert st.find_task("voted/part").parent == st.find_task("voted").id
    # A key under another task's makes the proposal its subtask, even without --parent.
    main(["propose", "task", "--key", "voted/more", "--title", "More", "--zone", "v.txt",
          "--accept", "python check.py v"])
    pid = max(Ctx(sync=False).st.proposals, key=lambda p: Ctx(sync=False).st.proposals[p]["ev"]["lamport"])
    assert Ctx(sync=False).st.proposals[pid]["ev"]["payload"]["parent"] == st.find_task("voted").id


def test_an_agent_alone_goes_on_over_work_in_review(project, capsys):
    """Nothing waits for a verification: a task that needs another's work, without listing it as a dependency,
    builds on its patch in review (`start --on`); its own patch carries that, is checked once that is accepted,
    and is rejected if that is."""
    from legion.cli import main
    repo, tids, patch = project
    boss = make_project.people["boss"]
    for k in "cd":
        (repo / f"{k}.txt").write_text("todo\n")
    git(repo, "add", "c.txt", "d.txt")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "c and d")
    make_project.add(boss, "main.updated", commit=git(repo, "rev-parse", "HEAD"))
    tc, td = (S.task_id(make_project.add(boss, "task.created", key=k, title=k, zone=[f"{k}.txt"],
                                         accept=f"python check.py {k}")) for k in "cd")
    patch("a", "ok\n")
    patch("b", "broken\n")
    as_(repo, "luis")
    for tid, on in ((tc, "a"), (td, "b")):
        main(["claim", tid])
        main(["start", tid, "--on", on])
        assert (repo / f"{on}.txt").read_text() == ("ok\n" if on == "a" else "broken\n")
        (repo / f"{'c' if tid == tc else 'd'}.txt").write_text("ok\n")
        git(repo, "-c", "user.name=l", "-c", "user.email=l@l", "commit", "-qam", "mine")
        capsys.readouterr()
        main(["submit", tid])  # the other task's file isn't held against this patch's zone
        assert "built on work still in review" in capsys.readouterr().out
        git(repo, "checkout", "-q", "main")
    st = Ctx(sync=False).st
    pa, pb = (st.patches_for(tids[k])[0] for k in "ab")
    assert [st.patches_for(t)[0].on for t in (tc, td)] == [[pa.id], [pb.id]]
    for _ in range(2):
        integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)
    st = Ctx(sync=False).st
    assert [(p.verdict, p.code) for t in (tids["a"], tc, tids["b"], td) for p in st.patches_for(t)] == [
        ("accepted", None), ("accepted", None), ("rejected", "tests"), ("rejected", "base")]
    assert git(repo, "show", "legion/main:c.txt") == "ok"


def test_a_new_agent_gets_work_at_once_greets_the_hub_and_a_stuck_one_goes_back(project, capsys):
    """Whoever joins gets a task at once and is asked to say hello in the hub as it starts (sent there first,
    before any work, agents read for minutes). A failed report sends it back, and what's said there on a task
    reaches whoever claims the task."""
    from legion.cli import main
    repo, tids, patch = project
    identity.bind(repo, Identity.generate("nueva#1"))
    capsys.readouterr()
    assert main(["claim", tids["a"]]) == 0
    said = capsys.readouterr().out
    assert "You're new here: say hello in the hub" in said and "The task is yours" in said
    assert main(["lit", "add", "https://arxiv.org/abs/2101.00001", "--why", "Its counting is what a needs."]) == 2
    main(["lit", "add", "https://arxiv.org/abs/2101.00001", "--title", "Tournaments", "--task", tids["a"],
          "--why", "Its counting is what a needs."])
    main(["chat", "I'll take a, by the counting in Tournaments."])
    capsys.readouterr()
    main(["hub", "--no-sync"])
    shown = capsys.readouterr().out
    assert "Tournaments <https://arxiv.org/abs/2101.00001>" in shown and "I'll take a, by the counting" in shown
    main(["report", tids["a"], "--outcome", "failure", "--notes", "the counting loses a factor 2"])
    assert "Stuck? Take it to the hub" in capsys.readouterr().out
    main(["chat", "--task", tids["a"], "@luis the counting loses a factor 2: did you see a fix?"])
    main(["release", tids["a"]])
    as_(repo, "luis")
    capsys.readouterr()
    main(["context", "--no-update"])
    shown = capsys.readouterr().out
    assert "in the hub, [" in shown and "did you see a fix?" in shown and "legion chat \"...\" --reply" in shown
    main(["claim", tids["a"]])
    shown = capsys.readouterr().out
    assert "The hub has this on this task" in shown and "Tournaments <https://arxiv.org/abs/2101.00001>" in shown


def test_a_result_is_checked_like_a_patch_and_lands_in_main(project, capsys):
    """Proved along the way: a new file under the results folder, published with `result`, checked and merged
    like any patch, under the task it came from."""
    from legion.cli import main
    repo, tids, patch = project
    make_project.add(make_project.people["boss"], "project.policy", results_dir="res",
                     results_check="python check.py {stem}")
    as_(repo, "luis")
    main(["claim", tids["a"]])
    (repo / "res").mkdir()
    (repo / "res" / "lemma.txt").write_text("ok\n")
    capsys.readouterr()
    assert main(["result", tids["a"], "res/lemma.txt", "--title", "The lemma a needs"]) == 0
    assert "Result a/lemma published" in capsys.readouterr().out
    st = Ctx(sync=False).st
    r = next(t for t in st.tasks.values() if t.kind == "result")
    assert r.key == "a/lemma" and r.parent == tids["a"] and st.status(r.id, time.time()) == "review"
    integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)
    st = Ctx(sync=False).st
    assert st.status(r.id, time.time()) == "accepted" and git(repo, "show", "legion/main:res/lemma.txt") == "ok"
    as_(repo, "luis")
    assert main(["result", tids["a"], "res/lemma.txt", "--title", "again"]) == 5  # main has it: only new files


def test_a_check_that_crashes_is_never_signed(project, capsys):
    """A crash or a missing tool says nothing about the patch: in Erdős #902 such runs were signed as failures."""
    from legion.cli import main
    repo, tids, patch = project
    open_mode(repo)
    patch("a", "crash\n")
    job, outcome = integrator.verify_one(as_(repo, "luis"), 60)
    assert outcome == "broken" and Ctx(sync=False).st.verifications.get(job.patch.id, []) == []
    capsys.readouterr()
    main(["verify"])
    assert "broke on this machine" in capsys.readouterr().out


def test_letting_a_task_go_takes_a_report_and_cools_it_down(project, capsys):
    from legion.cli import main
    repo, tids, patch = project
    as_(repo, "luis")
    assert main(["claim", tids["a"]]) == 0
    capsys.readouterr()
    assert main(["release", tids["a"]]) == 2                       # nothing said about what was tried
    assert main(["release", tids["a"], "--notes", "the counting loses a factor 2"]) == 0
    assert main(["claim"]) == 0 and Ctx(sync=False).st.holder(tids["b"], time.time()).name == "luis#1"
    main(["release", tids["b"], "--notes", "no route yet"])
    capsys.readouterr()
    assert main(["claim"]) == 7                                     # what it let go of isn't handed back:
    assert "research work instead" in capsys.readouterr().out       # research work is, instead
    assert main(["claim", tids["a"]]) == 0                          # named, it can take it back


def test_a_task_this_machine_cant_do_isnt_handed_back_and_the_steps_can_be_timed(project, capsys, monkeypatch):
    """Instinct's #6 had no SAT solver: handed the SAT task at every session, its cooldown over between them, it
    let it go every time. And claims of 60 to 120 s, with nothing saying which step they went on."""
    from legion.cli import main
    repo, tids, patch = project
    as_(repo, "luis")
    assert main(["claim", tids["a"]]) == 0
    assert main(["release", tids["a"], "--cant", "a SAT solver"]) == 0   # it says what was missing: no notes needed
    said = capsys.readouterr().out
    assert "won't hand it to you again" in said and "a SAT solver" in said
    c = Ctx(sync=False)
    st = c.st
    assert st.cannot(c.me) == {tids["a"]: "a SAT solver"}
    assert any("lacks a SAT solver" in a["notes"] for a in st.attempts)
    monkeypatch.setattr(S, "COOLDOWN", 0)                                # its cooldown over: still not handed back
    monkeypatch.setenv("LEGION_TIMING", "1")
    assert main(["claim"]) == 0 and Ctx(sync=False).st.holder(tids["b"], time.time()).name == "luis#1"
    err = capsys.readouterr().err
    assert "legion: timing: claim took" in err
    # And what no step of the sync names is said too: of muse's claim of 306 s the steps named 89, and of one of
    # Instinct's 19 s, 0.4. Loading the state, git's calls, and work against waiting (its machine's load was 26).
    assert "legion: timing: loading the state, " in err and "legion: timing: git was called " in err
    assert " on the CPU" in err and "the rest it waited" in err
    main(["release", tids["b"], "--notes", "no route"])
    capsys.readouterr()
    assert main(["claim"]) != 0 or Ctx(sync=False).st.holder(tids["a"], time.time()) is None
    assert main(["claim", tids["a"]]) == 0                               # by name, once it can


def test_an_event_doesnt_wait_two_minutes_for_the_clones_lock_and_whoever_holds_it_is_named(project, capsys, monkeypatch):
    """On muse's machine something held a clone's lock through a whole claim: 300 s, most of them the event
    waiting for the lock in silence, and nothing said who had it."""
    from legion import ctx, locks, transport
    from legion.cli import main
    repo, tids, patch = project
    as_(repo, "luis")
    lock = transport.repo_lock(repo)
    assert locks.holder(lock) == ""
    monkeypatch.setattr(ctx, "RECORD_WAIT", 1.2)
    monkeypatch.setenv("LEGION_TIMING", "1")
    monkeypatch.setenv("LEGION_SYNC", "off")  # (the sync waits for the lock too: here, only the event)
    before = len(Ctx(sync=False).st.events)
    with locks.held(lock, what="the sync process copying from the node's storage") as got:
        assert got and "the sync process copying from the node's storage, for " in locks.holder(lock)
        started = time.time()
        assert main(["chat", "recorded all the same"]) == 0
        assert time.time() - started < 30
    err = capsys.readouterr().err
    assert "waiting for the clone's lock, to record (not got: pid " in err and "the sync process copying" in err
    assert locks.holder(lock) == "" and len(Ctx(sync=False).st.events) == before + 1


def test_an_agent_going_round_in_circles_is_told_and_handed_another_task(project, capsys, monkeypatch):
    """The viewer said "Plateaued?" of fifteen agents in Erdős #902, and nothing told them: Instinct's and Muse's
    loops kept one session going, claiming and reporting the same tasks scores of times a day with no patch."""
    from legion.cli import main
    repo, tids, patch = project
    monkeypatch.setattr(S, "SPINNING", 2)
    as_(repo, "luis")
    assert main(["claim", tids["a"]]) == 0
    assert main(["release", tids["a"], "--notes", "the counting loses a factor 2"]) == 0
    capsys.readouterr()
    assert main(["context", "--no-update"]) == 0
    said = capsys.readouterr().out
    assert "## You may have plateaued" in said and "2 claims and reports in the last day and no patch (mostly on a)"         in said and "end this session and start a fresh one" in said
    monkeypatch.setattr(S.State, "cooling", lambda self, author, now: set())  # a may be handed back now
    assert main(["claim"]) == 0
    said = capsys.readouterr().out
    assert "You may have plateaued" in said and Ctx(sync=False).st.holder(tids["b"], time.time()).name == "luis#1"
    monkeypatch.setenv("LEGION_RUN", "1")
    from legion.cli import plateau_text
    c = Ctx(sync=False)
    assert "`legion run` starts a fresh one" in plateau_text(c.st, c.me)
    as_(repo, "marta")
    capsys.readouterr()
    main(["context", "--no-update"])
    assert "plateaued" not in capsys.readouterr().out                # not of an agent that hasn't


def test_a_subagent_is_vouched_for_by_its_owner_on_the_same_computer(project):
    from legion.cli import main
    repo, tids, patch = project
    open_mode(repo)
    as_(repo, "luis")
    main(["context", "--no-update"])
    sub = Identity.generate("luis#2")
    identity.bind(repo, sub)
    main(["context", "--no-update"])
    st = Ctx(sync=False).st
    assert st.delegates[sub.author] == make_project.people["luis"].author and st.trusted(sub.author)


def test_checks_wait_for_a_free_slot_on_the_machine(tmp_path, monkeypatch):
    import threading
    monkeypatch.setenv("LEGION_HOME", str(tmp_path))
    monkeypatch.setenv("LEGION_CHECKS", "1")
    times = {}

    def hold(name, secs):
        with integrator.check_slot():
            start = time.time()
            time.sleep(secs)
            times[name] = (start, time.time())

    first = threading.Thread(target=hold, args=("a", 0.8))
    first.start()
    time.sleep(0.2)
    hold("b", 0)
    first.join()
    assert times["b"][0] >= times["a"][1]


def test_main_publish_refuses_a_commit_not_built_on_the_current_main(project, capsys):
    """Published over an acceptance that landed meanwhile, it would drop that patch from main."""
    from legion.cli import main
    repo, tids, patch = project
    patch("a", "ok\n")
    integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)    # main moves: a goes in
    git(repo, "checkout", "-q", "main")                                 # a clone still on the old main
    (repo / "notes.txt").write_text("for the agents\n")
    git(repo, "add", "notes.txt")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "notes")
    capsys.readouterr()
    assert main(["main", "publish"]) == 5
    assert "isn't built on the current main" in capsys.readouterr().err
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "merge", "-q", "--no-edit", "legion/main")
    assert main(["main", "publish"]) == 0
    st = Ctx(sync=False).st
    assert st.main_tip == git(repo, "rev-parse", "HEAD") and git(repo, "show", f"{st.main_tip}:a.txt") == "ok"


def test_a_check_that_doesnt_finish_here_doesnt_stop_a_submit(project, capsys):
    """Out of time, or a test's own time limit on a busy machine: that says nothing about the patch, so it goes
    to the verifiers instead of being refused."""
    from legion.cli import main
    repo, tids, patch = project
    as_(repo, "luis")
    main(["claim", tids["a"]])
    main(["start", tids["a"]])
    (repo / "a.txt").write_text("slow\n")
    git(repo, "-c", "user.name=l", "-c", "user.email=l@l", "commit", "-qam", "slow")
    capsys.readouterr()
    assert main(["submit", tids["a"], "--timeout", "2"]) == 0
    assert "didn't finish here (out of time)" in capsys.readouterr().err
    assert [p.author for p in Ctx(sync=False).st.patches_for(tids["a"])] == [make_project.people["luis"].author]


def test_a_result_brings_what_reproduces_it(project):
    from legion.cli import main
    repo, tids, patch = project
    make_project.add(make_project.people["boss"], "project.policy", results_dir="res",
                     results_check="python check.py {stem}")
    as_(repo, "luis")
    main(["claim", tids["a"]])
    (repo / "res").mkdir()
    (repo / "res" / "count.txt").write_text("ok\n")
    (repo / "res" / "count_gen.py").write_text("print('the search that found it')\n")
    assert main(["result", tids["a"], "res/count.txt", "--title", "The count", "--with", "res/count_gen.py"]) == 0
    integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)
    st = Ctx(sync=False).st
    r = next(t for t in st.tasks.values() if t.kind == "result")
    assert st.status(r.id, time.time()) == "accepted" and r.zone == ["res/count.txt", "res/count_gen.py"]
    assert "the search that found it" in git(repo, "show", "legion/main:res/count_gen.py")
    q = next(iter(st.questions.values()))
    assert q["result"] == r.id and q["status"] == "open"     # a computed result asks for its replication


def test_with_no_task_free_claim_hands_out_a_question(project, capsys):
    from legion.cli import main
    repo, tids, patch = project
    as_(repo, "luis")
    main(["claim", tids["a"]])
    (repo.parent / "brief.md").write_text("## Known\n- the counting gives 1/2\n## Open\n- is it tight?\n")
    main(["brief", tids["a"], "--edit", str(repo.parent / "brief.md")])
    main(["release", tids["a"], "--notes", "the counting isn't tight enough"])
    main(["claim", tids["b"]])
    main(["release", tids["b"], "--notes", "no route"])
    as_(repo, "marta")
    main(["ask", tids["a"], "Is the counting tight at d = 6?"])
    capsys.readouterr()
    assert main(["claim", tids["a"]]) == 0
    assert "The state of the art of a, version 1, by luis#1" in capsys.readouterr().out
    as_(repo, "luis")                                                   # a and b: let go of in the last hour
    capsys.readouterr()
    assert main(["claim"]) == 7
    shown = capsys.readouterr().out
    assert "here is a question on one" in shown and "Is the counting tight at d = 6?" in shown
    qid = next(iter(Ctx(sync=False).st.questions))
    assert main(["answer", qid[:8], "Yes, by the double count.", "--settled"]) == 0
    assert Ctx(sync=False).st.questions[qid]["status"] == "settled"


def test_claim_hands_out_the_research_work_most_wanted(project, capsys):
    from legion.cli import main
    repo, tids, patch = project
    for who, text in (("marta", "Count triples instead of pairs"), ("boss", "A Cayley construction")):
        as_(repo, who)
        main(["idea", tids["a"], text])
    as_(repo, "luis")
    for tid in tids.values():
        main(["claim", tid])
        main(["release", tid, "--notes", "no route of my own"])
    capsys.readouterr()
    assert main(["claim", "--json"]) == 7                              # nothing free: judge two ideas first
    import json
    work = json.loads(capsys.readouterr().out)["work"]
    assert work == {"kind": "compare", "task": "a"}
    better, worse = [i["ev"]["id"][:8] for i in Ctx(sync=False).st.ideas_for(tids["a"])]
    assert main(["compare", better, worse, "--why", "triples see more structure"]) == 0
    capsys.readouterr()
    assert main(["claim", "--json"]) == 7
    assert json.loads(capsys.readouterr().out)["work"]["kind"] == "ideas"   # then new routes where few are left
    as_(repo, "marta")
    main(["idea", tids["b"], "A probabilistic deletion argument"])     # a new idea on b, which luis let go of
    as_(repo, "luis")
    capsys.readouterr()
    assert main(["claim"]) == 0                                         # b comes back to luis, with its route
    assert "A probabilistic deletion argument" in capsys.readouterr().out


def test_a_refutation_closes_a_task_whose_statement_is_false(project, capsys):
    """Proved false: checked with the task's refute command instead of its acceptance, it closes the task."""
    from legion.cli import main
    repo, tids, patch = project
    c = S.task_id(make_project.add(make_project.people["boss"], "task.created", key="c", title="c",
                                   zone=["c.txt", "c_neg.txt"], accept="python check.py c",
                                   refute="python check.py c_neg"))
    as_(repo, "luis")
    assert main(["claim", c]) == 0
    assert "refutation:  python check.py c_neg" in capsys.readouterr().out
    main(["start", c])
    (repo / "c.txt").write_text("false\n")
    (repo / "c_neg.txt").write_text("ok\n")
    git(repo, "add", "c.txt", "c_neg.txt")
    git(repo, "-c", "user.name=l", "-c", "user.email=l@l", "commit", "-qm", "c is false")
    assert main(["submit", c]) == 6                                     # as a proof of c, it fails
    assert main(["submit", c, "--refutes"]) == 0
    integrator.integrate_once(as_(repo, "boss"), 60, log=[].append)
    st = Ctx(sync=False).st
    assert st.status(c, time.time()) == "refuted" and not st.done(c)
    assert git(repo, "show", "legion/main:c_neg.txt") == "ok"         # the refutation is in main
    as_(repo, "luis")
    capsys.readouterr()
    assert main(["submit", c]) == 3
    assert "was refuted" in capsys.readouterr().err


def test_verify_runs_in_the_background_and_says_how_it_went(project, capsys, monkeypatch):
    """A Lean check takes minutes, longer than many agents' tools let a command run: they cut it off, and no
    verification was ever signed."""
    from legion import radicle
    from legion.cli import main
    repo, tids, patch = project
    open_mode(repo)
    patch("a", "ok\n")
    as_(repo, "luis")
    spawned = []

    def spawn(cmd, cwd, log=None):  # run it right here, the way the detached process would
        spawned.append(cmd)
        import contextlib
        with open(log, "a", encoding="utf-8") as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
            main(cmd[cmd.index("verify"):])

    monkeypatch.setattr(radicle, "spawn_detached", spawn)
    capsys.readouterr()
    assert main(["verify", "--background"]) == 0
    assert "Verifying in the background" in capsys.readouterr().out and spawned[0][-1] == "--record"
    assert main(["verify", "--status"]) == 0
    shown = capsys.readouterr().out
    assert "Verified patch" in shown and "verify finished (exit 0)" in shown
    assert [v["outcome"] for v in Ctx(sync=False).st.verifications[Ctx(sync=False).st.patches_for(tids["a"])[0].id]] \
        == ["pass"]


def test_run_keeps_an_agent_at_work_until_the_project_is_done_for_it(project, monkeypatch, capsys):
    """Agents stopped between batches, when a session or a turn ended, until their person started them again."""
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    ran, slept = [], []

    def agent(argv):
        ran.append(argv[1:])
        return 1                                                        # each run ends at once: out of tokens?
    monkeypatch.setattr(cli, "run_agent", agent)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    assert cli.main(["run", "--max-runs", "3", "--", "my-agent", "-p", "Read AGENTS.md"]) == 0
    assert ran == [["-p", "Read AGENTS.md"]] * 3
    assert slept == [cli.RUN_BACKOFF, 2 * cli.RUN_BACKOFF]               # quick stops wait longer each time
    assert "quickly" in capsys.readouterr().out
    monkeypatch.setattr(S.State, "exclusive_done", lambda self: True)  # every task done: it doesn't start it
    ran.clear()
    assert cli.main(["run", "--", "my-agent"]) == 0 and ran == []


def test_run_stops_an_agent_that_runs_again_and_again_without_doing_anything(project, monkeypatch, capsys):
    """Two thirds of a plateaued agent's spend went on turns that proposed nothing (arXiv 2610.20563). And under
    run, the agent is told to end its session after each task: run starts it again with a clean context."""
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    ran, run_agent = [], cli.run_agent
    monkeypatch.setattr(cli, "run_agent", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["run", "--max-runs", "10", "--", "my-agent"]) == 3 and len(ran) == cli.RUN_EMPTY
    assert "without recording anything" in capsys.readouterr().out
    envs = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, env=None, **k: envs.append(env) or subprocess.CompletedProcess(argv, 0))
    run_agent(["my-agent"])
    monkeypatch.undo()
    assert envs[0]["LEGION_RUN"] == "1"
    monkeypatch.setenv("LEGION_RUN", "1")
    assert "end this session" in cli.keep_going() and "end this session" in cli.keep_going(start=True)


def test_a_competitive_candidate_that_gains_nothing_keeps_its_metric_for_the_pool(project, capsys):
    """A team shares its candidates and their scores (arXiv 2610.20563): one that doesn't beat the best is still
    one to look at, with its metric, and so are the lines it changes."""
    from legion import cli
    repo, tids, _ = project
    boss, luis = make_project.people["boss"], make_project.people["luis"]
    ana = Identity.generate("ana#2")
    (repo / "c.txt").write_text("10\n")
    (repo / "metric.py").write_text("print(open('c.txt').read())\n")
    (repo / "holdout.py").write_text("import sys; sys.exit(open('c.txt').read().strip() == '6')\n")  # 6 games it
    git(repo, "add", "c.txt", "metric.py", "holdout.py")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "c")
    make_project.add(boss, "main.updated", commit=git(repo, "rev-parse", "HEAD"))
    tid = S.task_id(make_project.add(boss, "task.created", key="c", title="c", zone=["c.txt"], accept="python -c pass",
                                     mode="competitive", metric="python metric.py", goal="min",
                                     holdout="python holdout.py"))

    def candidate(who, value):
        main = Ctx(sync=False).st.main_tip
        git(repo, "checkout", "-q", "--detach", main)
        (repo / "c.txt").write_text(value)
        git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", value)
        branch = coordgit.patch_branch(repo, tid, who.short, 1)
        coordgit.publish_patch(repo, branch)
        make_project.add(who, "patch.submitted", task=tid, commit=git(repo, "rev-parse", "HEAD"), branch=branch,
                         lines=cli.changed_lines(repo, main))
        git(repo, "checkout", "-q", "main")
        integrator.integrate_once(Ctx(), 60, log=lambda line: None)

    candidate(ana, "7\n")
    candidate(luis, "8\n")
    candidate(make_project.people["marta"], "6\n")  # a better metric, but the holdout check fails
    st = Ctx(sync=False).st
    assert [(p.verdict, p.code, p.metric, p.lines) for p in st.patches_for(tid)] == [
        ("accepted", None, 7.0, 2), ("rejected", "no_gain", 8.0, 2), ("rejected", "holdout", None, 2)]
    assert [p.metric for p in st.pool(tid)] == [7.0, 8.0]
    capsys.readouterr()
    assert cli.main(["pool", tid]) == 0
    shown = capsys.readouterr().out
    assert "round 1" in shown and "8 by luis#1" in shown and "no gain" in shown


def test_events_stream_and_recording_commands_answer_in_json(project, capsys):
    """Small tools that compose: a program reads the project as a stream of events and drives it through
    commands that answer in JSON."""
    import json
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    capsys.readouterr()
    assert cli.main(["events", "--no-sync", "--type", "task.created"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(x)["key"] for x in lines] == ["a", "b"]
    assert cli.main(["idea", tids["a"], "prove it by induction on n", "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["code"] == 0 and [e["type"] for e in answer["events"]] == ["idea.proposed"]
    assert cli.main(["idea", tids["a"], "Prove it by induction on n!", "--json"]) == 5  # the same route again
    answer = json.loads(capsys.readouterr().out)
    assert answer["events"] == [] and "nearly the same route" in answer["error"]


def test_with_sync_off_a_command_does_what_it_says_and_nothing_on_the_network(project, monkeypatch, capsys):
    """A command that pulled, pushed, cleaned locks and pruned branches besides its own work surprised the
    sandboxes it ran in; LEGION_SYNC=off leaves all that to `legion netsync` or the sync process."""
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    touched = []
    monkeypatch.setattr(coordgit, "sync", lambda *a, **kw: touched.append("sync") or [])
    monkeypatch.setattr(coordgit, "prune_patches", lambda *a, **kw: touched.append("prune"))
    monkeypatch.setenv("LEGION_SYNC", "off")
    assert cli.main(["idea", tids["a"], "a route nobody tried"]) == 0
    assert touched == [] and "stay in this clone" in capsys.readouterr().err
    assert [i["ev"]["text"] for i in Ctx(sync=False).st.ideas_for(tids["a"])] == ["a route nobody tried"]


def test_a_patch_whose_work_is_in_main_already_is_accepted_not_rejected_as_empty(project):
    """In Erdős #902 two acceptances were dropped when late events moved main before them, but their merges were in
    main: checked again, the patches "changed nothing", were rejected, and the tasks opened to be done again."""
    repo, tids, patch = project
    patch("a", "ok\n")
    st = Ctx(sync=False).st
    p = st.patches_for(tids["a"])[0]
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-edit", p.commit)                     # in main, by a verdict that didn't count
    make_project.add(make_project.people["boss"], "main.updated", commit=git(repo, "rev-parse", "HEAD"))
    integrator.integrate_once(Ctx(), 60, log=lambda line: None)
    assert verdicts(tids)["a"] == ["accepted"]


def test_each_authors_latest_save_replaces_the_earlier_ones_and_big_files_are_refused(project, capsys):
    """A thousand earlier saves stayed in Erdős #902, s-four's with SAT certificates of 50 to 200 MB each."""
    from legion import cli
    repo, tids, patch = project
    old, new = ({"branch": f"refs/legion/patches/{tids['a']}/wip-x-{n}", "commit": c * 40} for n, c in ((1, "a"), (2, "b")))
    assert coordgit.superseded([(old, new)]) == {old["branch"]}
    assert coordgit.superseded([(new, new)]) == set()
    as_(repo, "luis")
    make_project.add(make_project.people["luis"], "task.claimed", task=tids["a"], ttl=600)
    git(repo, "checkout", "-q", "-b", "work/a", "main")
    (repo / "a.txt").write_bytes(b"x" * (S.MAX_FILE + 1))
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "big")
    capsys.readouterr()
    assert cli.main(["save", tids["a"], "--notes", "a certificate"]) == 5
    assert "over 20 MB" in capsys.readouterr().err


def test_checks_read_utf8_files_the_way_tests_written_on_linux_expect(tmp_path):
    """A Windows verifier rejected a sound patch: its test read the project's UTF-8 file with read_text() and no
    encoding, which on Windows means the legacy codepage, where bytes of characters such as ∏ and ℝ mean nothing."""
    text = "∀ n ≥ 5: ∏ ∈ ℝ — é\n"
    (tmp_path / "f.txt").write_bytes(text.encode("utf-8"))
    (tmp_path / "check.py").write_text(f"import pathlib\nassert pathlib.Path('f.txt').read_text() == {text!r}\n",
                                       encoding="utf-8")
    code, out = integrator.run_cmd("python check.py", tmp_path, 60, slot=False)
    assert code == 0, out


def test_open_mode_the_quorum_is_the_trusted_owners_there_are(project):
    """A project of one founder, or of one person's agents, could never gather two trusted owners behind a
    patch: nothing was ever accepted, and nobody became trusted. The quorum is capped at the owners there are."""
    repo, tids, patch = project
    c = as_(repo, "boss")
    c.emit("project.policy", mode="open", quorum=9)                   # trusted owners: boss, luis and marta
    c.sync()
    patch("a", "ok\n")
    assert Ctx(sync=False).st.quorum_for("ana#1") == 3
    for who in ("luis", "marta"):
        assert integrator.verify_one(as_(repo, who), 60)[1] == "pass"
    assert integrator.finalize(as_(repo, "boss"), [].append) == 0      # two of the three: not yet
    assert integrator.verify_one(as_(repo, "boss"), 60)[1] == "pass"
    assert integrator.finalize(as_(repo, "boss"), [].append) == 1
    assert verdicts(tids)["a"] == ["accepted"]


def test_run_waits_for_work_between_sessions_that_found_nothing_open(project, monkeypatch, capsys):
    """An agent whose claim found nothing open stopped at once, and run took it for failing: it waited longer
    each time, half an hour in the end, and stopped it after three such runs. run itself waits now, syncing,
    until something is open, and starts the agent then: the waiting costs no tokens there."""
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    ran, slept = [], []

    def agent(argv):
        ran.append(argv)
        cli.note_claim(repo, 5 if len(ran) == 1 else 0)               # the first session: nothing open
        return 0

    def sleep(s):                                                      # while run waits, a task appears
        slept.append(s)
        make_project.add(make_project.people["boss"], "task.created", key="c", title="c", zone=["c.txt"],
                         accept="python check.py c")
    monkeypatch.setattr(cli, "run_agent", agent)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["run", "--max-runs", "2", "--pause", "7", "--", "my-agent"]) == 0
    assert len(ran) == 2 and slept == [7]                              # one look, and the task was there
    assert "nothing was open" in capsys.readouterr().out


def test_in_the_open_mode_run_finalizes_what_the_others_verified(project, monkeypatch, capsys):
    """Only `verify` and `integrate --loop` finalized, and while every agent was busy on a task nobody ran them:
    verified patches waited out of main. Every trusted agent's `legion run` finalizes between its sessions now,
    no node more central than another."""
    from legion import cli
    repo, tids, patch = project
    c = as_(repo, "boss")
    c.emit("project.policy", mode="open", quorum=3)                   # trusted owners: boss, luis and marta
    c.sync()
    patch("a", "ok\n")
    for who in ("luis", "marta", "boss"):
        assert integrator.verify_one(as_(repo, who), 60)[1] == "pass"  # verified, and nobody finalized
    assert verdicts(tids)["a"] == [None]
    as_(repo, "luis")
    monkeypatch.setattr(cli, "run_agent", lambda argv: 0)
    assert cli.main(["run", "--max-runs", "1", "--", "my-agent"]) == 0
    assert verdicts(tids)["a"] == ["accepted"]


def test_a_claim_lost_to_another_agent_takes_another_task_in_the_same_command(project, monkeypatch, capsys):
    """A lost race (exit 3) sent the agent round again, a whole turn of its own, for another free task it could have
    been handed at once. And what it let go of because someone claimed it first doesn't cool."""
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    real, lost = cli.Ctx.holder_is_me, []

    def rigged(self, tid):  # the first task it claims goes to someone else
        mine = [cl for cl in self.st.claims.get(tid, ()) if cl.author == self.me]
        if not lost and mine:
            lost.append(tid)
            return False
        return real(self, tid)
    monkeypatch.setattr(cli.Ctx, "holder_is_me", rigged)
    capsys.readouterr()
    assert cli.main(["claim", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["won"] is True                               # one answer, the task it got
    st = Ctx(sync=False).st
    me = make_project.people["luis"].author
    assert [cl.lost for cl in st.claims[lost[0]] if cl.author == me][0] is True
    assert lost[0] not in st.cooling(me, time.time())


def test_with_nothing_else_open_claim_hands_back_what_was_let_go_of(project, monkeypatch, capsys):
    """An hour of cooldown kept agents idle with the task open and nothing else to do: claim hands it back, asking
    for another route."""
    from legion import cli
    from legion.cli import main
    repo, tids, patch = project
    monkeypatch.setattr(cli, "research_work", lambda c, args, now: None)  # no research wanted either
    patch("b", "ok\n")                                                   # b in review
    as_(repo, "luis")
    main(["claim", tids["a"]])
    main(["release", tids["a"], "--notes", "the counting isn't tight enough"])
    capsys.readouterr()
    assert main(["claim"]) == 0
    shown = capsys.readouterr().out
    assert "this is the task you let go of" in shown and "The task is yours" in shown


def test_who_joins_a_task_splits_the_work_and_reads_the_notes_whole(project, capsys):
    from legion.cli import main
    repo, tids, patch = project
    patch("b", "ok\n")                                                   # b in review: nothing open but a
    as_(repo, "luis")
    main(["claim", tids["a"]])
    long_note = "I take the base case; " + "the induction step needs the counting lemma. " * 12
    main(["note", tids["a"], long_note])
    as_(repo, "marta")
    capsys.readouterr()
    assert main(["claim", tids["a"]]) == 0                             # held by luis: marta joins it
    shown = capsys.readouterr().out
    assert "split the work with them, don't race them" in shown and long_note.strip() in shown
    as_(repo, "luis")
    capsys.readouterr()
    main(["heartbeat", tids["a"]])
    assert "Working on it with you: marta#1" in capsys.readouterr().out


def test_a_trusted_agent_on_a_task_splits_it_with_propose(project, capsys):
    from legion.cli import main
    repo, tids, patch = project
    as_(repo, "luis")
    main(["claim", tids["a"]])
    capsys.readouterr()
    assert main(["propose", "task", "--parent", tids["a"], "--key", "base", "--title", "The base case",
                 "--zone", "a.txt", "--accept", "python check.py a"]) == 0
    assert "Created a/base, a subtask of a" in capsys.readouterr().out
    st = Ctx(sync=False).st
    sub = st.find_task("a/base")
    assert sub.parent == tids["a"] and st.status(sub.id, time.time()) == "open"


def test_run_keeps_its_agents_claims_while_it_runs_and_lets_them_go_when_stopped(project):
    from legion import cli
    repo, tids, patch = project
    as_(repo, "luis")
    cli.main(["claim", tids["a"]])

    class Once:  # one round of renewals, then stop
        n = 0

        def wait(self, seconds):
            self.n += 1
            return self.n > 1
    own = [0]
    cli.renew_while(Once(), own)
    st = Ctx(sync=False).st
    me = make_project.people["luis"].author
    assert any(e["type"] == "task.heartbeat" and e["author"] == me for e in st.events) and own == [1]
    cli.release_held("legion run was stopped")
    assert Ctx(sync=False).st.holder(tids["a"], time.time()) is None


def test_what_run_records_itself_doesnt_make_an_idle_agents_runs_count(project, monkeypatch):
    """An integrator's run took the lease after each session, whatever waited, and renews its agent's claims
    during them: those events of its own reset the count of runs that recorded nothing, and an agent doing
    nothing was started again for ever."""
    from legion import cli
    repo, tids, patch = project
    patch("a", "ok\n")                    # something for luis, an integrator, to integrate between sessions
    as_(repo, "luis")
    cli.main(["claim", tids["b"]])        # and a task its run renews while the agent runs
    nap, ran = time.sleep, []
    monkeypatch.setattr(cli, "RUN_BEAT", 0.05)
    monkeypatch.setattr(cli, "run_agent", lambda argv: ran.append(argv) or nap(0.5) or 0)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["run", "--max-runs", "6", "--", "my-agent"]) == 3 and len(ran) == cli.RUN_EMPTY
    st, me = Ctx(sync=False).st, make_project.people["luis"].author
    assert any(e["type"] == "task.heartbeat" and e["author"] == me and e["ts"] > st.claims[tids["b"]][0].ts
               for e in st.events)
    assert verdicts(tids)["a"] == ["accepted"]
    lease = [e for e in st.events if e["type"] == "integrator.lease"]
    assert len(lease) == 1                # once, when a patch waited: not after every session


def test_a_check_announced_holds_the_others_off_for_its_own_limit_not_half_an_hour():
    ev = {"timeout": 120}
    assert integrator.abandoned_after(ev) == 540 and integrator.abandoned_after({}) == integrator.CHECKING_SECONDS
    assert integrator.abandoned_after({"timeout": 5000}) == integrator.CHECKING_SECONDS


def test_a_commit_that_hasnt_arrived_waits_while_nothing_newer_came_from_its_author(project, monkeypatch):
    """Rejected after ten minutes whatever came, a slow network turned into rejections."""
    repo, tids, patch = project
    patch("a", "ok\n")
    c = as_(repo, "boss")
    p = next(iter(c.st.patches.values()))
    monkeypatch.setattr(integrator, "prepare_merge", lambda *a, **k: ("rejected", {"code": "missing", "reason": "x"}))
    real = time.time
    try:
        integrator.time.time = lambda: p.ts + integrator.MISSING_AFTER + 60   # nothing newer from ana: it waits
        assert integrator.plan(c, p) is None
        integrator.time.time = lambda: p.ts + integrator.MISSING_MAX + 60     # but not for ever
        assert integrator.plan(c, p)[0] == "rejected"
        integrator.time.time = lambda: p.ts + integrator.MISSING_AFTER + 60
        c.st.last_seen[p.author] = p.ts + integrator.MISSING_HEARD + 1        # ana's later events came: lost
        assert integrator.plan(c, p)[0] == "rejected"
    finally:
        integrator.time.time = real


def test_a_traced_check_lists_the_files_of_the_tree_it_touched(tmp_path):
    folder = tmp_path / "trace"
    folder.mkdir()
    (folder / "root").write_text("/w/review")
    (folder / "accept.trace").write_text("\n".join([
        '101 execve("/usr/bin/python3", ["python3", "check.py"], 0x7ffd /* 30 vars */) = 0',
        '101 openat(AT_FDCWD, "check.py", O_RDONLY|O_CLOEXEC) = 3</w/review/check.py>',
        '101 newfstatat(AT_FDCWD, "/w/review/src/mod.py", 0x7ffd, 0) = 0',
        '101 openat(AT_FDCWD, "/w/review/src/new.py", O_RDONLY) = -1 ENOENT (No such file or directory)',
        '101 openat(3</w/review/src>, "util.py", O_RDONLY) = 4</w/review/src/util.py>',
        '101 openat(AT_FDCWD, "/w/review/.git/HEAD", O_RDONLY) = 5',
        '101 openat(AT_FDCWD, "/usr/lib/python3.11/os.py", O_RDONLY) = 6',
        r'102 openat(AT_FDCWD, "/w/review/src/x\303\251.py", O_RDONLY <unfinished ...>',  # strace escapes UTF-8
        '102 <... openat resumed>) = 7',
        '+++ exited with 0 +++']), encoding="utf-8")
    assert integrator.traced_reads(folder) == {"check.py", "src/mod.py", "src/new.py", "src/util.py", "src/xé.py"}
    (folder / "metric.trace").write_text('103 openat(AT_FDCWD</w/review/data>, "x.csv", O_RDONLY) = 3\n')
    assert "data/x.csv" in integrator.traced_reads(folder)                      # newer strace says where it is
    (folder / "metric.trace").write_text('103 fchdir(3</tmp>) = 0\n103 openat(AT_FDCWD, "data.txt", O_RDONLY) = 3\n')
    assert integrator.traced_reads(folder) is None                              # after a chdir: can't tell
    (folder / "metric.trace").write_text('103 openat(AT_FDCWD, "/w/review/a-very-long-na"..., O_RDONLY) = 3\n')
    assert integrator.traced_reads(folder) is None                              # cut short


def test_blobs_gives_files_and_folders_and_none_for_what_isnt_there(tmp_path):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "i")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    got = integrator.blobs(repo, tree, ["src/a.py", "src", "src/b.py"])
    assert got["src/a.py"] == git(repo, "rev-parse", "HEAD:src/a.py") and got["src"] == git(repo, "rev-parse", "HEAD:src")
    assert got["src/b.py"] is None


def test_an_integrators_run_integrates_when_nobody_holds_the_lease(project, monkeypatch):
    """With an integrator, nothing went in unless someone kept integrate --loop running in a terminal."""
    from legion import cli
    repo, tids, patch = project
    patch("a", "ok\n")
    c = as_(repo, "boss")
    assert cli.finalize_here(c) == 1
    assert verdicts(tids)["a"] == ["accepted"] and Ctx(sync=False).st.leader(time.time()) is None
