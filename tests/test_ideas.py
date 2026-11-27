"""Getting a goal out of its rut: barriers every line says it escapes, rounds of ideas from fields handed out (blind
to the map, judged against it), deep lines worked on over several sessions, and the exemplar move."""
import json
import subprocess
import sys
import types

from legion import attack as A
from legion import research as R
from legion import state as S
from legion.identity import Identity
from test_research import CEL, goal, propose, round_of, why
from test_state import ANA, BEA, BOSS

ESC = ("moments: it builds an explicit object instead of bounding second moments, so neither moment barrier "
       "applies to it.")


def finding(log, tid, line, text="Every second-moment bound loses a factor n.", who=CEL):
    return log.add(who, "finding.stated", task=tid, line=line, cid=text[:20], statement=text, status="proved",
                   kind="barrier")


def test_a_goal_names_its_barriers_and_every_line_then_says_why_it_escapes_them():
    log, tid = goal()
    other = S.task_id(log.events[2])  # w-0: not a goal
    log.add(ANA, "goal.barrier", task=other, barrier="moments", statement="Moment methods lose a factor of n here.")
    assert "research goal" in why(log)
    log.add(BEA, "goal.barrier", task=tid, barrier="moments", statement="Moment methods lose a factor of n here.")
    assert "trusted agent that works on the goal" in why(log)
    log.add(ANA, "goal.barrier", task=tid, barrier="Not A Key", statement="Moment methods lose a factor of n here.")
    assert "short key" in why(log)
    log.add(ANA, "goal.barrier", task=tid, barrier="moments", statement="too short")
    assert "precisely" in why(log)
    log.add(ANA, "goal.barrier", task=tid, barrier="moments", statement="Moment methods lose a factor of n.",
            **{"from": ["nope"]})
    assert "rests on findings" in why(log)
    line = round_of(log, tid, "moments", 1)
    f = finding(log, tid, line)
    log.add(ANA, "goal.barrier", task=tid, barrier="moments", statement="First and second moment bounds lose a "
            "factor of n against the truth: the variance is dominated by near-disjoint pairs.", **{"from": [f["id"]]})
    st = log.state()
    assert [b["barrier"] for b in st.barriers_for(tid)] == ["moments"]
    # From now on a line says which barrier it could fall into, and why it doesn't.
    propose(log, BEA, tid, "explicit", "An explicit construction", "Take the cubic residues mod p and check S4.")
    assert "escapes" in why(log) and "moments" in why(log)
    propose(log, BEA, tid, "explicit", "An explicit construction", "Take the cubic residues mod p and check S4.",
            escapes="It builds an explicit object, so no barrier of the kind listed applies to it at all.")
    assert "BY NAME" in why(log)  # long enough, but answering no barrier by its name
    ok = propose(log, BEA, tid, "explicit", "An explicit construction", "Take the cubic residues mod p and check S4.",
                 escapes=ESC)
    st = log.state()
    assert ok["id"] in st.lines
    t = st.tasks[tid]
    assert "BARRIERS" in R.map_text(st, t) and "near-disjoint pairs" in R.map_text(st, t)
    assert "--escapes" in R.propose_text(st, t, "bea", 0) and "near-disjoint" in R.propose_text(st, t, "bea", 0)
    spec = R.spec_for(st, t, [st.lines[ok["id"]]])
    assert "WHY IT ESCAPES the goal's barriers: moments: it builds an explicit object" in spec["lines"][0]["task"]
    assert "barriers.json" in spec["synthesis"]
    assert R.map_data(st, t, 1000.0)["barriers"][0]["name"] == "moments"
    # One that turns out not to bar what it said is retired; a restatement keeps its name.
    log.add(ANA, "goal.barrier", task=tid, barrier="gone", retired=True)
    assert "no barrier gone" in why(log)
    log.add(ANA, "goal.barrier", task=tid, barrier="moments", retired=True)
    assert log.state().barriers_for(tid) == []
    for i in range(S.MAX_BARRIERS + 1):
        log.add(BOSS, "goal.barrier", task=tid, barrier=f"b-{i}", statement=f"Family number {i} fails for a reason.")
    assert f"{S.MAX_BARRIERS} barriers already" in why(log)


