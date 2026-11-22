import json
import subprocess
import sys
import time
import types

import pytest

from legion import attack as A
from legion import events as E
from legion import integrator
from legion import state as S
from legion.ctx import Fail
from legion.identity import Identity
from test_state import ANA, BEA, BOSS, Log

CEL = Identity.generate("cel")

SPEC = {"goal": "Settle the order of f(n).", "common": "The problem: ...\nKnown: ...",
        "lines": [{"key": "d1", "title": "Prove the core conjecture", "front": "lower bound", "task": "Prove it."},
                  {"key": "c1", "title": "Doubling constructions", "front": "construction", "task": "Build them."}],
        "check": "A record needs the strictest check.", "budget": {"line": 60}}


def research(zone=("research/",)):
    """A project with a research task that ANA, trusted (two accepted patches), works on."""
    log = Log()
    log.add(BOSS, "project.init", project="p", integrators=[BOSS.author])
    tid = S.task_id(log.add(BOSS, "task.created", key="f", title="F", zone=list(zone), accept="true"))
    for i in range(2):
        t = S.task_id(log.add(BOSS, "task.created", key=f"w-{i}", title="w", zone=[f"w{i}"], accept="true"))
        q = log.add(ANA, "patch.submitted", task=t, commit="c", branch=f"legion/w/{i}")
        log.add(BOSS, "patch.accepted", patch=q["id"], merge_commit=f"m-{i}")
    log.add(ANA, "task.claimed", task=tid, ttl=600)
    return log, tid


def launched(spec=SPEC):
    log, tid = research()
    st = log.state()
    rnd, pieces = A.plan(st, st.tasks[tid], json.loads(json.dumps(spec)), "ana")
    for p in pieces:
        log.add(ANA, "task.created", **p, split=True)
    return log, tid, rnd, pieces


def test_a_round_makes_a_line_and_its_check_per_direction_and_one_synthesis():
    log, tid, rnd, pieces = launched()
    st = log.state()
    assert not st.ignored
    kids = {t.key: t for t in st.tasks.values() if t.parent == tid}
    assert rnd == "r1" and list(kids) == ["f/r1-d1", "f/r1-d1-check", "f/r1-c1", "f/r1-c1-check", "f/r1-synthesis"]
    line, check, synth = kids["f/r1-d1"], kids["f/r1-d1-check"], kids["f/r1-synthesis"]
    assert line.zone == ["research/attacks/r1/d1/"] and check.zone == ["research/attacks/r1/d1-check/"]
    assert line.deps == [] and check.deps == [line.id] and check.apart == [line.id]
    assert synth.deps == [kids["f/r1-d1-check"].id, kids["f/r1-c1-check"].id]
    assert line.attack == {"round": "r1", "role": "line", "line": "d1"}
    assert line.accept == "python -m unittest discover -s legion_contracts/f/r1-d1 -p test_*.py"
    assert "test_report.py" in line.contract and "test_verdicts.py" in check.contract
    # Everyone shares the goal and the common text; the line's own task and budget go to its pieces.
    assert "Settle the order of f(n)." in line.body and "Known: ..." in synth.body
    assert "Prove it." in line.body and "about 60 steps" in line.body and "Prove it." in check.body
    assert "strictest check" in check.body and "send ana" in synth.body
    # The lines are open at once, the checks and the synthesis wait for what they read.
    assert [st.status(k.id, 2000) for k in (line, check, synth)] == ["open", "blocked", "blocked"]
    assert A.rounds(st, st.tasks[tid])["r1"][0].key == "f/r1-d1" and A.next_round(st, st.tasks[tid]) == "r2"


def test_only_a_trusted_agent_on_the_task_launches_a_round():
    log, tid = research()
    st = log.state()
    _, pieces = A.plan(st, st.tasks[tid], json.loads(json.dumps(SPEC)), "bea")
    log.add(BEA, "task.claimed", task=tid, ttl=600, join=True)
    log.add(BEA, "task.created", **pieces[0], split=True)  # works on it, but isn't trusted
    assert not any(t.parent == tid for t in log.state().tasks.values())


