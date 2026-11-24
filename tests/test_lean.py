"""Lean for agents whose machine has none: a request names a commit and a file, a node with Lean answers it once."""
import types

from legion import coordgit, integrator
from legion import lean as L
from legion.identity import Identity
from test_attack import research
from test_state import ANA, BOSS

CEL = Identity.generate("cel")
SHA = "a" * 40
BRANCH = coordgit.PATCHES + "lean/ana-1"


def why(log):
    return log.state().ignored[-1][1]


def test_a_trusted_agent_asks_for_a_compile_and_one_answer_comes_back_to_it():
    log, _ = research()  # ANA is trusted there
    log.add(CEL, "lean.requested", path="x/A.lean", commit=SHA, branch=BRANCH)
    assert "trusted agent" in why(log)
    log.add(ANA, "lean.requested", path="../A.lean", commit=SHA, branch=BRANCH)
    assert ".lean file's path" in why(log)
    log.add(ANA, "lean.requested", path="x/A.lean", commit=SHA, branch="refs/heads/main")
    assert "patch namespace" in why(log)
    req = log.add(ANA, "lean.requested", path="x/A.lean", commit=SHA, branch=BRANCH)
    st = log.state()
    assert [e["id"] for e in st.lean_pending(1000.0)] == [req["id"]]
    assert st.lean_pending(1000.0 + 25 * 3600) == []  # an old one isn't waited on
    log.add(BOSS, "lean.checked", request=req["id"], ok=False, output="x/A.lean:3:2: error: unknown identifier")
    log.add(BOSS, "lean.checked", request=req["id"], ok=True, output="")
    assert "answered already" in why(log)
    st = log.state()
    assert st.lean_pending(1000.0) == [] and st.lean[req["id"]]["checked"]["ok"] is False
    said = [i for i in st.inbox(ANA.author, "ana", 1000.0) if i["id"] == st.lean[req["id"]]["checked"]["id"]]
    assert said and "it fails" in said[0]["text"] and "unknown identifier" in said[0]["text"]
    for i in range(30):
        log.add(ANA, "lean.requested", ts=2000.0 + i, path=f"x/B{i}.lean", commit=SHA, branch=BRANCH)
    assert "Lean compiles a day" in why(log)


def test_serve_compiles_each_request_in_a_folder_of_its_own_and_answers_it(monkeypatch, tmp_path):
    log, _ = research()
    req = log.add(ANA, "lean.requested", path="x/A.lean", commit=SHA, branch=BRANCH)
    ran = []
    monkeypatch.setattr(coordgit, "need_commit", lambda repo, sha, ref=None, wait=0.0: True)
    monkeypatch.setattr(integrator, "common_dir", lambda repo: tmp_path)
    monkeypatch.setattr(integrator, "checkout", lambda repo, commit, dest: dest.mkdir(parents=True) or dest)
    monkeypatch.setattr(integrator, "run_cmd", lambda cmd, cwd, timeout: (ran.append((cmd, cwd)), (1, "error: bad"))[1])
    c = types.SimpleNamespace(st=log.state(), repo=tmp_path, me=BOSS.author, sync=lambda: None)

    def emit(type, **kw):
        ev = log.add(BOSS, type, **kw)
        c.st = log.state()
        return ev
    c.emit = emit
    assert L.serve(c, 1000.0, log=lambda *_: None) == 1
    assert ran and "x/A.lean" in ran[0][0] and ran[0][1] == tmp_path / "legion-review" / "lean"
    st = log.state()
    assert st.lean[req["id"]]["checked"]["ok"] is False and st.lean[req["id"]]["checked"]["output"] == "error: bad"
    assert L.serve(c, 1000.0, log=lambda *_: None) == 0  # answered: not again