def test_deep_lines_dont_hold_their_round_back_nor_go_stale_and_there_are_few_of_them():
    log, tid = goal()
    deep = propose(log, ANA, tid, "codes", "Concatenated codes as tournaments", "Read the tournament off a code.",
                   deep=True)
    propose(log, BEA, tid, "paley", "Cubic residues", "Take the cubic residues mod p.", deep=True)
    propose(log, CEL, tid, "nibble", "A nibble", "Grow the tournament by a Rodl nibble.", deep=True)
    assert f"at most {S.MAX_DEEP} deep lines" in why(log)
    plain = propose(log, CEL, tid, "nibble", "A nibble", "Grow the tournament by a Rodl nibble.")
    st = log.state()
    rnd, pieces = A.plan(st, st.tasks[tid], R.spec_for(st, st.tasks[tid], [st.lines[deep["id"]],
                                                                           st.lines[plain["id"]]]), "ana")
    by = {p["key"]: p for p in pieces}
    line = by[f"f/r1-{R.line_key(st.lines[deep['id']])}"]
    assert line["attack"]["deep"] is True and "THIS IS A DEEP LINE" in line["body"] and "KILL TEST" in line["body"]
    assert by[line["key"] + "-check"]["attack"]["deep"] is True
    synth = by["f/r1-synthesis"]
    assert synth["deps"] == [f"f/r1-{R.line_key(st.lines[plain['id']])}-check"]  # not the deep line's check
    made = {}
    for p in pieces:
        made[p["key"]] = S.task_id(log.add(ANA, "task.created", **p, split=True))
    assert all(log.state().find_task(k) for k in made)
    # The round closes without its deep line.
    for k in (f"f/r1-{R.line_key(st.lines[plain['id']])}", f"f/r1-{R.line_key(st.lines[plain['id']])}-check",
              "f/r1-synthesis"):
        q = log.add(CEL if "check" not in k else BEA, "patch.submitted", task=made[k], commit="ab" * 20,
                    branch=f"legion/x/{k[-6:]}")
        log.add(BOSS, "patch.accepted", patch=q["id"], merge_commit=f"m-{k}")
    st = log.state()
    assert st.research_rounds(tid)[0]["closed"]
    # Let go of session after session, it is never stale for the agent on it.
    dl = made[line["key"]]
    for i in range(3):
        log.add(CEL, "task.claimed", task=dl, ttl=600, ts=2000.0 + 100 * i)
        log.add(CEL, "task.released", task=dl, notes="saved, next session", ts=2050.0 + 100 * i)
    assert dl not in log.state().stale(CEL.author)


def test_a_dry_goal_gets_a_round_of_ideas_blind_to_its_map_each_from_its_own_field():
    log, tid = goal()
    line = round_of(log, tid, "moments", 1)
    assert not log.state().ideas_due(tid)  # one dry round: not yet
    round_of(log, tid, "local-lp", 2)
    f = finding(log, tid, line)
    log.add(ANA, "goal.barrier", task=tid, barrier="moments", statement="Second-moment bounds lose a factor n.",
            **{"from": [f["id"]]})
    st = log.state()
    assert st.dry(tid) == S.DRY_REFRAME and st.ideas_due(tid)
    spec = R.idea_spec(st, st.tasks[tid], 1000.0)
    fields = [x["field"] for x in spec["lines"]]
    assert len(fields) == len(set(fields)) >= 3 and set(fields) <= set(R.FIELDS)
    assert "Second-moment bounds lose a factor n" in spec["common"] and "THE MAP OF" not in spec["common"]
    assert "Any bound on f(n) nobody has counts" in spec["goal"]
    emitted = []
    c = types.SimpleNamespace(st=st, ident=types.SimpleNamespace(name="ana"), me=ANA.author, sync=lambda: None)

    def emit(type, **p):
        emitted.append(log.add(ANA, type, **p))
        c.st = log.state()
    c.emit = emit
    assert R.launch_due(c, 1000.0)
    st = log.state()
    assert not [w for _, w in st.ignored if "task" in w]
    kids = [t for t in st.tasks.values() if t.parent == tid and t.attack.get("round") == "r3"]
    ideas = [t for t in kids if t.attack["role"] == "line"]
    assert len(ideas) == len(fields) and all(t.attack["kind"] == "idea" and t.attack["field"] for t in ideas)
    one = ideas[0]
    assert "Don't read the map" in one.body and one.attack["field"] in one.body
    assert "kill_test" in one.body and "Second-moment bounds lose a factor n" in one.body
    assert "THE MAP OF" not in one.body and "test_idea.py" in one.contract
    judge = next(t for t in kids if t.attack["role"] == "check" and t.attack["line"] == one.attack["line"])
    assert "NOW read the map" in judge.body and judge.apart == [one.id] and "test_judgement.py" in judge.contract
    synth = next(t for t in kids if t.attack["role"] == "synthesis")
    assert synth.attack.get("kind") == "idea" and not synth.attack.get("assess") and "DEEP LINES" in synth.body
    assert any(m["type"] == "message.sent" for m in emitted)  # announced, as any round
    st = log.state()
    rs = st.research_rounds(tid)
    assert rs[-1]["ideas"] and not st.ideas_due(tid)  # one at a time
    # A round of ideas neither moves its goal nor counts against it, and it lifts the goal out of being spent.
    for t in kids:
        q = log.add(BEA if t.attack["role"] == "check" else CEL, "patch.submitted", task=t.id, commit="cd" * 20,
                    branch=f"legion/i/{t.key[-8:]}")
        log.add(BOSS, "patch.accepted", patch=q["id"], merge_commit=f"mi-{t.key}")
    st = log.state()
    assert st.research_rounds(tid)[-1]["closed"] and st.dry(tid) == S.DRY_REFRAME
    assert not st.ideas_due(tid)  # no round of lines closed since


