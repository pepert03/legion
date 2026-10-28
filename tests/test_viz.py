import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from legion import coordgit, viz
from legion import events as E
from legion.identity import Identity

BOSS = Identity.generate("boss")
ANA = Identity.generate("ana")


def project_events():
    evs = [E.create(BOSS, "project.init", 1, project="p", integrators=[BOSS.author]),
           E.create(BOSS, "task.created", 2, key="a", title="A", zone=["a.py"], accept="true")]
    tid = "t-" + evs[1]["id"][:8]
    evs.append(E.create(ANA, "task.claimed", 3, task=tid, ttl=3600))
    return evs, tid


def test_snapshot_live_and_replay():
    evs, tid = project_events()
    live = viz.snapshot(evs, None)
    assert live["live"] and live["index"] == 3
    assert [t["status"] for t in live["tasks"]] == ["claimed"]
    ana = next(a for a in live["agents"] if a["name"] == "ana")
    assert ana["holds"] == [tid] and ana["active"]
    past = viz.snapshot(evs, 2)  # before the claim
    assert not past["live"] and past["tasks"][0]["status"] == "open"
    assert all(not a["holds"] for a in past["agents"])


def test_history_of_task_and_agent():
    evs, tid = project_events()
    assert any(item["text"] == "claims a" for item in viz.history(evs, tid, None))
    ana = viz.agent_id(ANA.author)
    items = viz.history(evs, ana, None)
    assert [i["type"] for i in items] == ["task.claimed"] and items[0]["name"] == "ana"


def test_recent_activity_skips_heartbeats():
    evs, tid = project_events()
    evs.append(E.create(ANA, "task.heartbeat", 4, task=tid))
    recent = viz.snapshot(evs, None)["recent"]
    assert [r["type"] for r in recent] == ["project.init", "task.created", "task.claimed"]