@pytest.mark.parametrize("change, why", [
    (lambda s: s.update(lines=[]), "1 to 12 lines"),
    (lambda s: s.update(lines=s["lines"] * 13), "1 to 12 lines"),
    (lambda s: s.update(lines=s["lines"] * 2), "two lines are called"),
    (lambda s: s["lines"][0].update(key="d1-check"), "invalid line key"),
    (lambda s: s["lines"][0].update(key="Synthesis"), "invalid line key"),
    (lambda s: s.pop("goal"), "say what the round is for"),
    (lambda s: s["lines"][1].pop("task"), "line c1 needs task"),
    (lambda s: s.update(folder="../out"), "folder is a path"),
    (lambda s: s.update(budget={"line": 5}), "budget"),
])
def test_a_spec_that_cant_make_a_round_says_why(change, why):
    spec = json.loads(json.dumps(SPEC))
    change(spec)
    assert why in A.check_spec(spec)


def test_a_round_stays_inside_the_task_and_doesnt_reuse_a_name():
    log, tid, rnd, _ = launched()
    st = log.state()
    with pytest.raises(Fail, match="already has a round r1"):
        A.plan(st, st.tasks[tid], dict(SPEC, round="r1"), "ana")
    with pytest.raises(Fail, match="stay inside its zone"):
        A.plan(st, st.tasks[tid], dict(SPEC, folder="elsewhere"), "ana")
    log2, tid2 = research(zone=("src/f.py",))
    with pytest.raises(Fail, match="no folder for the round"):
        A.plan(log2.state(), log2.state().tasks[tid2], SPEC, "ana")


def test_whoever_wrote_a_line_cant_take_its_check():
    log, tid, rnd, pieces = launched()
    st = log.state()
    line, check = st.find_task("f/r1-d1"), st.find_task("f/r1-d1-check")
    log.add(BEA, "task.claimed", task=line.id, ttl=600)
    log.add(BEA, "work.saved", task=line.id, commit="s" * 40, branch="legion/save/x", notes="half")
    q = log.add(CEL, "patch.submitted", task=line.id, commit="c" * 40, branch="legion/p/d1")
    log.add(BOSS, "patch.accepted", patch=q["id"], merge_commit="m" * 40)
    st = log.state()
    assert st.status(check.id, 2000) == "open"
    assert check.id in st.barred(BEA.author) and check.id in st.barred(CEL.author)  # saved work, or the patch
    assert check.id not in st.barred(ANA.author) and st.apart_error(check, ANA.author) is None
    assert "which you wrote" in st.apart_error(check, CEL.author)
    # A check sent anyway by the line's writer is rejected, by a rule any node can confirm from the log.
    p = log.add(CEL, "patch.submitted", task=check.id, commit="d" * 40, branch="legion/p/d1c")
    st = log.state()
    verdict = integrator.plan(types.SimpleNamespace(st=st), st.patches[p["id"]])
    assert verdict[0] == "rejected" and verdict[1]["code"] == "apart" and "apart" in S.PLAIN


def test_a_round_calls_every_owner_about_and_its_holes_stay_on_show():
    """The launch tells every owner active here (a message reaches each of their agents: idle ones under `legion
    run` start again with it, busy ones are told to start subagents), and `context` lists the pieces nobody
    holds, leaving out the check of a line the agent wrote."""
    log, tid = research()
    log.add(BEA, "task.claimed", task=S.task_id(log.events[1]), ttl=600, join=True)
    st = log.state()
    rnd, pieces = A.plan(st, st.tasks[tid], json.loads(json.dumps(SPEC)), "ana")

    def emit(type, **payload):
        log.add(ANA, type, **payload)
    c = types.SimpleNamespace(st=st, ident=types.SimpleNamespace(name="ana"), emit=emit)
    for p in pieces:
        log.add(ANA, "task.created", **p, split=True)
    assert A.announce(c, st.tasks[tid], rnd, pieces, 1000.0) == ["ana", "bea", "boss"]
    st = log.state()
    msg = st.messages[-1]
    assert msg["to"] == ["@ana", "@bea", "@boss"] and "(f/r1-d1, f/r1-c1)" in msg["text"] and "subagents" in msg["text"]
    assert st.reaches(msg, BEA.author, "bea#2", 1000.0)  # a subagent of bea's is called too
    assert [t.key for t in A.open_pieces(st, 1000.0)] == ["f/r1-c1", "f/r1-d1"]  # checks wait for their lines
    log.add(BEA, "task.claimed", task=st.find_task("f/r1-d1").id, ttl=600)
    call = A.call_text(log.state(), CEL.author, 1000.0)
    assert "1 piece(s)" in call and "f/r1-c1" in call and "f/r1-d1" not in call and "--number" in call
    assert A.call_text(log.state(), CEL.author, 99999.0).count("f/r1-") == 2  # bea's claim lapsed