def test_ideas_lift_a_spent_goal_and_a_dry_streak_has_only_so_many():
    log, tid = goal()
    for n in range(1, 1 + S.DRY_SPENT):
        round_of(log, tid, f"front-{n}", n)
    st = log.state()
    assert st.spent(tid) and st.ideas_due(tid)
    made = 0
    for k in range(S.MAX_IDEA_ROUNDS + 1):
        st = log.state()
        if not st.ideas_due(tid):
            break
        rnd, pieces = A.plan(st, st.tasks[tid], R.idea_spec(st, st.tasks[tid], 1000.0), "ana")
        for i, p in enumerate(pieces):
            ev = log.add(ANA, "task.created", **p, split=True)
            q = log.add(BEA if p["attack"]["role"] == "check" else CEL, "patch.submitted", task=S.task_id(ev),
                        commit=f"{k}{i}" * 20, branch=f"legion/i/{k}-{i}")
            log.add(BOSS, "patch.accepted", patch=q["id"], merge_commit=f"mi{k}{i}")
        made += 1
        assert not log.state().spent(tid)  # its lines may run
        round_of(log, tid, f"after-{k}", 10 + k)
    assert made == S.MAX_IDEA_ROUNDS and not log.state().ideas_due(tid) and log.state().spent(tid)
    # The fields go round: the next idea round hands out those used least.
    st = log.state()
    used = [t.attack["field"] for t in st.tasks.values() if t.attack.get("kind") == "idea" and t.attack["role"] == "line"]
    assert len(set(used)) == len(used) or len(used) > len(R.FIELDS)


def test_a_synthesis_names_barriers_and_proposes_deep_lines(tmp_path):
    log, tid = goal()
    line = round_of(log, tid, "moments", 1)
    f = finding(log, tid, line)
    st = log.state()
    d = tmp_path / "research" / "attacks" / "r9" / "synthesis"
    d.mkdir(parents=True)
    (d / R.BARRIERS).write_text(json.dumps([
        {"name": "Second Moments!", "statement": "Second-moment bounds lose a factor n.", "from": [f["id"], "zz"]},
        {"name": "x", "statement": "short"}]), encoding="utf-8")
    (d / R.NEXT_LINES).write_text(json.dumps([
        {"front": "codes", "method": "transfer", "move": "transfer", "title": "Concatenated codes",
         "text": "Kill test first: check the inner code at n = 3. Then build the outer code.", "learn": "codes fail",
         "escapes": ESC, "deep": True}]), encoding="utf-8")
    from legion.ctx import Fail

    def emit(type, **p):
        n = len(log.state().ignored)
        ev = log.add(CEL, type, **p)
        c.st = log.state()
        if len(c.st.ignored) > n:
            raise Fail(c.st.ignored[-1][1])
        return ev
    c = types.SimpleNamespace(st=st, repo=tmp_path, ident=types.SimpleNamespace(name="cel"), me=CEL.author,
                              emit=emit)
    log.add(BOSS, "patch.submitted", task=line, commit="ef" * 20, branch="legion/x/y")  # CEL took part already
    assert R.take_barriers(c, st.tasks[tid], str(d.relative_to(tmp_path)).replace("\\", "/")) == 1
    bars = log.state().barriers_for(tid)
    assert [b["barrier"] for b in bars] == ["second-moments"] and bars[0]["from"] == [f["id"]]
    assert R.propose_next(c, st.tasks[tid], str(d.relative_to(tmp_path)).replace("\\", "/")) == 1
    x = log.state().open_lines(tid)[-1]["ev"]
    assert x["deep"] is True and x["escapes"] == ESC


