import json
import time
import types

from legion import attack as A
from legion import integrator
from legion import research as R
from legion import state as S
from legion.identity import Identity
from test_attack import research
from test_state import ANA, BEA, BOSS, Log

CEL = Identity.generate("cel")
LINE = dict(method="compute", move="specialise", learn="which small cases are out of reach of a SAT solver")


def goal():
    """A project whose task f is a research goal, with ANA trusted and on it."""
    log, tid = research()
    log.add(BOSS, "goal.opened", task=tid, text="Any bound on f(n) nobody has counts.")
    return log, tid


def propose(log, who, tid, front, title, text=None, ts=1000.0, **more):
    return log.add(who, "line.proposed", ts=ts, task=tid, front=front, title=title,
                   text=text or f"Do exactly this about {title}.", **{**LINE, **more})


def why(log):
    return log.state().ignored[-1][1]


def test_a_research_goal_isnt_handed_out_whole_and_only_those_on_it_open_one():
    log, tid = research()
    log.add(BEA, "goal.opened", task=tid)  # not trusted, not on the task
    assert tid not in log.state().goals and "founder" in why(log)
    log.add(ANA, "goal.opened", task=tid, text="what counts")  # trusted, and it claimed the task
    st = log.state()
    assert st.goals[tid]["text"] == "what counts" and st.goal_of(tid) == tid
    assert tid not in [t.id for t in st.claimable(99999.0)] and tid not in [t.id for t in st.joinable(BEA.author, 1000.0)]
    log.add(BOSS, "goal.opened", task=tid, closed=True)
    assert tid not in log.state().goals and tid in [t.id for t in log.state().claimable(99999.0)]


def test_a_line_says_how_it_differs_from_what_its_front_already_has():
    log, tid = goal()
    other = S.task_id(log.events[2])  # w-0: not a goal
    propose(log, ANA, other, "sat", "Small cases by SAT")
    assert "research goal" in why(log)
    first = propose(log, ANA, tid, "sat", "Small cases by SAT", "Encode n = 4 as CNF and run a solver on it.")
    assert first["id"] in log.state().lines
    propose(log, BEA, tid, "sat", "Another SAT run", "Encode n = 5 with symmetry breaking and run kissat.")
    assert "already has 1 line(s)" in why(log) and "differs" in why(log)
    propose(log, BEA, tid, "paley", "Small cases by SAT again", "Encode n = 4 as CNF and run a solver on it.")
    assert "says nearly what line" in why(log)  # the same words on another front
    ok = propose(log, BEA, tid, "sat", "Another SAT run", "Encode n = 5 with symmetry breaking and run kissat.",
                 differs="It breaks the symmetry of the Paley automorphisms first, which the first line didn't.")
    st = log.state()
    assert ok["id"] in st.lines and [x["ev"]["id"] for x in st.open_lines(tid)] == [first["id"], ok["id"]]
    for bad, msg in ((dict(method="magic"), "method is one of"), (dict(move="hope"), "move is one of"),
                     (dict(learn=""), "learns if the line fails"), (dict(needs=["rm -rf"]), "needs lists")):
        propose(log, CEL, tid, "new", "A new one", **bad)
        assert msg in why(log)
    propose(log, CEL, tid, "Not A Key", "A new one")
    assert "front names" in why(log)
    for i in range(S.MAX_PROPOSALS):
        propose(log, CEL, tid, f"front-{i}", f"Unrelated topic number {i}", f"zeta{i} eta{i} theta{i} iota{i} kappa{i}")
    propose(log, CEL, tid, "front-9", "One too many", "lambda mu nu xi omicron")
    assert "proposed lines waiting per agent" in why(log)


def test_lines_are_backed_by_other_owners_once():
    log, tid = goal()
    p = propose(log, ANA, tid, "sat", "Small cases by SAT")
    log.add(Identity.generate("ana#2"), "line.backed", task=tid, line=p["id"])
    assert "own lines" in why(log)
    log.add(BEA, "line.backed", task=tid, line=p["id"])
    log.add(BEA, "line.backed", task=tid, line=p["id"])
    assert "backed that line already" in why(log)
    assert log.state().lines[p["id"]]["backed"] == {"bea": "bea"}