def run_contract(tmp, piece):
    where = tmp / S.contract_dir(piece["key"])
    where.mkdir(parents=True, exist_ok=True)
    for name, text in piece["contract"].items():
        (where / name).write_text(text, encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", S.contract_dir(piece["key"]),
                           "-p", "test_*.py"], cwd=tmp, capture_output=True, text=True).returncode


def test_the_contracts_hold_each_piece_to_its_report(tmp_path):
    _, _, _, pieces = launched()
    line, check, synth = pieces[0], pieces[1], pieces[-1]
    report = tmp_path / "research/attacks/r1/d1/report.json"
    verdicts = tmp_path / "research/attacks/r1/d1-check/verdicts.json"
    assert run_contract(tmp_path, line) != 0  # no report yet
    claim = {"id": "D1-1", "statement": "s", "kind": "theorem", "status": "proved", "evidence": "e", "how_to_check": "h"}
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"summary": "x", "claims": [dict(claim, status="sure")], "dead_ends": "",
                                  "next_step": ""}), encoding="utf-8")
    assert run_contract(tmp_path, line) != 0  # a status nobody can read
    report.write_text(json.dumps({"summary": "x", "claims": [claim, dict(claim, id="D1-2")], "dead_ends": "",
                                  "next_step": "y"}), encoding="utf-8")
    assert run_contract(tmp_path, line) == 0
    verdicts.parent.mkdir(parents=True)
    one = {"claim_id": "D1-1", "verdict": "confirmed", "reason": "re-derived"}
    verdicts.write_text(json.dumps({"verdicts": [one], "overall": "o"}), encoding="utf-8")
    assert run_contract(tmp_path, check) != 0  # a claim left without a verdict
    verdicts.write_text(json.dumps({"verdicts": [one, dict(one, claim_id="D1-2", verdict="not verified")],
                                    "overall": "o"}), encoding="utf-8")
    assert run_contract(tmp_path, check) == 0
    out = tmp_path / "research/attacks/r1/synthesis/report.md"
    out.parent.mkdir(parents=True)
    out.write_text("Too short.", encoding="utf-8")
    assert run_contract(tmp_path, synth) != 0
    out.write_text("word " * 200, encoding="utf-8")
    assert run_contract(tmp_path, synth) == 0


