from legion import events as E
from legion import state as S
from legion import trust
from legion.identity import Identity

BOSS = Identity.generate("boss")
ANA1 = Identity.generate("ana#1")
ANA2 = Identity.generate("ana#2")
LUIS = Identity.generate("luis#1")


class Log:
    """Events created once, with an increasing Lamport clock."""

    def __init__(self):
        self.events = []

    def add(self, who, type, **payload):
        ev = E.create(who, type, len(self.events) + 1, **payload)
        self.events.append(ev)
        return ev

    def state(self):
        return S.build(self.events)


def project(n=3):
    log = Log()
    log.add(BOSS, "project.init", project="p", integrators=[BOSS.author])
    tids = [S.task_id(log.add(BOSS, "task.created", key=f"t{i}", title="t", zone=[f"t{i}.py"], accept="true"))
            for i in range(n)]
    return log, tids


def submit(log, who, tid):
    return log.add(who, "patch.submitted", task=tid, commit="c", branch=f"legion/{tid}/{who.short}")["id"]


def test_new_agents_start_at_fifty():
    assert trust.Trust().score == 50 and trust.Trust().as_dict()["level"] == "new"


def test_failing_tests_weigh_more_than_conflicts():
    log, (a, b, _) = project()
    pa, pb = submit(log, ANA1, a), submit(log, LUIS, b)
    log.add(BOSS, "patch.rejected", patch=pa, reason="x", code="tests")
    log.add(BOSS, "patch.rejected", patch=pb, reason="x", code="conflict")
    agents, _ = trust.compute(log.state(), 0)
    assert agents[ANA1.author].score < agents[LUIS.author].score < 50


def test_people_add_up_their_agents_and_integrator_is_not_scored():
    log, (a, b, c) = project()
    for who, tid in ((ANA1, a), (ANA2, b), (LUIS, c)):
        log.add(BOSS, "patch.accepted", patch=submit(log, who, tid))
    _, people = trust.compute(log.state(), 0)
    assert set(people) == {"ana", "luis"}
    assert people["ana"].counts == {"accepted": 2} and people["ana"].score > people["luis"].score


def test_abandoned_claims_count_against():
    log, (a, _, _) = project()
    log.add(ANA1, "task.claimed", task=a, ttl=60)
    agents, _ = trust.compute(log.state(), now=10 ** 12)  # long after the claim expired
    assert agents[ANA1.author].counts == {"abandoned": 1}


def test_legacy_reasons_are_classified():
    assert trust.legacy_code("toca archivos fuera de su zona (x)") == "zone"
    assert trust.legacy_code("the acceptance command fails") == "tests"
    assert trust.legacy_code("does not improve the metric: 2 vs 1") == "no_gain"