def test_a_round_takes_the_least_explored_fronts_first_and_no_method_takes_it_all():
    log, tid = goal()
    ids = {}
    for who, front, method, n in ((ANA, "sat", "compute", 1), (ANA, "sat-b", "compute", 2), (BEA, "census", "compute", 3),
                                  (BEA, "barrier", "barrier", 4), (CEL, "paley", "construct", 5)):
        ids[front] = propose(log, who, tid, front, f"Topic {n}", f"alpha{n} beta{n} gamma{n} delta{n} epsilon{n}",
                             method=method)["id"]
    log.add(BEA, "line.backed", task=tid, line=ids["paley"])
    st = log.state()
    pick = lambda n: [x["ev"]["front"] for x in R.select(st, tid, st.open_lines(tid), n)]  # noqa: E731
    assert pick(2) == ["paley", "sat"]  # the backed one first, then the earliest
    # Four lines: at most two by one method while another is on offer.
    assert pick(4) == ["paley", "sat", "sat-b", "barrier"] and sorted(pick(5)) == sorted(ids)
    # A front that already ran a line goes after the ones that never did.
    rnd, pieces = A.plan(st, st.tasks[tid], R.spec_for(st, st.tasks[tid], [st.lines[ids["paley"]]]), "ana")
    for p in pieces:
        log.add(ANA, "task.created", **p, split=True)
    propose(log, CEL, tid, "paley", "Topic 6", "zeta eta theta iota kappa", method="construct",
            differs="It takes the cubic residues where the first took the quadratic ones.")
    st = log.state()
    assert st.lines[ids["paley"]]["piece"] == st.find_task(f"f/r1-paley-{ids['paley'][:4]}").id
    assert [x["ev"]["front"] for x in R.select(st, tid, st.open_lines(tid), 5)][-1] == "paley"


def test_a_round_is_due_once_enough_agents_proposed_or_the_first_proposal_has_waited():
    log, tid = goal()
    assert R.due(log.state(), tid, 1000.0) is None  # nothing proposed
    propose(log, ANA, tid, "sat", "Topic 1", "alpha beta gamma delta epsilon")
    st = log.state()
    assert R.due(st, tid, 1000.0) is None and len(R.due(st, tid, 1000.0 + R.PROPOSE_WINDOW)) == 1
    propose(log, BEA, tid, "paley", "Topic 2", "zeta eta theta iota kappa")
    st = log.state()
    chosen = R.due(st, tid, 1000.0)
    assert [x["ev"]["front"] for x in chosen] == ["sat", "paley"]
    # Launched, its lines wait for someone: no other round until they have, and the proposals taken are in it.
    emitted = []
    c = types.SimpleNamespace(st=st, ident=types.SimpleNamespace(name="ana"), me=ANA.author, sync=lambda: None,
                              emit=lambda type, **p: emitted.append(log.add(ANA, type, **p)))
    assert R.may_launch(st, st.tasks[tid], ANA.author, 1000.0) and not R.may_launch(st, st.tasks[tid], BEA.author, 1000.0)
    assert R.launch(c, st.tasks[tid], chosen, 1000.0) == "r1"
    st = log.state()
    assert not st.ignored and not st.open_lines(tid)
    line = st.find_task(f"f/r1-{R.line_key(chosen[0])}")
    assert line.attack["role"] == "line" and "METHOD: compute. MOVE: specialise" in line.body
    assert "IF IT FAILS, THE PROJECT LEARNS: which small cases" in line.body and "legion ask f" in line.body
    assert sorted(t.key for t in R.free_lines(st, tid, 1000.0)) == sorted(f"f/r1-{R.line_key(x)}" for x in chosen)
    propose(log, CEL, tid, "census", "Topic 3", "lambda mu nu xi omicron")
    assert R.due(log.state(), tid, 1000.0 + 2 * R.PROPOSE_WINDOW) is None


def test_each_agent_is_handed_its_own_move_and_only_reframing_ones_once_rounds_go_dry():
    log, tid = goal()
    st = log.state()
    moves = {R.move_for(st, tid, f"agent#{i}") for i in range(40)}
    assert len(moves) >= 6 and moves <= set(S.MOVES)
    st.dry = lambda t: S.DRY_REFRAME
    assert {R.move_for(st, tid, f"agent#{i}") for i in range(40)} <= set(S.REFRAME)


