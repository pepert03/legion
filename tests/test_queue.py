"""The integrator's merge queue and its persistent review folders, on a real repository."""

import subprocess

import pytest

from legion import coordgit, identity, integrator
from legion import events as E
from legion import state as S
from legion.ctx import Ctx
from legion.identity import Identity

CHECK = '''import pathlib, sys
here = pathlib.Path(__file__).parent
build = here / "build"  # git-ignored: what a check leaves for the next one
build.mkdir(exist_ok=True)
runs = build / "runs"
runs.write_text(str(int(runs.read_text()) + 1) if runs.exists() else "1")
sys.exit(0 if (here / (sys.argv[1] + ".txt")).read_text().strip() == "ok" else 1)
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
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
    identity.bind(repo, boss)
    lamport = iter(range(1, 100))

    def add(who, type, **payload):
        ev = E.create(who, type, next(lamport), **payload)
        coordgit.append(repo, who, ev)
        return ev

    add(boss, "project.init", project="p", integrators=[boss.author], runners=["python"])
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
