from legion import events as E
from legion import state as S
from legion.identity import Identity

A, B, C, D = (Identity.generate(f"integrator#{i}") for i in range(1, 5))
AGENT = Identity.generate("ana#1")


class Log:
    """Events created once, with an increasing Lamport clock and a controllable ts."""

    def __init__(self):
        self.events = []

    def add(self, who, type, ts=1000.0, **payload):
        ev = E.create(who, type, len(self.events) + 1, **payload)
        ev = dict(ev, ts=ts)  # pin ts, then sign again
        body = E.canonical(E._body(ev))
        import hashlib
        ev["id"] = hashlib.sha256(body).hexdigest()[:16]
        ev["sig"] = who.sign(body)
        self.events.append(ev)
        return ev

    def state(self):
        return S.build(self.events)


def project():
    log = Log()
    log.add(A, "project.init", project="p", integrators=[A.author])
    log.add(A, "main.updated", commit="m0")
    return log


def test_one_integrator_adds_another_alone_then_majority_is_needed():
    log = project()
    log.add(A, "integrator.vote", action="add", key=B.author, member="integrator#2")
    assert log.state().integrators == {A.author, B.author}  # 1 of 1 is a majority
    log.add(A, "integrator.vote", action="add", key=C.author)
    assert C.author not in log.state().integrators          # 1 of 2 is not
    log.add(B, "integrator.vote", action="add", key=C.author)
    assert C.author in log.state().integrators              # 2 of 2


def test_agents_cannot_vote_and_the_last_integrator_stays():
    log = project()
    log.add(AGENT, "integrator.vote", action="add", key=AGENT.author)
    log.add(A, "integrator.vote", action="remove", key=A.author)
    st = log.state()
    assert st.integrators == {A.author}
    assert {why for _, why in st.ignored} == {"only an integrator can publish this event",
                                              "can't remove the last integrator"}


def test_removed_integrator_loses_its_powers():
    log = project()
    log.add(A, "integrator.vote", action="add", key=B.author)
    log.add(A, "integrator.vote", action="remove", key=B.author)
    log.add(B, "integrator.vote", action="remove", key=B.author)  # B votes for its own removal: 2 of 2
    log.add(B, "task.created", key="t", title="t", zone=["x"], accept="true")
    st = log.state()
    assert st.integrators == {A.author} and not st.tasks


def test_lease_expires_and_a_backup_takes_over():
    log = project()
    log.add(A, "integrator.vote", action="add", key=B.author)
    log.add(A, "integrator.lease", ts=1000.0, ttl=30)
    log.add(B, "integrator.lease", ts=1001.0, ttl=30)
    st = log.state()
    assert st.leader(1010).author == A.author  # the earlier lease wins while it lives
    assert st.leader(1031).author == B.author  # A went quiet: B's lease is the one left
    log.add(A, "integrator.renew", ts=1040.0)  # too late: an expired lease isn't revived
    assert log.state().leader(1041) is None  # both expired: nobody integrates until someone takes it
    assert log.state().ignored[-1][1] == "the lease had already expired"


def test_clean_release_hands_over_at_once():
    log = project()
    log.add(A, "integrator.vote", action="add", key=B.author)
    log.add(A, "integrator.lease", ts=1000.0, ttl=60)
    log.add(B, "integrator.lease", ts=1001.0, ttl=60)
    log.add(A, "integrator.release", ts=1002.0)
    assert log.state().leader(1003).author == B.author


def test_two_integrators_accepting_at_once_cannot_fork_main():
    log = project()
    log.add(A, "integrator.vote", action="add", key=B.author)
    t1 = S.task_id(log.add(A, "task.created", key="a", title="a", zone=["a"], accept="true"))
    t2 = S.task_id(log.add(A, "task.created", key="b", title="b", zone=["b"], accept="true"))
    p1 = log.add(AGENT, "patch.submitted", task=t1, commit="c1", branch="legion/a/1")["id"]
    p2 = log.add(AGENT, "patch.submitted", task=t2, commit="c2", branch="legion/b/1")["id"]
    # Both integrators merged onto m0 without seeing each other.
    log.add(A, "patch.accepted", patch=p1, merge_commit="m1", base="m0")
    log.add(B, "patch.accepted", patch=p2, merge_commit="m2", base="m0")
    st = log.state()
    assert st.main_tip == "m1"                       # the first one wins
    assert st.patches[p2].verdict is None            # the second stays pending, to be redone on m1
    assert st.ignored[-1][1] == "stale base: main moved on since this was computed"
    log.add(B, "patch.accepted", patch=p2, merge_commit="m3", base="m1")
    assert log.state().main_tip == "m3"


def test_payload_cannot_overwrite_the_header():
    import pytest
    with pytest.raises(ValueError):
        E.create(A, "integrator.vote", 1, action="add", key=B.author, name="someone else")


def test_scratch_copies_are_removed_without_following_links(tmp_path):
    import os
    import subprocess
    from legion.integrator import remove_tree
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "precious.txt").write_text("keep me")
    scratch = tmp_path / "scratch"
    (scratch / ".lake").mkdir(parents=True)
    link = scratch / ".lake" / "packages"
    if os.name == "nt":  # a junction, as check.py makes
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(shared)], check=True, capture_output=True)
    else:
        link.symlink_to(shared, target_is_directory=True)
    assert (link / "precious.txt").exists()
    remove_tree(scratch)
    assert not scratch.exists()
    assert (shared / "precious.txt").read_text() == "keep me"


def test_a_long_check_keeps_the_lease_beating(monkeypatch):
    from legion import integrator
    beats = []
    monkeypatch.setattr(integrator, "BEAT_EVERY", 0.2)
    monkeypatch.setattr(integrator, "beat", lambda: beats.append(1))
    code, out = integrator.run_cmd('python -c "import time; time.sleep(1); print(42)"', ".", 30)
    assert (code, out.strip()) == (0, "42") and len(beats) >= 3
    code, out = integrator.run_cmd('python -c "import time; time.sleep(5)"', ".", 1)
    assert code is None and "timed out" in out
