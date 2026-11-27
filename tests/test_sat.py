"""A SAT solver for agents whose machine has none: a request names a commit and a problem, a node with kissat answers
it once, and says whether its answer is certified (a model checked against every clause, a proof drat-trim verified)."""
import gzip
import types

from legion import coordgit, integrator
from legion import sat as SAT
from legion.identity import Identity
from test_attack import research
from test_state import ANA, BOSS

CEL = Identity.generate("cel")
SHA = "a" * 40
BRANCH = coordgit.PATCHES + "sat/ana-1"


def why(log):
    return log.state().ignored[-1][1]


def ask(log, who=ANA, **kw):
    return log.add(who, "sat.requested", **{"path": "x/p.cnf", "commit": SHA, "branch": BRANCH, "seconds": 600,
                                            "vars": 3, **kw})


def test_a_trusted_agent_asks_for_a_solve_and_one_answer_comes_back_to_it():
    log, _ = research()  # ANA is trusted there
    ask(log, CEL)
    assert "trusted agent" in why(log)
    ask(log, path="x/p.txt")
    assert ".py printing one" in why(log)
    ask(log, path="../p.cnf")
    assert ".py printing one" in why(log)
    ask(log, branch="refs/heads/main")
    assert "patch namespace" in why(log)
    ask(log, seconds=99999)
    assert "seconds" in why(log)
    ask(log, vars=10**6)
    assert "variables" in why(log)
    req = ask(log, path="x/gen.py")
    st = log.state()
    assert [e["id"] for e in st.sat_pending(1000.0)] == [req["id"]]
    assert st.sat_pending(1000.0 + 25 * 3600) == []  # an old one isn't waited on
    log.add(BOSS, "sat.checked", request=req["id"], outcome="unknown", certified=True, output="", assignment="")
    assert "only a model or a refutation" in why(log)
    log.add(BOSS, "sat.checked", request=req["id"], outcome="maybe", certified=False, output="", assignment="")
    assert "outcome is one of" in why(log)
    log.add(BOSS, "sat.checked", request=req["id"], outcome="unsat", certified=True,
            output="kissat: unsat after 3 s; drat-trim verified its proof in 1 s", assignment="")
    log.add(BOSS, "sat.checked", request=req["id"], outcome="sat", certified=True, output="", assignment="1 2")
    assert "answered already" in why(log)
    st = log.state()
    assert st.sat_pending(1000.0) == [] and st.sat[req["id"]]["checked"]["outcome"] == "unsat"
    said = [i for i in st.inbox(ANA.author, "ana", 1000.0) if i["id"] == st.sat[req["id"]]["checked"]["id"]]
    assert said and "unsat certified" in said[0]["text"] and "drat-trim verified" in said[0]["text"]
    for i in range(6):
        ask(log, ts=2000.0 + i, path=f"x/q{i}.cnf")
    assert "SAT solves a day" in why(log)


def test_a_model_is_checked_against_every_clause(tmp_path):
    cnf = tmp_path / "p.cnf"
    cnf.write_text("c a comment\np cnf 3 3\n1 2 0\n-1 3\n0 -3 -2 0\n", encoding="utf-8")
    good = SAT.model_of("s SATISFIABLE\nv 1 -2 3 0\n")
    assert good == {1: True, 2: False, 3: True}
    assert SAT.check_model(cnf, good) is None
    assert SAT.check_model(cnf, {1: True, 2: True, 3: True}) == "clause 3 is false under the model"
    assert SAT.check_model(cnf, {}) == "clause 1 is false under the model"  # unassigned is not true
    cut = tmp_path / "cut.cnf"
    cut.write_text("p cnf 2 2\n1 0\n2", encoding="utf-8")
    assert "middle of a clause" in SAT.check_model(cut, {1: True, 2: True})


def test_the_problem_comes_as_plain_dimacs_from_a_compressed_file_or_a_script(tmp_path, monkeypatch):
    folder, work = tmp_path / "c", tmp_path / "w"
    (folder / "x").mkdir(parents=True)
    work.mkdir()
    with gzip.open(folder / "x" / "p.cnf.gz", "wt") as f:
        f.write("p cnf 1 1\n1 0\n")
    out, why_not = SAT.plain_cnf(folder, "x/p.cnf.gz", work)
    assert why_not == "" and out.read_text() == "p cnf 1 1\n1 0\n"
    ran = []
    monkeypatch.setattr(integrator, "run_cmd", lambda cmd, cwd, timeout: (ran.append((cmd, cwd)), (2, "Traceback"))[1])
    out, why_not = SAT.plain_cnf(folder, "x/gen.py", work)
    assert out is None and "failed (exit 2)" in why_not and "Traceback" in why_not
    assert 'gen.py" >' in ran[0][0] and ran[0][1] == folder / "x"  # run beside its helpers
    script = tmp_path / "s" / "gen.py"
    script.parent.mkdir()
    script.write_text("print(1)")
    (script.parent / "helper.py").write_text("")
    assert [p.name for p in SAT.files_for(script)] == ["gen.py", "helper.py"]
    assert SAT.files_for(folder / "x" / "p.cnf.gz") == [folder / "x" / "p.cnf.gz"]