def round_of(log, tid, front, n):
    """A round with one line, its check and synthesis accepted; returns the line's piece."""
    st = log.state()
    p = propose(log, ANA, tid, front, f"Topic {n}", f"alpha{n} beta{n} gamma{n} delta{n} epsilon{n}")
    st = log.state()
    _, pieces = A.plan(st, st.tasks[tid], R.spec_for(st, st.tasks[tid], [st.lines[p["id"]]]), "ana")
    made = []
    for i, piece in enumerate(pieces):
        ev = log.add(ANA, "task.created", **piece, split=True)
        who = BEA if piece["attack"]["role"] == "check" else CEL
        q = log.add(who, "patch.submitted", task=S.task_id(ev), commit=f"{n}{i}" * 20, branch=f"legion/r/{n}-{i}")
        log.add(BOSS, "patch.accepted", patch=q["id"], merge_commit=f"m{n}{i}")
        made.append(S.task_id(ev))
    return made[0]


def test_findings_make_the_map_and_rounds_that_confirm_nothing_spend_the_goal():
    log, tid = goal()
    line = round_of(log, tid, "sat", 1)
    f = log.add(CEL, "finding.stated", task=tid, line=line, cid="C-1", statement="f(4) >= 50", status="proved",
                kind="theorem", evidence="research/attacks/r1/x/proof.md")
    dead = log.add(CEL, "finding.stated", task=tid, line=line, cid="dead-ends", statement="Paley 47 has no S4.",
                   status="dead end")
    log.add(CEL, "finding.judged", task=tid, finding=f["id"], verdict="confirmed", reason="mine")
    assert "other than its author" in why(log)
    st = log.state()
    assert st.finding_state(st.findings[f["id"]]) == "unchecked" and st.findings[f["id"]]["front"] == "sat"
    assert st.research_rounds(tid)[0]["closed"] and st.dry(tid) == 1
    log.add(BEA, "finding.judged", task=tid, finding=f["id"], verdict="confirmed", reason="re-derived it")
    st = log.state()
    assert st.finding_state(st.findings[f["id"]]) == "confirmed" and st.independent(st.findings[f["id"]])
    assert st.dry(tid) == 0 and st.research_rounds(tid)[0]["confirmed"] == 1
    log.add(ANA, "finding.judged", task=tid, finding=f["id"], verdict="refuted", reason="step 3 divides by zero")
    assert log.state().finding_state(log.state().findings[f["id"]]) == "contested"
    log.add(ANA, "finding.judged", task=tid, finding=f["id"], verdict="confirmed", reason="I misread step 3")
    st = log.state()
    text = R.map_text(st, st.tasks[tid])
    assert "ESTABLISHED" in text and "f(4) >= 50" in text and "DEAD ENDS" in text and "Paley 47" in text
    assert "sat (compute): 1 line(s) run, 1 confirmed, 0 refuted, 1 dead end(s)" in text
    assert "construct" in text.split("METHODS nobody")[1]
    d = R.map_data(st, st.tasks[tid], 1000.0)
    assert d["lines"][0]["worker"] == "cel" and d["lines"][0]["checker"] == "bea" and d["findings"][0]["line"]
    # A finding replaced by its author leaves the map; one stated by a stranger never gets on it.
    log.add(CEL, "finding.stated", task=tid, line=line, cid="dead-ends", statement="Paley 47 and 59 have no S4.",
            status="dead end", replaces=dead["id"])
    assert [x["ev"]["statement"] for x in log.state().findings_for(tid)][-1] == "Paley 47 and 59 have no S4."
    assert len(log.state().findings_for(tid)) == 2
    log.add(Identity.generate("dan"), "finding.stated", task=tid, statement="P = NP", status="proved")
    assert "states its findings" in why(log)
    # Dry rounds: after DRY_SPENT of them the goal is spent, until someone reopens it.
    for n in range(2, 2 + S.DRY_SPENT):
        round_of(log, tid, f"front-{n}", n)
    st = log.state()
    assert st.dry(tid) == S.DRY_SPENT and st.spent(tid) and R.due(st, tid, 99999.0) is None
    assert "is spent" in R.waiting_text(st, ANA.author, 1000.0)
    log.add(BOSS, "goal.opened", task=tid, text="New: the literature has a doubling lemma we never used.")
    assert not log.state().spent(tid)