def test_the_idea_and_its_judgement_are_held_to_their_form(tmp_path):
    key = "f/r3-idea-entropy"
    for name, code, good, bad in (
            ("test_idea.py", A.idea_contract(key, "out/idea.json"),
             {k: f"enough words about the {k} of it" for k in A.IDEA_KEYS},
             {k: "x" for k in A.IDEA_KEYS}),
            ("test_judgement.py", A.judgement_contract(key + "-check", "out/judgement.json"),
             {"novelty": 2, "plausibility": 1, "escapes_hold": True, "kill_test": "survived", "reason": "checked"},
             {"novelty": 5, "plausibility": 1, "escapes_hold": True, "kill_test": "maybe", "reason": "checked"})):
        k = key if name == "test_idea.py" else key + "-check"
        cdir = tmp_path / S.contract_dir(k)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / name).write_text(code, encoding="utf-8")
        (tmp_path / "out").mkdir(exist_ok=True)
        target = tmp_path / "out" / ("idea.json" if name == "test_idea.py" else "judgement.json")
        for data, ok in ((good, True), (bad, False)):
            target.write_text(json.dumps(data), encoding="utf-8")
            p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(cdir), "-p", "test_*.py"],
                               cwd=tmp_path, capture_output=True, text=True)
            assert (p.returncode == 0) == ok, p.stderr[-800:]


def test_the_digest_of_a_round_of_ideas_shows_each_idea_and_its_judgement():
    text = A.digest_text([{"line": "idea-entropy", "title": "r3 idea", "status": "done", "check_status": "done",
                           "idea": {k: f"the {k}" for k in A.IDEA_KEYS},
                           "judgement": {"novelty": 3, "plausibility": 2, "escapes_hold": True, "kill_test": "survived",
                                         "reason": "the kill test ran clean"}}])
    assert "mechanism: the mechanism" in text and "novelty 3/3, plausibility 2/3" in text and "survived" in text


def test_the_exemplar_move_is_handed_out_and_says_how():
    assert "exemplar" in S.MOVES and "exemplar" in S.REFRAME and set(R.MOVE_TEXT) == set(S.MOVES)
    assert "legion sat" in R.MOVE_TEXT["exemplar"] and "computational exemplars" in R.FIELDS


def test_an_idea_answers_each_barrier_by_name_at_launch_and_at_submit(tmp_path):
    key = "f/r3-idea-entropy"
    code = A.idea_contract(key, "out/idea.json", ["moments", "local-counts"])
    cdir = tmp_path / S.contract_dir(key)
    cdir.mkdir(parents=True)
    (cdir / "test_idea.py").write_text(code, encoding="utf-8")
    (tmp_path / "out").mkdir()
    idea = {k: f"enough words about the {k} of it" for k in A.IDEA_KEYS}
    for escapes, ok in (("It avoids moments since it counts nothing.", False),
                        ("moments: it counts nothing. Local counts: it reads the whole tournament.", True)):
        (tmp_path / "out" / "idea.json").write_text(json.dumps(dict(idea, escapes=escapes)), encoding="utf-8")
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(cdir), "-p", "test_*.py"],
                           cwd=tmp_path, capture_output=True, text=True)
        assert (p.returncode == 0) == ok, p.stderr[-600:]
    # Barriers named after the launch: submit asks for them, live.
    log, tid = goal()
    for n in (1, 2):
        round_of(log, tid, f"front-{n}", n)
    log.add(ANA, "goal.barrier", task=tid, barrier="moments", statement="Second-moment bounds lose a factor n.")
    st = log.state()
    spec = R.idea_spec(st, st.tasks[tid], 1000.0)
    assert spec["barriers"] == ["moments"]
    _, pieces = A.plan(st, st.tasks[tid], spec, "ana")
    assert "'moments'" in pieces[0]["contract"]["test_idea.py"]
    for p in pieces:
        log.add(ANA, "task.created", **p, split=True)
    log.add(ANA, "goal.barrier", task=tid, barrier="flag-topology", statement="Flag complexes alone force nothing.")
    st = log.state()
    t = st.find_task(pieces[0]["key"])
    d = tmp_path / t.zone[0]
    d.mkdir(parents=True)
    (d / A.IDEA_FILE).write_text(json.dumps(dict(idea, escapes="moments: it counts nothing at all here.")),
                                 encoding="utf-8")
    c = types.SimpleNamespace(st=st, repo=tmp_path)
    assert R.unanswered(c, t) == ["flag-topology"]
    (d / A.IDEA_FILE).write_text(json.dumps(dict(idea, escapes="moments: nothing counted. Flag topology: unused.")),
                                 encoding="utf-8")
    assert R.unanswered(c, t) == [] and R.unanswered(c, st.tasks[tid]) == []