def test_custom_logo_is_served(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    logos = tmp_path / ".legion" / "logos"
    logos.mkdir(parents=True)
    (logos / "claude.svg").write_text("<svg/>")
    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler(viz.Source(tmp_path, 60, fetch=False)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        r = urllib.request.urlopen(base + "/logo/claude")
        assert r.headers["Content-Type"] == "image/svg+xml" and r.read() == b"<svg/>"
        for bad in ("/logo/..%2Fsecret", "/logo/codex"):
            try:
                urllib.request.urlopen(base + bad)
                raise AssertionError(f"{bad} should give 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        server.shutdown()


def test_http_endpoints(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    evs, _ = project_events()
    for ev in evs:
        coordgit.append(tmp_path, BOSS, ev)
    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler(viz.Source(tmp_path, 60, fetch=False)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "force-graph" in page
        state = json.loads(urllib.request.urlopen(base + "/state.json?upto=2").read())
        assert state["project"] == "p" and state["index"] == 2 and state["total"] == 3
    finally:
        server.shutdown()


def test_agent_names_are_person_and_number():
    assert viz.split_name("ana#2") == ("ana", 2)
    assert viz.split_name("ana") == ("ana", None)
    assert viz.split_name("#3") == ("#3", None)
    assert viz.split_name("ana#x") == ("ana#x", None)


def test_timeline_marks_milestones_with_their_position():
    evs, tid = project_events()
    evs.append(E.create(ANA, "task.heartbeat", 4, task=tid))
    marks = viz.timeline(evs)
    assert [(m["i"], m["type"]) for m in marks] == [(1, "project.init"), (2, "task.created"), (3, "task.claimed")]


def test_every_event_type_has_a_description():
    from legion.cli import event_text
    from legion import state as S
    st = S.build([])
    for t in E.TYPES:
        assert event_text({"type": t, "id": "x" * 16, "lamport": 1}, st) != t, t


def test_an_agent_that_only_joined_shows_up():
    from legion import state as S
    boss, muse = Identity.generate("boss"), Identity.generate("jose#1")
    evs = [E.create(boss, "project.init", 1, project="p", integrators=[boss.author]),
           E.create(muse, "agent.joined", 2)]
    assert not S.build(evs).ignored
    assert any(a["name"] == "jose#1" for a in viz.snapshot(evs, len(evs))["agents"])


def test_an_agent_silent_for_hours_with_nothing_held_leaves_the_graph(monkeypatch):
    evs, tid = project_events()
    evs.append(E.create(ANA, "task.heartbeat", 4, task=tid))  # ana's claim is renewed: still hers past the threshold
    evs[2] = E.create(ANA, "task.claimed", 3, task=tid, ttl=3 * 3600)
    luis = Identity.generate("luis")
    evs.append(E.create(luis, "agent.joined", 5))
    now = evs[-1]["ts"]
    monkeypatch.setattr(viz.time, "time", lambda: now + viz.GONE_SECONDS + 1)
    agents = {a["name"]: a for a in viz.snapshot(evs, None)["agents"]}
    assert agents["luis"]["gone"]          # silent and holding nothing
    assert not agents["ana"]["gone"]       # silent too, but it holds a claim: it's working
    monkeypatch.setattr(viz.time, "time", lambda: now + 10)
    assert not {a["name"]: a for a in viz.snapshot(evs, None)["agents"]}["luis"]["gone"]


def test_an_agent_with_a_claim_and_nothing_since_is_silent(monkeypatch):
    """A claim keeps the task until it expires, but an agent that stopped mustn't look at work meanwhile."""
    evs, tid = project_events()  # ana claims for an hour
    claimed = evs[-1]["ts"]
    monkeypatch.setattr(viz.time, "time", lambda: claimed + 600)
    ana = next(a for a in viz.snapshot(evs, None)["agents"] if a["name"] == "ana")
    assert ana["holds"] == [tid] and ana["silent"] is None
    monkeypatch.setattr(viz.time, "time", lambda: claimed + viz.SILENT_SECONDS + 60)
    ana = next(a for a in viz.snapshot(evs, None)["agents"] if a["name"] == "ana")
    assert ana["holds"] == [tid] and ana["silent"] == viz.SILENT_SECONDS + 60
    assert ana["claim_ends"] == claimed + 3600


def test_a_checker_sits_on_the_task_until_its_result(monkeypatch):
    evs, tid = project_events()
    p = E.create(ANA, "patch.submitted", 4, task=tid, commit="c" * 40, branch="legion/t/a-1")
    luis = Identity.generate("luis")
    evs += [p, E.create(luis, "patch.checking", 5, task=tid, patch=p["id"])]
    luis_now = lambda: next(a for a in viz.snapshot(evs, None)["agents"] if a["name"] == "luis")  # noqa: E731
    assert luis_now()["holds"] == [tid] and luis_now()["checking"] == [tid]
    assert any(i["text"].startswith("checks patch") for i in viz.history(evs, tid, None))  # in the task's history
    evs.append(E.create(luis, "patch.verified", 6, task=tid, patch=p["id"], base_tree="a" * 40, tree="b" * 40,
                        outcome="pass"))
    assert luis_now()["holds"] == []  # its result is in: back off the node
    evs.append(E.create(BOSS, "patch.checking", 7, task=tid, patch=p["id"]))
    boss = next(a for a in viz.snapshot(evs, None)["agents"] if a["name"] == "boss")
    assert boss["holds"] == [tid]  # the integrator too, while it reviews
    monkeypatch.setattr(viz.time, "time", lambda: evs[-1]["ts"] + viz.CHECKING_SECONDS + 1)
    assert next(a for a in viz.snapshot(evs, None)["agents"] if a["name"] == "boss")["holds"] == []  # abandoned


def test_in_the_open_mode_founders_are_agents_and_trusted_ones_are_marked():
    evs, tid = project_events()
    evs.append(E.create(BOSS, "project.policy", 4, mode="open", quorum=1))
    snap = viz.snapshot(evs, None)
    boss = next(a for a in snap["agents"] if a["name"] == "boss")
    ana = next(a for a in snap["agents"] if a["name"] == "ana")
    assert snap["mode"] == "open" and snap["people"] == {}
    assert boss["role"] == "founder" and boss["trusted"] and ana["role"] == "agent" and not ana["trusted"]


def test_the_hub_sits_at_the_centre_with_its_chat_and_literature():
    evs, tid = project_events()
    evs += [E.create(ANA, "hub.message", 4, text="stuck on the counting", task=tid),
            E.create(BOSS, "lit.added", 5, url="arxiv:2101.00001", title="Tournaments", why="the counting"),
            E.create(ANA, "lit.added", 6, url="https://arxiv.org/pdf/2101.00001", why="yes, Lemma 4")]
    snap = viz.snapshot(evs, None)
    assert snap["hub"] == {"messages": 1, "literature": 1}
    ana = next(a for a in snap["agents"] if a["name"] == "ana")
    assert ana["at_hub"] and ana["holds"] == [tid]          # it came to the hub from its task
    hub = viz.hub(evs, None)
    assert [e["type"] for e in hub["chat"]] == ["hub.message", "lit.added", "lit.added"]
    assert hub["chat"][2]["title"] == "Tournaments" and hub["chat"][2]["url"] == "https://arxiv.org/pdf/2101.00001"
    assert [(r["url"], [b["name"] for b in r["by"]]) for r in hub["literature"]] == [
        ("https://arxiv.org/abs/2101.00001", ["boss", "ana"])]
    assert "hub.message" in [e["type"] for e in viz.history(evs, tid, None)]  # it's part of the task's conversation


def test_a_result_is_drawn_under_its_task():
    evs, tid = project_events()
    evs += [E.create(BOSS, "project.policy", 4, results_dir="R", results_check="check {file}"),
            E.create(ANA, "result.submitted", 5, task=tid, key="lemma", title="A lemma", files=["R/L.lean"],
                     commit="c", branch=f"legion/{tid}/result-ana-1")]
    r = next(t for t in viz.snapshot(evs, None)["tasks"] if t["kind"] == "result")
    assert (r["parent"], r["status"], r["key"]) == (tid, "review", "a/lemma")
    assert [e["type"] for e in viz.hub(evs, None)["chat"]] == ["result.submitted"]
    assert [e["type"] for e in viz.history(evs, r["id"], None)] == ["result.submitted"]  # its own window has it