def test_a_task_let_go_of_again_with_nothing_new_on_it_isnt_handed_back():
    log, tid = research()
    for ts in (2000.0, 5000.0):
        log.add(BEA, "task.claimed", ts=ts, task=tid, ttl=600, join=True)
        log.add(BEA, "attempt.reported", ts=ts + 10, task=tid, outcome="failure", notes="nothing changed")
        log.add(BEA, "task.released", ts=ts + 11, task=tid)
        # Another agent saying it looked isn't news, nor are BEA's own notes.
        log.add(BEA, "task.note", ts=ts + 12, task=tid, text="freshness review: unchanged")
    st = log.state()
    assert tid in st.stale(BEA.author) and tid not in st.stale(ANA.author)
    assert tid not in st.cooling(BEA.author, 5000.0 + 10 * S.COOLDOWN)  # the cooldown alone had run out
    log.add(Identity.generate("bea#2"), "task.note", ts=6000.0, task=tid, text="I read it too: unchanged")
    assert tid in log.state().stale(BEA.author)  # its own owner's other agent saying it looked
    log.add(ANA, "work.saved", ts=7000.0, task=tid, commit="s" * 40, branch="legion/save/x", notes="a new lemma")
    assert tid not in log.state().stale(BEA.author)  # work by someone else: worth another look
    log.add(BEA, "task.claimed", ts=8000.0, task=tid, ttl=600, join=True)
    log.add(BEA, "task.released", ts=8010.0, task=tid)
    assert tid not in log.state().stale(BEA.author)  # once since the news: not yet
    log.add(BEA, "task.claimed", ts=9000.0, task=tid, ttl=600, join=True)
    log.add(BEA, "task.released", ts=9010.0, task=tid)
    assert tid in log.state().stale(BEA.author)
    log.add(BOSS, "task.note", ts=9500.0, task=tid, text="try the doubling lemma of the 1965 paper")
    assert tid not in log.state().stale(BEA.author)  # another owner's words count


def test_a_question_an_agent_keeps_taking_without_answering_goes_to_someone_else():
    log, tid = research()
    q = log.add(ANA, "question.asked", task=tid, text="Is f(4) = 51?")
    for i in range(S.QUESTION_TAKES):
        assert log.state().question_for(BEA.author, "bea", 1000.0 + i)["ev"]["id"] == q["id"]
        log.add(BEA, "question.taken", ts=1000.0 + i, task=tid, question=q["id"])
    st = log.state()
    assert st.question_for(BEA.author, "bea", 2000.0) is None
    assert st.question_for(CEL.author, "cel", 1000.0 + 3 * S.QUESTION_TTL)["ev"]["id"] == q["id"]


def test_a_piece_that_needs_a_program_stays_off_machines_without_it(monkeypatch):
    log, tid = goal()
    p = propose(log, ANA, tid, "lean", "Formalise the lemma", needs=["lake"])
    st = log.state()
    _, pieces = A.plan(st, st.tasks[tid], R.spec_for(st, st.tasks[tid], [st.lines[p["id"]]]), "ana")
    assert pieces[0]["needs"] == ["lake"] and "needs" not in pieces[1]  # the line needs it; its check says its own
    log.add(ANA, "task.created", **pieces[0], split=True)
    line = log.state().find_task(pieces[0]["key"])
    monkeypatch.setattr(R.shutil, "which", lambda name: None)
    assert R.lacking(line) == ["lake"]
    monkeypatch.setattr(R.shutil, "which", lambda name: "/usr/bin/" + name)
    assert R.lacking(line) == []


