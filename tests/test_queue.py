"""The integrator's merge queue and its persistent review folders, on a real repository."""

import subprocess
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
    assert integrator.hold_lease(boss, 300)[0]
    luis = as_(repo, "luis")
    assert not integrator.hold_lease(luis, 300)[0]
    taken = integrator.hold_shards(luis, 300, 1)
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
    pa = st.review_patch(tids["a"])
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
    main(["release", tid])
    assert "nobody else can see" in capsys.readouterr().out  # the second commit wasn't saved
    git(repo, "checkout", "-q", "main")
    as_(repo, "marta")
    main(["claim", tid])
    shown = capsys.readouterr().out
    save = Ctx(sync=False).st.saves(tid)[0]
    assert "Work in progress on this task is saved" in shown and save["id"][:8] in shown and "stage 2 left" in shown
    main(["start", tid, "--from", save["id"][:8]])
    assert (repo / "a.txt").read_text() == "stage 1\n" and "luis" in capsys.readouterr().out


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
