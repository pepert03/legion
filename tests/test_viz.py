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