def test_the_cycle_from_the_command_line(tmp_path, monkeypatch, capsys):
    """A goal is opened; agents asked for a task are asked for a line instead; with two proposed, the next claim
    launches the round and hands out its line; the line's report and its check's verdicts land on the map."""
    from legion.cli import main
    from legion.ctx import Ctx
    from test_queue import as_, git, make_project
    repo, tids, _ = make_project(tmp_path, monkeypatch)
    boss = make_project.people["boss"]
    for k in tids.values():  # nothing else to hand out
        q = make_project.add(boss, "patch.submitted", task=k, commit=git(repo, "rev-parse", "HEAD"), branch="legion/x")
        make_project.add(boss, "patch.accepted", patch=q["id"])
    fid = S.task_id(make_project.add(boss, "task.created", key="f", title="F", zone=["research/"],
                                     accept="python check.py a"))
    as_(repo, "boss")
    assert main(["goal", "f", "--text", "Any new bound counts."]) == 0
    capsys.readouterr()
    assert main(["claim"]) == 7  # not the goal itself: a line for its next round
    shown = capsys.readouterr().out
    assert "f is a research goal" in shown and "YOUR MOVE:" in shown and "line propose f" in shown
    assert not Ctx(sync=False).st.claims.get(fid)  # and no claim was recorded for it
    line = tmp_path / "line.txt"
    line.write_text("Encode n = 4 as CNF and decide it with a SAT solver. Result: SAT with the tournament, or UNSAT "
                    "with the proof.", encoding="utf-8")
    args = ["--method", "compute", "--file", str(line), "--learn", "whether n = 4 is within a solver's reach"]
    assert main(["line", "propose", "f", "--front", "sat", "--title", "Decide n = 4 by SAT", *args]) == 0
    assert main(["line", "propose", "f", "--front", "sat", "--title", "Decide n = 5 by cubes", "--method", "compute",
                 "--text", "Cube and conquer on the n = 5 encoding.", "--learn", "x"]) == 1  # the front has a line
    assert "differs" in capsys.readouterr().err
    as_(repo, "luis")
    assert main(["line", "list", "f"]) == 0
    assert "aren't shown until you have proposed yours" in capsys.readouterr().out  # decide yours first
    assert main(["line", "propose", "f", "--front", "barrier", "--title", "Why Paley can't do it", "--method",
                 "barrier", "--text", "Prove that quadratic residue tournaments miss the bound by a log factor.",
                 "--learn", "which hypothesis of the barrier fails"]) == 0
    shown = capsys.readouterr().out
    st = Ctx(sync=False).st
    assert "round r1 on f launched from 2 proposed line(s)" in shown  # two agents proposed: it's due, and luis may
    keys = sorted(t.key for t in st.tasks.values() if t.parent == fid and t.attack.get("role") == "line")
    assert len(keys) == 2 and keys[0].startswith("f/r1-barrier-") and keys[1].startswith("f/r1-sat-")
    assert main(["claim"]) == 0 and "The task is yours" in capsys.readouterr().out  # a line of the round
    held = Ctx(sync=False).st.held_by(make_project.people["luis"].author, time.time())[0]
    assert held.attack["role"] == "line"

    def work(who, key, files):
        """`who` takes the piece, writes its files and submits it; boss integrates."""
        as_(repo, who)
        assert main(["claim", key]) == 0 and main(["start", key]) == 0
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", key)
        assert main(["submit", key]) == 0
        git(repo, "checkout", "-q", "main")
        as_(repo, "boss")
        integrator.integrate_once(Ctx(), 60, log=lambda *_: None)
        return Ctx(sync=False).st

    d = held.zone[0].rstrip("/")
    claim = {"id": "L-1", "statement": "There is an S4 tournament on 51 vertices.", "kind": "construction",
             "status": "checked by computation", "evidence": f"{d}/t51.txt", "how_to_check": "count its bad 4-sets"}
    report = {"summary": "found one", "claims": [claim], "next_step": "try 50",
              "dead_ends": "Circulants on 49 and 50 vertices: none has S4, by exhaustive search."}
    st = work("luis", held.key, {f"{d}/report.json": json.dumps(report)})
    found = st.findings_for(fid)
    assert [(f["ev"]["cid"], f["ev"]["status"]) for f in found] == [("L-1", "checked by computation"),
                                                                     ("dead-ends", "dead end")]
    assert all(st.finding_state(f) == "unchecked" for f in found) and found[0]["front"] in ("sat", "barrier")
    verdicts = {"verdicts": [{"claim_id": "L-1", "verdict": "confirmed", "reason": "recounted: 0 bad 4-sets"}],
                "overall": "it stands"}
    st = work("marta", held.key + "-check", {f"{d}-check/verdicts.json": json.dumps(verdicts)})
    assert st.finding_state(st.findings_for(fid)[0]) == "confirmed"
    capsys.readouterr()
    assert main(["map", "f", "--no-sync"]) == 0
    shown = capsys.readouterr().out
    assert "ESTABLISHED" in shown and "S4 tournament on 51 vertices" in shown and "marta#1: confirmed" in shown
    assert "DEAD ENDS" in shown and "Circulants on 49 and 50" in shown
    main(["status", "--no-sync"])
    assert "research" in capsys.readouterr().out
    as_(repo, "boss")
    main(["context", "--no-update"])
    assert "## Research goals" in capsys.readouterr().out