def serving(log, tmp_path, monkeypatch, run):
    monkeypatch.setattr(coordgit, "need_commit", lambda repo, sha, ref=None, wait=0.0: True)
    monkeypatch.setattr(integrator, "common_dir", lambda repo: tmp_path)

    def checkout(repo, commit, dest):
        (dest / "x").mkdir(parents=True, exist_ok=True)
        (dest / "x" / "p.cnf").write_text("p cnf 3 2\n1 2 0\n-1 3 0\n", encoding="utf-8")
        return dest
    monkeypatch.setattr(integrator, "checkout", checkout)
    monkeypatch.setattr(integrator, "run_cmd", run)
    monkeypatch.setattr(SAT, "tool", lambda name: f"/opt/{name}")
    c = types.SimpleNamespace(st=log.state(), repo=tmp_path, me=BOSS.author, sync=lambda: None)

    def emit(type, **kw):
        ev = log.add(BOSS, type, **kw)
        c.st = log.state()
        return ev
    c.emit = emit
    return c


def test_serve_solves_and_certifies_a_model(monkeypatch, tmp_path):
    log, _ = research()
    req = ask(log, vars=2)
    ran = []

    def run(cmd, cwd, timeout):
        ran.append(cmd)
        return 10, "s SATISFIABLE\nv 1 -2 3 0\n"
    c = serving(log, tmp_path, monkeypatch, run)
    assert SAT.serve(c, 1000.0, log=lambda *_: None) == 1
    assert "--time=600" in ran[0] and "kissat" in ran[0] and "--no-binary" in ran[0]
    ch = log.state().sat[req["id"]]["checked"]
    assert ch["outcome"] == "sat" and ch["certified"] and ch["assignment"] == "1 -2"
    assert "satisfies every clause" in ch["output"]
    assert not (tmp_path / "legion-review" / "sat-work").exists()  # the proof's gigabytes don't stay
    assert SAT.serve(c, 1000.0, log=lambda *_: None) == 0  # answered: not again


def test_serve_certifies_a_refutation_only_when_its_proof_checks(monkeypatch, tmp_path):
    log, _ = research()
    a, b = ask(log), ask(log, path="x/q.cnf")
    answers = iter([(20, "s UNSATISFIABLE\n"), (0, "c parsing\ns VERIFIED\n"),
                    (20, "s UNSATISFIABLE\n"), (1, "c ERROR: ...\ns NOT VERIFIED\n")])
    c = serving(log, tmp_path, monkeypatch, lambda cmd, cwd, timeout: next(answers))
    assert SAT.serve(c, 1000.0, most=2, log=lambda *_: None) == 2
    st = log.state()
    ok, bad = st.sat[a["id"]]["checked"], st.sat[b["id"]]["checked"]
    assert ok["outcome"] == "unsat" and ok["certified"] and "drat-trim verified" in ok["output"]
    assert bad["outcome"] == "unsat" and not bad["certified"] and "did not verify" in bad["output"]
    said = [i["text"] for i in st.inbox(ANA.author, "ana", 1000.0) if i["id"] == bad["id"]]
    assert said and "unsat NOT certified" in said[0]


def test_serve_says_unknown_when_time_runs_out_and_answers_nothing_without_kissat(monkeypatch, tmp_path):
    log, _ = research()
    req = ask(log)
    c = serving(log, tmp_path, monkeypatch, lambda cmd, cwd, timeout: (0, "s UNKNOWN\n"))
    monkeypatch.setattr(SAT, "tool", lambda name: None)
    assert SAT.serve(c, 1000.0, log=lambda *_: None) == 0
    monkeypatch.setattr(SAT, "tool", lambda name: f"/opt/{name}")
    assert SAT.serve(c, 1000.0, log=lambda *_: None) == 1
    ch = log.state().sat[req["id"]]["checked"]
    assert ch["outcome"] == "unknown" and not ch["certified"]