def test_a_record_missing_accepts_doesnt_hand_out_or_finalize_what_main_has(tmp_path, monkeypatch):
    from legion import coordgit, integrator
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    g("init", "-q")
    g("config", "user.email", "x@y")
    g("config", "user.name", "x")
    g("commit", "-q", "--allow-empty", "-m", "start")
    tip = g("rev-parse", "HEAD")
    g("commit", "-q", "--allow-empty", "-m", "work on the check")
    g("commit", "-q", "--allow-empty", "-m", "legion: f/r1-a-check (bea, patch 1234abcd)")
    g("update-ref", "refs/legion/peers/aaaaaaaaaaaaaaaa-node1/main", "HEAD")
    g("checkout", "-q", "-b", "fork", tip)
    g("commit", "-q", "--allow-empty", "-m", "legion: f/r1-b (eve, patch 99999999)")
    g("update-ref", "refs/legion/peers/bbbbbbbbbbbbbbbb-node2/main", "HEAD")
    # Only trusted nodes' mains count, and only those ahead of this record's tip.
    assert coordgit.accepted_ahead(repo, tip, {"aaaaaaaaaaaaaaaa"}) == {"f/r1-a-check"}
    assert coordgit.accepted_ahead(repo, tip, {"cccccccccccccccc"}) == set()
    assert coordgit.accepted_ahead(repo, g("rev-parse", "refs/legion/peers/aaaaaaaaaaaaaaaa-node1/main"),
                                   {"aaaaaaaaaaaaaaaa"}) == set()
    assert coordgit.accepted_ahead(repo, None, {"aaaaaaaaaaaaaaaa"}) == set()
    # A finalizer whose record is behind finalizes nothing: every verdict it made would be refused.
    said = []
    c = types.SimpleNamespace(st=types.SimpleNamespace(policy={"mode": "open"}, trusted=lambda a: True),
                              me="ed25519:aa", reload=lambda: None, ahead=lambda: {"f/r1-a-check"})
    assert integrator.finalize(c, log=said.append) == 0 and "not finalizing" in said[0]


def test_claim_hands_out_a_round_of_ideas_pieces_before_other_open_work():
    from legion import cli
    log, tid = goal()
    for n in (1, 2):
        round_of(log, tid, f"front-{n}", n)
    p = propose(log, BEA, tid, "census", "Topic 9", "lambda mu nu xi omicron")
    st = log.state()
    _, pieces = A.plan(st, st.tasks[tid], R.spec_for(st, st.tasks[tid], [st.lines[p["id"]]]), "ana")
    _, ideas = A.plan(st, st.tasks[tid], dict(R.idea_spec(st, st.tasks[tid], 1000.0), round="r9"), "ana")
    for x in pieces + ideas:
        log.add(ANA, "task.created", **x, split=True)
    st = log.state()
    free = [t for t in st.claimable(1000.0) if t.parent == tid]
    assert {t.attack.get("kind") for t in free} == {None, "idea"}
    for who in (ANA, BEA, CEL):
        first = min(free, key=lambda t: cli.claim_rank(st, t, who.author, 1000.0))
        assert first.attack.get("kind") == "idea"