def test_claim_says_once_that_nothing_is_new_instead_of_handing_the_same_task_back(tmp_path, monkeypatch, capsys):
    from legion.cli import main
    from legion.ctx import Ctx
    from test_queue import as_, make_project
    repo, tids, _ = make_project(tmp_path, monkeypatch)
    as_(repo, "luis")
    for k in ("a", "b"):
        for _ in range(S.NOOP_AFTER):
            assert main(["claim", tids[k]]) == 0
            assert main(["release", tids[k], "--notes", "read it all: nothing changed"]) == 0
    monkeypatch.setattr(S.State, "needs_ideas", lambda self, *a, **k: None)  # it has no new routes to offer either
    before = len(Ctx(sync=False).st.events)
    capsys.readouterr()
    assert main(["claim"]) == 5
    shown = capsys.readouterr().out
    assert "Nothing is new on a, b" in shown and "legion wait" in shown
    assert len(Ctx(sync=False).st.events) == before  # and it recorded nothing
    assert main(["claim", tids["a"]]) == 5 and "nothing new has come on it" in capsys.readouterr().err
    assert main(["claim", tids["a"], "--route", "induct on the out-degree instead"]) == 0
    assert "another route: induct" in Ctx(sync=False).st.notes[tids["a"]][-1]["text"]


def test_the_viewer_gets_the_map_who_worked_for_whom_and_the_pulse():
    from legion import viz
    log, tid = goal()
    line = round_of(log, tid, "sat", 1)
    f = log.add(CEL, "finding.stated", task=tid, line=line, cid="C-1", statement="f(4) >= 50", status="proved")
    log.add(BEA, "finding.judged", task=tid, finding=f["id"], verdict="confirmed", reason="re-derived it")
    propose(log, BEA, tid, "paley", "Topic 9", "zeta eta theta iota kappa")
    snap = viz.snapshot(sorted(log.events, key=S.event_key), None)
    (m,) = snap["research"]["maps"]
    assert m["goal"] and m["task"] == "f" and [x["state"] for x in m["lines"]] == ["accepted", "proposed"]
    assert m["findings"][0]["state"] == "confirmed" and m["findings"][0]["line"] == m["lines"][0]["id"]
    assert m["rounds"] == [{"round": "r1", "lines": 1, "confirmed": 1, "closed": True}] and m["methods"] == {"compute": 1}
    assert {"from": viz.agent_id(BEA.author), "from_name": "bea", "to": viz.agent_id(CEL.author), "to_name": "cel",
            "kind": "checked", "n": 1} in snap["research"]["links"]
    assert set(snap["research"]["pulse"]) == {"fresh", "looks", "talk"}
    assert next(t for t in snap["tasks"] if t["id"] == tid)["research"]