def test_a_round_from_the_command_line_to_the_digest(tmp_path, monkeypatch, capsys):
    """The lead launches a round; another agent's line goes in through its contract; the line's writer can't take
    its check, someone else does, and the digest pairs the claim with its verdict."""
    from legion import coordgit
    from legion.cli import main
    from legion.ctx import Ctx
    from test_queue import as_, git, make_project
    repo, _, _ = make_project(tmp_path, monkeypatch)
    boss = make_project.people["boss"]
    tid = S.task_id(make_project.add(boss, "task.created", key="f", title="F", zone=["research/"],
                                     accept="python check.py a"))
    spec = tmp_path / "round.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    as_(repo, "boss")
    assert main(["attack", "launch", "f", str(spec)]) == 3  # the lead works on the task first
    main(["claim", tid])
    assert main(["attack", "launch", "f", str(spec), "--dry-run"]) == 0
    assert not Ctx(sync=False).st.find_task("f/r1-d1") and "YOUR LINE: Prove it." in capsys.readouterr().out
    assert main(["attack", "launch", "f", str(spec)]) == 0
    st = Ctx(sync=False).st
    assert [t.key for t in A.rounds(st, st.tasks[tid])["r1"]][-1] == "f/r1-synthesis"
    main(["attack", "status", "f"])
    assert "f/r1-d1-check" in capsys.readouterr().out
    people = {"ana": Identity.generate("ana#1"), "luis": make_project.people["luis"]}

    def send(who, key, files):
        t = Ctx(sync=False).st.find_task(key)
        git(repo, "checkout", "-q", "--detach", "main")
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", key)
        branch = coordgit.patch_branch(repo, t.id, people[who].short, 1)
        coordgit.publish_patch(repo, branch)
        ev = E.create(people[who], "patch.submitted", Ctx(sync=False).st.max_lamport + 1, task=t.id,
                      commit=git(repo, "rev-parse", "HEAD"), branch=branch)  # after what the commands recorded
        coordgit.append(repo, people[who], ev)
        git(repo, "checkout", "-q", "main")
        integrator.integrate_once(Ctx(), 60, log=lambda *_: None)
        return Ctx(sync=False).st.patches_for(t.id)[-1]

    claim = {"id": "D1-1", "statement": "s", "kind": "theorem", "status": "proved", "evidence": "e", "how_to_check": "h"}
    report = {"summary": "found s", "claims": [claim], "dead_ends": "none", "next_step": "go on"}
    p = send("ana", "f/r1-d1", {"research/attacks/r1/d1/report.json": json.dumps(report)})
    assert p.verdict == "accepted"
    st = Ctx(sync=False).st
    check = st.find_task("f/r1-d1-check")
    assert st.status(check.id, time.time()) == "open" and check.id in st.barred(people["ana"].author)
    verdicts = {"verdicts": [{"claim_id": "D1-1", "verdict": "confirmed", "reason": "redone"}], "overall": "stands"}
    p = send("ana", "f/r1-d1-check", {"research/attacks/r1/d1-check/verdicts.json": json.dumps(verdicts)})
    assert (p.verdict, p.code) == ("rejected", "apart")
    assert send("luis", "f/r1-d1-check", {"research/attacks/r1/d1-check/verdicts.json":
                                           json.dumps(verdicts)}).verdict == "accepted"
    capsys.readouterr()
    as_(repo, "boss")
    main(["attack", "digest", "f", "--no-sync"])
    shown = capsys.readouterr().out
    assert "D1-1 [proved; confirmed] s" in shown and "checker: redone" in shown and "line c1" in shown


def test_the_digest_pairs_every_claim_with_its_verdict(monkeypatch):
    log, tid, rnd, pieces = launched()
    files = {"research/attacks/r1/d1/report.json": json.dumps(
                 {"summary": "found", "dead_ends": "none", "next_step": "go on",
                  "claims": [{"id": "D1-1", "statement": "s1", "kind": "theorem", "status": "proved"},
                             {"id": "D1-2", "statement": "s2", "kind": "computation", "status": "partial"}]}),
             "research/attacks/r1/d1-check/verdicts.json": json.dumps(
                 {"overall": "D1-1 stands", "verdicts": [{"claim_id": "D1-1", "verdict": "confirmed",
                                                          "reason": "redone"}]})}
    monkeypatch.setattr(A, "_read", lambda repo, ref, path: files.get(path))
    c = types.SimpleNamespace(st=log.state(), repo=None)
    entries = A.digest(c, c.st.tasks[tid], "r1", "main")
    d1, c1 = entries
    assert [(x["id"], x["verdict"]) for x in d1["claims"]] == [("D1-1", "confirmed"), ("D1-2", "not verified")]
    assert d1["check_overall"] == "D1-1 stands" and "summary" not in c1
    text = A.digest_text(entries)
    assert "D1-1 [proved; confirmed] s1" in text and "its report isn't in main yet" in text