def test_what_happens_to_an_agents_findings_and_lines_reaches_its_inbox_and_an_old_free_line_doesnt_block():
    log, tid = goal()
    line = round_of(log, tid, "sat", 1)
    f = log.add(CEL, "finding.stated", task=tid, line=line, cid="C-1", statement="f(4) >= 50", status="proved")
    log.add(BEA, "finding.judged", task=tid, finding=f["id"], verdict="refuted", reason="step 3 divides by zero")
    log.add(ANA, "finding.stated", task=tid, statement="so f(5) >= 110", status="partial", **{"from": [f["id"]]})
    st = log.state()
    texts = [i["text"] for i in st.inbox(CEL.author, "cel", 1000.0)]
    assert any("judged your finding" in t and "refuted: step 3" in t for t in texts)
    assert any("built on a finding of yours" in t for t in texts)
    assert any("the line you proposed is in a round" in i["text"] for i in st.inbox(ANA.author, "ana", 1000.0)) is False
    # A line on offer holds the next round back, but not for ever.
    p = propose(log, BEA, tid, "paley", "Topic 7", "zeta eta theta iota kappa")
    st = log.state()
    _, pieces = A.plan(st, st.tasks[tid], R.spec_for(st, st.tasks[tid], [st.lines[p["id"]]]), "ana")
    log.add(ANA, "task.created", ts=5000.0, **pieces[0], split=True)
    assert any("the line you proposed is in a round" in i["text"] for i in log.state().inbox(BEA.author, "bea", 5000.0))
    propose(log, CEL, tid, "census", "Topic 8", "lambda mu nu xi omicron", ts=5000.0)
    st = log.state()
    assert R.due(st, tid, 5000.0 + R.PROPOSE_WINDOW) is None
    assert [x["ev"]["front"] for x in R.due(st, tid, 5000.0 + R.LINE_WAIT)] == ["census"]


def test_findings_nobody_but_their_author_stands_behind_are_handed_out_to_judge():
    log, tid = goal()
    f = log.add(ANA, "finding.stated", task=tid, statement="f(4) >= 50", status="proved", evidence="in main")
    dead = log.add(ANA, "finding.stated", task=tid, statement="Paley 47 has no S4.", status="dead end")
    log.add(ANA, "finding.stated", task=tid, statement="Is f(5) = 111?", status="unproved")  # open: nothing to judge
    st = log.state()
    assert st.finding_for(ANA.author, "ana") is None  # nobody judges their own
    picks = {st.finding_for(Identity.generate(f"x{i}").author, f"x{i}")["ev"]["id"] for i in range(12)}
    assert picks == {f["id"], dead["id"]}  # each agent its own among the first few; never the open one
    text = R.judge_text(st, st.findings[f["id"]])
    assert "f(4) >= 50" in text and "FIND WHAT IS WRONG" in text and f"finding judge {f['id'][:8]}" in text
    # Two agents' say is enough; whoever said isn't asked again, "not verified" included.
    log.add(BEA, "finding.judged", task=tid, finding=f["id"], verdict="not verified", reason="no Lean here")
    st = log.state()
    assert st.finding_for(BEA.author, "bea")["ev"]["id"] == dead["id"]
    log.add(CEL, "finding.judged", task=tid, finding=f["id"], verdict="confirmed", reason="the file proves it")
    log.add(BEA, "finding.judged", task=tid, finding=dead["id"], verdict="confirmed", reason="reran the search")
    log.add(CEL, "finding.judged", task=tid, finding=dead["id"], verdict="confirmed", reason="reran it too")
    assert log.state().finding_for(Identity.generate("dan").author, "dan") is None
    # Confirmed only by its author's own owner: another owner's agent is still asked; disagreement asks for more.
    g = log.add(ANA, "finding.stated", task=tid, statement="f(5) >= 110", status="partial")
    log.add(Identity.generate("ana#2"), "finding.judged", task=tid, finding=g["id"], verdict="confirmed", reason="ok")
    log.add(Identity.generate("ana#3"), "finding.judged", task=tid, finding=g["id"], verdict="confirmed", reason="ok")
    st = log.state()
    assert st.finding_for(Identity.generate("ana#4").author, "ana#4") is None
    assert st.finding_for(BEA.author, "bea")["ev"]["id"] == g["id"]
    log.add(BEA, "finding.judged", task=tid, finding=g["id"], verdict="refuted", reason="the case n = 5 is missing")
    st = log.state()
    assert st.finding_state(st.findings[g["id"]]) == "contested" and st.finding_for(CEL.author, "cel")["ev"]["id"] == g["id"]
