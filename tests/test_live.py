"""The live plane: events, messages and commits between clones through relays, with no git transport between them."""

import json
import subprocess
import threading
import time

import pytest

from legion import coordgit, identity, live, nostr
from legion import events as E
from legion.identity import Identity

from relay import Relay


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def relay(monkeypatch, tmp_path):
    """A relay on this machine, and legion told to use it and nothing else."""
    r = Relay()
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEGION_LIVE", "on")
    monkeypatch.setenv("LEGION_RELAYS", r.url)
    yield r
    nostr.close_all()
    nostr.forget()
    live._sent.clear()
    live._folders.clear()
    r.close()


def clone(tmp_path, name: str):
    """A repository of its own, with no remote and no peer: all it shares with the others is the relay."""
    repo = tmp_path / name.split("#")[0]
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "legion.transport", "relay")
    git(repo, "config", "legion.livekey", "11" * 32)
    ident = Identity.generate(name)
    identity.bind(repo, ident)
    return repo, ident


def record(repo, ident, type: str, **payload) -> dict:
    ev = E.create(ident, type, coordgit.state(repo).max_lamport + 1, **payload)
    coordgit.append(repo, ident, ev)
    return ev


def until(what, seconds: float = 5.0):
    end = time.time() + seconds
    while time.time() < end:
        got = what()
        if got:
            return got
        time.sleep(0.02)
    return what()


# --- the client ---------------------------------------------------------------------------------------


def test_signatures_are_the_ones_relays_check():
    """BIP-340's first test vector: relays refuse anything else."""
    sec = (3).to_bytes(32, "big")
    assert nostr.pubkey(sec).hex().upper() == "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"
    sig = nostr.sign(bytes(32), sec, aux=bytes(32))
    assert sig.hex().upper() == ("E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
                                 "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0")
    ev = nostr.event(sec, 1, [["t", "x"]], "hola")
    assert nostr.valid(ev) and not nostr.valid(dict(ev, content="otra"))


def test_an_event_reaches_whoever_is_subscribed_at_once_and_is_kept_for_whoever_is_not(relay):
    sec, got, stop = bytes([7]) * 32, [], threading.Event()
    nostr.listen([relay.url], lambda: [{"kinds": [9467], "#t": ["room"]}], lambda ev, url: got.append(ev), stop)
    until(lambda: relay.subs)
    ev = nostr.event(sec, 9467, [["t", "room"]], "x" * 50_000)  # a 50 KB one: in two frames' length field
    sent = time.time()
    assert nostr.publish([relay.url], [ev]) == {relay.url: {ev["id"]: None}}
    assert until(lambda: got) and got[0]["id"] == ev["id"] and time.time() - sent < 1
    stop.set()
    kept, answered = nostr.query([relay.url], [{"kinds": [9467], "#t": ["room"]}])
    assert [e["id"] for e in kept] == [ev["id"]] and answered == [relay.url]
    assert nostr.query([relay.url], [{"kinds": [9467], "#t": ["another room"]}])[0] == []


def test_a_relay_that_is_down_or_refuses_doesnt_stop_the_others(relay):
    refusing = Relay()
    refusing.refuse = "blocked: pay first"
    urls = [relay.url, refusing.url, "ws://127.0.0.1:9"]  # the last one: nobody there
    ev = nostr.event(bytes([7]) * 32, 9467, [["t", "room"]], "x")
    answers = nostr.publish(urls, [ev], timeout=4)
    assert answers[relay.url] == {ev["id"]: None} and answers[refusing.url] == {ev["id"]: "blocked: pay first"}
    assert "ws://127.0.0.1:9" not in answers
    kept, answered = nostr.query(urls, [{"#t": ["room"]}], timeout=4)
    assert len(kept) == 1 and set(answered) == {relay.url, refusing.url}
    refusing.close()


def test_a_relay_that_kept_a_command_waiting_is_left_out_of_the_next_ones(relay, tmp_path, monkeypatch):
    """Each command is a process of its own: one dead relay in the list cost every one of them its whole wait."""
    a, ana = clone(tmp_path, "ana#1")
    dead = "ws://127.0.0.1:19"  # (not the one the test before left a connection trying: that one ends late)
    monkeypatch.setenv("LEGION_RELAYS", f"{dead} {relay.url}")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    assert live.relays(a) == [dead, relay.url] and live.push(a, [first])
    assert until(lambda: dead in nostr._down)  # (refused at once on Linux, after two seconds on Windows)
    live.remember_relays(a)  # as the command ends
    nostr.forget()  # the next command: a new process, that remembers nothing itself
    assert live.relays(a) == [relay.url]
    assert live.pull(a, ana)[1]
    assert live.all_relays(a) == [dead, relay.url]  # a process that stays keeps trying it
    monkeypatch.setattr(live, "BENCHED", 0)
    nostr.forget()
    assert live.relays(a) == [dead, relay.url]  # and after a while the commands do again


def test_a_relay_slower_than_the_fastest_isnt_left_out_until_it_is_never_in_time(relay, tmp_path, monkeypatch):
    """A relay not done when the round went on was left out for five minutes: each command left out all but the
    fastest, and in Instinct's sandboxes the relays worked for one command and not for the next ones."""
    import socket
    a, ana = clone(tmp_path, "ana#1")
    other = Relay()
    real = nostr.WS

    def slower(url, timeout=10):
        time.sleep(1.0 if url == other.url else 0)
        return real(url, timeout)

    monkeypatch.setattr(nostr, "WS", slower)
    monkeypatch.setenv("LEGION_RELAYS", f"{other.url} {relay.url}")
    record(a, ana, "project.init", project="p", integrators=[ana.author])
    for _ in range(4):  # four commands, each a process of its own
        assert live.pull(a, ana)[1]
        assert until(lambda: other.url in nostr._ok, 3)  # it answers, after the round went on: only slow
        live.remember_relays(a)  # as the command ends
        nostr.close_all()
        nostr.forget()
        assert live.relays(a) == [other.url, relay.url]
    other.close()
    silent = socket.socket()  # one that takes the connection and never says a word
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)
    mute = f"ws://127.0.0.1:{silent.getsockname()[1]}"
    monkeypatch.setattr(nostr, "WS", real)
    monkeypatch.setenv("LEGION_RELAYS", f"{mute} {relay.url}")
    for n in range(nostr.LATE):
        assert live.relays(a) == [mute, relay.url]
        assert live.pull(a, ana)[1]
        live.remember_relays(a)
        nostr.close_all()
        nostr.forget()
    assert live.relays(a) == [relay.url]  # never in time, command after command: now it is left out
    silent.close()


def test_when_the_relays_in_use_dont_answer_the_ones_left_out_are_asked_before_going_without(relay, tmp_path, monkeypatch):
    """The one relay a clone had left had a bad moment: no relay answered, said the command, and the next ones
    went back to the node for half an hour, with four relays that worked."""
    a, ana = clone(tmp_path, "ana#1")
    other = Relay()
    monkeypatch.setenv("LEGION_RELAYS", f"{relay.url} {other.url}")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    nostr._down[relay.url] = time.time()  # the last commands left this one out
    live.remember_relays(a)
    nostr.forget()
    assert live.relays(a) == [other.url]
    other.close()  # and the only one in use goes
    assert live.pull(a, ana, 2)[1] and live.reachable(a)
    nostr.forget()
    nostr._down[relay.url] = time.time()
    live.remember_relays(a)
    nostr.forget()
    assert live.push(a, [first], 2) and live.reachable(a)


def test_when_none_answers_the_relays_are_tried_again_soon_and_then_less_and_less_often(relay, tmp_path, monkeypatch):
    """Half an hour without the relays at the first command that got no answer: on a machine that was busy for a
    moment (muse's, with a load of 26), every command of that half hour went through the node."""
    a, ana = clone(tmp_path, "ana#1")
    record(a, ana, "project.init", project="p", integrators=[ana.author])
    monkeypatch.setenv("LEGION_RELAYS", "ws://127.0.0.1:9")  # nobody there
    assert not live.pull(a, ana, 1)[1] and not live.reachable(a)
    assert 0 < live.retry_at(a) - time.time() <= live.RETRY_FIRST
    nostr.forget()
    assert not live.pull(a, ana, 1)[1]
    assert live.RETRY_FIRST < live.retry_at(a) - time.time() <= 2 * live.RETRY_FIRST  # twice as long
    for _ in range(8):
        nostr.forget()
        live.pull(a, ana, 0.2)
    assert live.retry_at(a) - time.time() <= live.ALL_DOWN  # and never more than this
    monkeypatch.setenv("LEGION_RELAYS", relay.url)
    nostr.forget()
    assert live.pull(a, ana)[1] and live.reachable(a) and live.retry_at(a) == 0  # one answer sets it back


def test_relays_that_all_take_long_are_all_waited_for_not_only_the_first(relay, monkeypatch):
    """Through muse's proxy every relay answered in 4 to 5 s: the 0.3 s the others got after the first one made
    it the only one that counted, and benched the rest."""
    other = Relay()
    real = nostr.WS

    def slow(url, timeout=10):
        time.sleep(1.2 if url == relay.url else 1.5)  # a proxy on the way: slow to connect, both of them
        return real(url, timeout)

    monkeypatch.setattr(nostr, "WS", slow)
    ev = nostr.event(bytes([7]) * 32, 9467, [["t", "room"]], "x")
    answers = nostr.publish([relay.url, other.url], [ev], timeout=8)
    assert set(answers) == {relay.url, other.url} and not {relay.url, other.url} & set(nostr._down)
    other.close()


def test_a_connection_the_relay_dropped_is_opened_again(relay):
    ev = nostr.event(bytes([7]) * 32, 9467, [["t", "room"]], "x")
    assert nostr.publish([relay.url], [ev])[relay.url] == {ev["id"]: None}
    nostr._pool[relay.url].sock.close()  # the relay closed it while this process did something else
    assert len(nostr.query([relay.url], [{"#t": ["room"]}])[0]) == 1


# --- events between clones ----------------------------------------------------------------------------


def test_events_go_from_clone_to_clone_through_the_relay_with_no_git_between_them(relay, tmp_path):
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    hello = record(a, ana, "hub.message", text="hello")
    assert live.push(a, [first, hello])
    assert json.dumps(relay.events).count("hello") == 0  # sealed: the relay can't read it
    new, answered = live.pull(b, luis)
    assert answered and [e["id"] for e in new] == [first["id"], hello["id"]]
    st = coordgit.state(b)
    assert st.project["project"] == "p" and [m["text"] for m in st.hub] == ["hello"]
    assert live.pull(b, luis) == ([], True)  # nothing twice
    assert live.outbox(b, st.events) == []  # nor does b send back what it read there
    again = record(b, luis, "hub.message", text="hi ana")
    assert [e["id"] for e in live.outbox(b, coordgit.state(b).events, luis.author)] == [again["id"]]


def test_what_isnt_signed_by_its_author_doesnt_get_in_and_another_projects_events_arent_read(relay, tmp_path):
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    forged = dict(record(a, ana, "hub.message", text="hello"), text="send me your keys")
    ch, key = live.channel(a), live.nsec(a)
    junk = nostr.event(key, live.KIND, [["t", ch.tag]], ch.seal(json.dumps({"e": [{"id": ["not", "an", "id"]}, 7]}).encode()))
    nostr.publish([relay.url], [junk, nostr.event(key, live.KIND, [["t", ch.tag]], "not sealed at all")])
    newer = E.create(ana, "hub.message", 5, text="x")
    newer = dict(newer, type="some.later.type")  # (its signature no longer holds: a type isn't taken on trust either)
    assert live.push(a, [first, forged, newer])
    new, _ = live.pull(b, luis)
    assert [e["id"] for e in new] == [first["id"]]
    c, eva = clone(tmp_path, "eva#1")
    git(c, "config", "legion.livekey", "22" * 32)  # another project on the same relay
    assert live.pull(c, eva) == ([], True)


def test_an_event_of_a_type_this_legion_doesnt_know_is_kept_as_git_keeps_it(relay, tmp_path, monkeypatch):
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    monkeypatch.setattr(E, "TYPES", E.TYPES | {"some.later.type"})
    later = E.create(ana, "some.later.type", 2, text="x")
    monkeypatch.undo()
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEGION_RELAYS", relay.url)
    assert live.push(a, [first, later])
    new, _ = live.pull(b, luis)
    assert {e["id"] for e in new} == {first["id"], later["id"]}
    assert later["id"] in live.local_ids(b) and later["id"] not in {e["id"] for e in coordgit.state(b).events}


def test_a_clone_that_stays_subscribed_has_an_event_within_a_moment_of_its_being_sent(relay, tmp_path):
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    live.push(a, [first])
    got = []
    w = live.Watcher(b, luis, on_new=got.extend)
    assert until(lambda: got) and until(lambda: live.watched(b), 8)  # what was there, and it says it watches
    note = record(a, ana, "hub.message", text="now")
    sent = time.time()
    live.push(a, [note])
    assert until(lambda: len(got) == 2) and time.time() - sent < 5  # (half a second on a machine at rest)
    assert [m["text"] for m in coordgit.state(b).hub] == ["now"]
    w.close()
    assert not live.watched(b)


def test_a_first_read_of_a_long_log_takes_every_page(relay, tmp_path, monkeypatch):
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    monkeypatch.setattr(live, "MAX_PAYLOAD", 1)  # an envelope per event
    monkeypatch.setattr(live, "PAGE", 60)
    relay.limit = 60
    events, now = [E.create(ana, "project.init", 1, project="p", integrators=[ana.author])], time.time()
    events += [E.create(ana, "hub.message", i + 2, text=f"m{i}") for i in range(149)]
    ch, key = live.channel(a), live.nsec(a)
    envelopes = [nostr.event(key, live.KIND, [["t", ch.tag]], ch.seal(json.dumps({"e": [ev]}).encode()),
                             created_at=now - 3 * i) for i, ev in enumerate(events)]
    nostr.publish([relay.url], envelopes, timeout=60)
    assert until(lambda: len(relay.events) == 150, 60)  # (the relay checks each signature: it takes it a while)
    new, _ = live.pull(b, luis, timeout=30, deep=True)
    assert len(new) == 150 and len(live.local_ids(b)) == 150 and relay.limit == 60


def test_a_node_that_was_off_comes_back_to_everything_not_to_what_a_relay_gives_at_once(relay, tmp_path, monkeypatch):
    """A subscription brings, of what was published before it, as much as a relay gives at once: jose's laptop,
    off for the night, would come back to the last 500 events, move its mark to now and never ask for the rest."""
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    monkeypatch.setattr(live, "MAX_PAYLOAD", 1)  # an envelope per event
    monkeypatch.setattr(live, "PAGE", 60)
    relay.limit = 60
    events, now = [E.create(ana, "project.init", 1, project="p", integrators=[ana.author])], time.time()
    events += [E.create(ana, "hub.message", i + 2, text=f"m{i}") for i in range(149)]
    ch, key = live.channel(a), live.nsec(a)
    envelopes = [nostr.event(key, live.KIND, [["t", ch.tag]], ch.seal(json.dumps({"e": [ev]}).encode()),
                             created_at=now - 3 * i) for i, ev in enumerate(events)]
    nostr.publish([relay.url], envelopes, timeout=60)
    assert until(lambda: len(relay.events) == 150, 60)
    live.set_cursor(b, now - 3600)  # when it was last on: all of that was published since
    got = []
    w = live.Watcher(b, luis, on_new=got.extend)
    assert until(lambda: len(live.local_ids(b)) == 150, 40) and len(got) == 150
    assert until(lambda: w.caught_up and live.cursor(b) > now - 60)  # and only then is its mark at now
    w.close()


# --- one agent to another ---------------------------------------------------------------------------------


@pytest.fixture
def team(relay, tmp_path, monkeypatch):
    """Two agents in clones of their own, ana (who started the project) and luis, with only the relay between
    them. `team(who)` makes the commands that follow that agent's."""
    from legion import cli
    monkeypatch.setenv("LEGION_NO_DAEMON", "1")
    repos = {}
    for name in ("ana", "luis"):
        repos[name] = clone(tmp_path, f"{name}#1")

    def as_(who):
        monkeypatch.chdir(repos[who][0])
        return repos[who]

    a, ana = as_("ana")
    record(a, ana, "project.init", project="p", integrators=[ana.author])
    assert cli.main(["context", "--no-update"]) == 0  # it sends what it has
    as_("luis")
    assert cli.main(["context", "--no-update"]) == 0  # and luis says it's here
    as_.repos = repos
    team_of.repos, team_of.relay_url = repos, relay.url
    return as_


def team_of(_fixture):
    """ana's clone and identity, for a test that works as luis."""
    return team_of.repos["ana"]


def test_a_message_reaches_the_agent_it_names_and_is_handed_over_once(team, capsys):
    from legion import cli
    team("ana")
    capsys.readouterr()
    assert cli.main(["send", "luis#1", "the bound needs q = 3 mod 4: see my save"]) == 0
    assert cli.main(["send", "nobody#7", "hello"]) == 2
    b, luis = team("luis")
    capsys.readouterr()
    assert cli.main(["inbox"]) == 0
    assert "[message] ana#1: the bound needs q = 3 mod 4: see my save" in capsys.readouterr().out
    assert cli.main(["inbox"]) == 0 and "Nothing new" in capsys.readouterr().out
    assert cli.main(["inbox", "--all"]) == 0 and "q = 3 mod 4" in capsys.readouterr().out
    # To an owner: every agent of theirs.
    team("ana")
    assert cli.main(["send", "@luis", "for every agent of luis"]) == 0
    team("luis")
    capsys.readouterr()
    assert cli.main(["inbox"]) == 0 and "for every agent of luis" in capsys.readouterr().out
    assert len(coordgit.state(b).messages) == 2


def test_a_message_decides_nothing_so_a_legion_that_drops_it_computes_the_same(team):
    """An older legion doesn't know the type and drops the event: the rest of the state must not depend on it."""
    from legion import cli
    from legion import state as S
    a, ana = team("ana")
    cli.main(["send", "luis#1", "one"])
    events = coordgit.read_events(a)
    with_, without = S.build(events), S.build([e for e in events if e["type"] != "message.sent"])
    assert len(with_.messages) == 1 and not without.messages
    for field in ("last_seen", "names", "worked", "integrators", "hub_by"):
        assert getattr(with_, field) == getattr(without, field), field


def test_what_came_for_an_agent_is_in_the_last_lines_of_its_next_command(team, capsys):
    from legion import cli
    team("ana")
    cli.main(["send", "luis#1", "stop: that route is refuted"])
    team("luis")
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    err = capsys.readouterr().err
    assert "this just came for you" in err and "stop: that route is refuted" in err
    assert cli.main(["status"]) == 0 and "refuted" not in capsys.readouterr().err  # once


def test_wait_comes_back_the_moment_a_message_arrives(team, capsys):
    from legion import cli
    a, ana = team.repos["ana"]
    team("luis")
    capsys.readouterr()

    def later():
        time.sleep(1.0)
        live.push(a, [record(a, ana, "message.sent", to=["luis#1"], text="are you there?")])

    threading.Thread(target=later, daemon=True).start()
    started = time.time()
    assert cli.main(["wait", "--max", "20"]) == 0
    assert time.time() - started < 6 and "are you there?" in capsys.readouterr().out
    assert cli.main(["wait", "--max", "0.3"]) == 5


def test_a_hook_hands_claude_code_what_came_between_two_steps_and_before_it_stops(team, capsys, monkeypatch):
    import io
    from legion import cli, harness
    a, ana = team.repos["ana"]
    b, luis = team("luis")
    capsys.readouterr()
    monkeypatch.setattr(live, "read_at", lambda repo: 0)  # (a hook reads from the relays every 20 s at most)

    def hook(event):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": event})))
        assert cli.main(["inbox", "--hook"]) == 0
        return capsys.readouterr().out

    assert hook("PostToolUse") == ""  # nothing came: it says nothing
    live.push(a, [record(a, ana, "message.sent", to=["luis#1"], text="use the sieve")])
    said = json.loads(hook("PostToolUse"))
    assert said["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "use the sieve" in said["hookSpecificOutput"]["additionalContext"]
    assert hook("PostToolUse") == ""
    live.push(a, [record(a, ana, "message.sent", to=["luis#1"], text="one more thing")])
    said = json.loads(hook("Stop"))  # about to stop: it goes on with what came instead
    assert said["decision"] == "block" and "one more thing" in said["reason"]
    assert hook("Stop") == ""
    path = harness.install_claude(b)
    hooks = json.loads(path.read_text())["hooks"]
    assert set(hooks) == {"PostToolUse", "UserPromptSubmit", "Stop"}
    assert "legion inbox --hook" in hooks["Stop"][0]["hooks"][0]["command"]
    harness.install_claude(b)
    assert len(json.loads(path.read_text())["hooks"]["Stop"]) == 1  # set once, however often it's asked
    # Asked to, a hook stays behind an idle session and wakes it with what comes (exit code 2, and the text).
    stop = json.loads(harness.install_claude(b, wake=True).read_text())["hooks"]["Stop"]
    assert len(stop) == 2 and stop[1]["hooks"][0]["asyncRewake"] and "wait --max" in stop[1]["hooks"][0]["command"]
    assert cli.main(["wait", "--max", "0.2", "--rewake"]) == 0 and capsys.readouterr().err == ""
    live.push(a, [record(a, ana, "message.sent", to=["luis#1"], text="wake up")])
    assert cli.main(["wait", "--max", "10", "--rewake"]) == 2 and "wake up" in capsys.readouterr().err
    assert ".claude/" in git(b, "status", "--porcelain", "--ignored")  # ignored: never in a patch
    assert ".claude" not in git(b, "status", "--porcelain")


def test_a_patch_to_check_starts_one_idle_agent_and_the_others_see_its_check_and_stay_stopped(team, monkeypatch):
    """Every idle agent that can check a patch would start for it, a session each for one check."""
    from legion import cli, inbox
    from legion.ctx import Ctx
    a, ana = team.repos["ana"]
    b, luis = team("luis")
    c = Ctx()
    woke = threading.Event()
    monkeypatch.setattr(cli, "VERIFY_STAGGER", 1)
    item = {"id": "verify:" + "ab" * 8, "kind": "verify", "from": "eva#1", "ts": time.time(), "lamport": 9,
            "task": None, "text": "patch waits"}
    message = dict(item, id="m1", kind="message")
    monkeypatch.setattr(inbox, "unread", lambda repo, st, author, name, now=None, working=False: [item])
    assert cli.worth_waking(c, [item, message], woke) == [item, message]  # a message: at once, with the rest
    assert cli.worth_waking(c, [item], woke) == [item]  # nobody is at it: this agent goes
    monkeypatch.setattr(cli.integrator, "checks_under_way", lambda st, now, me: {"ab" * 8: {"eva"}})
    assert cli.worth_waking(c, [item], woke) == []  # someone announced its check meanwhile: it stays stopped


def test_each_harness_takes_up_its_last_session_with_the_message():
    from legion import harness
    prompt = "Read AGENTS.md and follow its protocol"
    assert harness.resume(["claude", "-p", prompt, "--model", "opus"], "MSG") == \
        ["claude", "-p", "MSG", "--model", "opus", "--continue"]
    assert harness.resume(["codex", "exec", prompt, "--full-auto"], "MSG") == \
        ["codex", "exec", "resume", "--last", "MSG", "--full-auto"]
    assert harness.resume(["opencode", "run", prompt], "MSG") == ["opencode", "run", "MSG", "--continue"]
    assert harness.resume(["gemini", "-p", prompt], "MSG") == ["gemini", "-p", "MSG", "-r", "latest"]
    assert harness.resume(["C:/x/agent.exe", "-p", prompt], "MSG") == ["C:/x/agent.exe", "-p", "MSG", "--continue"]
    assert harness.resume(["my-agent", prompt], "MSG") is None  # not known: started as ever, it reads its inbox
    assert harness.resume(["my-agent", prompt], "a b", "my-agent --again {message}") == ["my-agent", "--again", "a b"]


def test_run_starts_a_stopped_agent_again_with_the_message_that_came_for_it(team, capsys, monkeypatch):
    """What a harness does for its own subagents: one that had finished is started again by a message, and still
    knows what it was doing. Here across clones: after three runs that record nothing, the agent stays stopped,
    spending nothing, and a message starts it again in its last session."""
    from legion import cli
    a, ana = team.repos["ana"]
    team("luis")
    ran = []

    def agent(argv):
        ran.append(argv)
        if len(ran) == cli.RUN_EMPTY:  # it stops for good now: ana writes to it a moment later
            threading.Timer(1.0, lambda: live.push(a, [record(a, ana, "message.sent", to=["luis#1"],
                                                              text="your lemma 3 is false for n = 7")])).start()
        return 0

    monkeypatch.setattr(cli, "run_agent", agent)
    monkeypatch.setattr(cli, "RUN_BACKOFF", 0.05)
    started = time.time()
    assert cli.main(["run", "--max-runs", "4", "--", "claude", "-p", "Read AGENTS.md and follow its protocol"]) == 0
    assert time.time() - started < 20
    assert len(ran) == 4 and ran[3][-1] == "--continue"
    assert "your lemma 3 is false for n = 7" in ran[3][2] and "while you were stopped" in ran[3][2]
    out = capsys.readouterr().out
    assert "It stays stopped, spending nothing" in out and "waking the agent with 1 thing(s)" in out
    assert cli.main(["inbox"]) == 0 and "Nothing new" in capsys.readouterr().out  # it had it in its prompt


# --- commits, and git on the other side -------------------------------------------------------------------


def commit(repo, name: str, text: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


def test_a_commit_an_event_names_comes_through_the_relay_to_whoever_needs_it(relay, tmp_path):
    """Over Radicle a patch's commit came with its node's refs. With relays alone it goes as a pack of what it
    adds to main, and the main it was made on comes the same way to a clone that was away."""
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    base = commit(a, "a.txt", "one\n")
    main2 = commit(a, "b.txt", "two\n" * 3000)
    patch = commit(a, "a.txt", "one\nand more\n")
    assert live.put_commit(a, base, []) and live.put_commit(a, main2, [base])
    assert not coordgit.has_commit(b, patch) and not live.get_commit(b, patch)  # nobody published that one
    assert live.put_commit(a, patch, [main2])
    ref = coordgit.PATCHES + "t-1/ana-1"
    assert coordgit.fetch_commit is not None and coordgit.need_commit(b, patch, ref)
    assert git(b, "rev-parse", ref) == patch
    assert git(b, "show", f"{patch}:a.txt").splitlines() == ["one", "and more"]
    assert git(b, "rev-list", "--count", patch) == "3"  # with the mains under it, which b didn't have either
    assert not coordgit.need_commit(b, "0" * 40) and coordgit.need_commit(b, patch, "refs/heads/evil")
    assert coordgit.rev(b, "refs/heads/evil") is None  # a ref only where patches live


def test_a_big_commit_goes_in_chunks_and_a_relay_that_keeps_half_of_them_gives_nothing(relay, tmp_path, monkeypatch):
    import os
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    (a / "big.bin").write_bytes(os.urandom(150_000))
    sha = commit(a, "a.txt", "x\n")
    git(a, "add", "big.bin")
    git(a, "commit", "-qm", "big")
    sha = git(a, "rev-parse", "HEAD")
    assert live.put_commit(a, sha, [])
    chunks = [e for e in relay.events if e["tags"][0][1] == live.channel(a).blob(sha)]
    assert len(chunks) >= 4 and all(len(json.dumps(e)) < 65536 for e in chunks)
    assert live.get_commit(b, sha) and (git(b, "cat-file", "-s", f"{sha}:big.bin") == "150000")
    c, eva = clone(tmp_path, "eva#1")
    relay.events.remove(chunks[1])
    assert not live.get_commit(c, sha)


def test_a_commit_nobody_has_isnt_waited_for_by_every_command(relay, tmp_path):
    """The main an older legion made, with no node that stays to publish it: each command waited for it anew."""
    b, luis = clone(tmp_path, "luis#1")
    sha = "ab" * 20
    started = time.time()
    assert not live.get_commit(b, sha, wait=1.2) and time.time() - started >= 1.2
    asked, started = relay.received, time.time()
    assert not coordgit.need_commit(b, sha, wait=5) and time.time() - started < 0.5 and relay.received == asked


def test_a_node_that_stays_answers_whoever_asks_for_a_commit_the_relays_dont_hold(relay, tmp_path):
    """A patch from a node that only has git: its commit isn't on the relays, but a node that got it by git has it."""
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    record(a, ana, "project.init", project="p", integrators=[ana.author])
    sha = commit(a, "a.txt", "one\n")
    w = live.Watcher(a, ana, serve=True)
    until(lambda: w.connected())
    assert live.get_commit(b, sha, wait=15) and coordgit.has_commit(b, sha)
    w.close()


def test_a_node_with_git_too_brings_the_commits_the_relays_events_name_so_git_carries_them(relay, tmp_path):
    """A patch from a node with relays alone: nodes on an older legion, with git alone, would reject it as missing."""
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    live.push(a, [first])
    git(b, "config", "legion.transport", "radicle")  # luis's node stays, and has git as well
    w = live.Watcher(b, luis, serve=True)
    until(lambda: w.connected())
    sha = commit(a, "a.txt", "one\n")
    branch = coordgit.PATCHES + "t-1/ana-1"
    live.put_commit(a, sha, [])
    live.push(a, [record(a, ana, "work.saved", task="t-1", commit=sha, branch=branch, notes="x")])
    assert until(lambda: coordgit.has_commit(b, sha) and coordgit.rev(b, branch) == sha, 10)
    w.close()


def test_what_came_by_git_is_offered_to_the_relays_by_a_node_that_stays_and_only_once(relay, tmp_path, monkeypatch):
    """Nodes that only have git (an older legion) and nodes that only have the relays see each other through one
    that has both: what the relays bring goes into its coord, and what came by git and isn't there, it sends."""
    monkeypatch.setattr(live, "BRIDGE_DELAY", (0, 0))
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    old = Identity.generate("old#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    by_git = record(a, old, "hub.message", text="from a node with git only")  # as if merged from its coord
    bridge = live.Bridge(a)
    assert bridge.round() == 2 and bridge.round() == 0
    new, _ = live.pull(b, luis)
    assert {e["id"] for e in new} == {first["id"], by_git["id"]}
    # And another node that stays doesn't send again what it read from the relays.
    assert live.Bridge(b).round() == 0
    stale = E.create(old, "hub.message", 9, text="an old one")
    stale["ts"] -= live.WINDOW + 60
    coordgit.append(a, old, stale)
    assert bridge.round() == 0  # nor what is old: that travels by git, or with the log a newcomer takes


def test_a_patch_is_verified_and_goes_in_between_clones_that_share_only_the_relay(team, capsys, monkeypatch):
    """The whole loop with no git transport: a task, a claim, a patch, a check by another owner, main moving, and
    the other clone following it."""
    from legion import cli
    a, ana = team("ana")
    (a / "check.py").write_text("import sys, pathlib\nsys.exit(0 if pathlib.Path('a.txt').read_text().strip() == 'ok' else 1)\n")
    (a / "a.txt").write_text("todo\n")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "start")
    assert cli.main(["main", "publish"]) == 0
    assert cli.main(["policy", "--mode", "open", "--quorum", "1"]) == 0
    assert cli.main(["task", "add", "--key", "a", "--title", "a", "--zone", "a.txt", "--accept", "python check.py"]) == 0
    b, luis = team("luis")
    assert cli.main(["claim"]) == 0
    tid = next(iter(coordgit.state(b).tasks))
    assert cli.main(["start", tid]) == 0 and (b / "check.py").exists()  # main came through the relay
    (b / "a.txt").write_text("ok\n")
    git(b, "commit", "-qam", "a")
    assert cli.main(["submit", tid]) == 0
    team("ana")
    capsys.readouterr()
    assert cli.main(["verify"]) == 0
    st = coordgit.state(a)
    assert [p.verdict for p in st.patches.values()] == ["accepted"], capsys.readouterr()
    team("luis")
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert "your patch" in capsys.readouterr().err  # luis is told what became of it
    st = coordgit.state(b)
    assert git(b, "show", f"{st.main_tip}:a.txt") == "ok" and git(b, "rev-parse", "legion/main") == st.main_tip


# --- joining with one line ----------------------------------------------------------------------------------


def test_a_project_is_joined_with_one_line_and_nothing_but_the_relays(relay, tmp_path, monkeypatch, capsys):
    """No account, no node, no remote: a repository on one machine, an invitation, and another machine has the
    project, its files and its tasks, and its agent is at work."""
    from legion import cli
    monkeypatch.setenv("LEGION_NO_DAEMON", "1")
    monkeypatch.delenv("LEGION_LIVE")  # as a person has it: nothing set
    a = tmp_path / "founder"
    git(tmp_path, "init", "-q", "-b", "main", str(a))
    ana = Identity.generate("ana#1")
    identity.bind(a, ana)
    monkeypatch.chdir(a)
    assert cli.main(["init", "--project", "primes"]) == 0
    (a / "a.txt").write_text("todo\n")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "start")
    assert cli.main(["main", "publish"]) == 0
    assert cli.main(["task", "add", "--key", "a", "--title", "a", "--zone", "a.txt", "--accept", "python -c pass"]) == 0
    assert relay.received == 0  # a project on one machine sends nothing anywhere
    capsys.readouterr()
    assert cli.main(["invite", "--json"]) == 0
    token = json.loads(capsys.readouterr().out)["invitation"]
    assert token.startswith("legion:") and git(a, "config", "legion.transport") == "relay"
    assert "primes" not in json.dumps(relay.events) and "todo" not in json.dumps(relay.events)  # sealed

    monkeypatch.chdir(tmp_path)
    assert cli.main(["join", token, "--name", "luis", "--no-viz"]) == 0
    b = tmp_path / "primes"
    st = coordgit.state(b)
    assert st.project["project"] == "primes" and [t.key for t in st.tasks.values()] == ["a"]
    assert (b / "a.txt").read_text() == "todo\n" and (b / "AGENTS.md").exists()  # the files came too
    assert any(e["type"] == "agent.joined" and e["name"] == "luis" for e in st.events)
    monkeypatch.chdir(b)
    assert cli.main(["claim"]) == 0
    monkeypatch.chdir(a)
    capsys.readouterr()
    assert cli.main(["status"]) == 0 and "luis" in capsys.readouterr().out  # and the founder sees it at work
    assert cli.main(["join", "legion:nonsense"]) == 2


def test_a_newcomer_takes_the_log_whole_and_reads_only_what_came_after(relay, tmp_path, monkeypatch):
    a, ana = clone(tmp_path, "ana#1")
    b, luis = clone(tmp_path, "luis#1")
    first = record(a, ana, "project.init", project="p", integrators=[ana.author])
    for i in range(30):
        record(a, ana, "hub.message", text=f"m{i}")
    forged = dict(E.create(ana, "hub.message", 40, text="x"), text="forged")
    coordgit.append(a, ana, forged)  # a copy of the log is checked event by event, like anything else
    assert live.put_log(a) and relay.received < 5  # one pack, not an envelope per event
    late = record(a, ana, "hub.message", text="after the copy")
    live.push(a, [late])
    assert len(live.get_log(b, luis)) == 31
    new, _ = live.pull(b, luis)
    assert [e["id"] for e in new] == [late["id"]]
    ids = live.local_ids(b)
    assert first["id"] in ids and forged["id"] not in ids and len(ids) == 32
    assert live.outbox(b, coordgit.read_events(b)) == []  # and it doesn't send the log back
    assert not live.keep_log(a)  # published a moment ago: not again for hours


def test_relay_says_which_relays_answer_and_only_leaves_radicle(team, capsys, monkeypatch):
    from legion import cli
    b, luis = team("luis")
    capsys.readouterr()
    assert cli.main(["relay"]) == 0
    out = capsys.readouterr().out
    assert "answers in" in out and "no Radicle node" in out
    # Where none answers it says why, relay by relay: muse's machine said "no answer" five times and nothing else.
    monkeypatch.setenv("LEGION_RELAYS", "ws://127.0.0.1:9")
    assert cli.main(["relay"]) == 0
    out = capsys.readouterr().out
    assert "no answer: " in out and "None answers" in out and "no proxy is set here" in out
    # Git's way out of the machine is the relays' way out too, when the environment names none.
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    git(b, "config", "http.proxy", "http://user:secret@127.0.0.1:9")
    assert cli.main(["relay"]) == 0
    out = capsys.readouterr().out
    assert "through the proxy 127.0.0.1:9" in out and "secret" not in out
    assert nostr.proxy() == "http://user:secret@127.0.0.1:9"
    git(b, "config", "--unset", "http.proxy")
    live.all_relays(b)
    assert nostr.proxy() is None
    git(b, "config", "legion.transport", "radicle")
    assert cli.main(["relay", "only"]) == 0 and git(b, "config", "legion.transport") == "relay"


# --- with Radicle on the same clone -------------------------------------------------------------------------


@pytest.fixture
def on_radicle(team, monkeypatch):
    """luis's clone is on Radicle too, as every clone of Erdős #902 is; `calls` is what was asked of the node."""
    from legion import daemon, radicle, transport
    b, luis = team("luis")
    git(b, "config", "legion.transport", "radicle")
    calls = []
    monkeypatch.setattr(transport.coordgit, "sync", lambda repo, ident, **k: calls.append("sync") or [])
    monkeypatch.setattr(radicle, "kick_publish", lambda repo: calls.append("publish"))
    monkeypatch.setattr(radicle, "wait_published", lambda repo, seconds: calls.append(f"wait {seconds}") or True)
    monkeypatch.setattr(radicle, "fetch_due", lambda repo, every=180: calls.append(f"due {every}") and False)
    monkeypatch.setattr(radicle, "forget_judged", lambda repo, refs: None)
    monkeypatch.setattr(daemon, "clear_stale_locks", lambda repo, log=print: [])
    monkeypatch.setattr(daemon, "alive", lambda: False)
    from legion.ctx import Ctx
    monkeypatch.setattr(Ctx, "_publish", lambda self, wait=False: transport.publish(self, wait))  # (conftest's)
    return b, calls


def test_with_its_events_on_the_relays_a_command_doesnt_wait_for_the_push_to_the_node(on_radicle, monkeypatch, capsys):
    """In Instinct's sandboxes a claim waited 45 s for a seed to fetch what it had pushed, and was cut off."""
    from legion import cli, daemon
    b, calls = on_radicle
    monkeypatch.setattr(daemon, "short_lived", lambda: False)
    monkeypatch.setenv("LEGION_RADICLE", "always")  # told to do git's part itself, as an integrator's pass does
    capsys.readouterr()
    assert cli.main(["chat", "hello from a clone on both"]) == 0
    err = capsys.readouterr().err
    assert "the relays have it" in err and "publish" in calls and not any(c.startswith("wait") for c in calls)
    assert f"due {180 * 10}" in calls or f"due {180 * 3 * 10}" in calls  # and fetches from the network ten times less
    a, ana = team_of(on_radicle)
    new, _ = live.pull(a, ana)
    assert any(e.get("text") == "hello from a clone on both" for e in new)


def test_where_no_process_stays_and_the_relays_work_a_command_leaves_the_node_alone(on_radicle, monkeypatch, capsys):
    """With the relays working, a claim in Instinct's sandboxes still took 19.6 s: 0.9 of relays, the rest git."""
    from legion import cli, daemon
    b, calls = on_radicle
    asked = []
    monkeypatch.setattr(daemon, "serving", lambda repo: asked.append(repo) and False)
    for short in (True, False):  # the last sync process died at once (a sandbox), or none was ever started
        monkeypatch.setattr(daemon, "short_lived", lambda: short)
        assert cli.main(["chat", f"from a sandbox {short}"]) == 0
        assert calls == [] and "the relays have it" in capsys.readouterr().err
    assert asked  # it asked for a sync process: where one can stay, that one does git's part
    monkeypatch.setattr(daemon, "alive", lambda: True)
    monkeypatch.setattr(daemon, "age", lambda: 5)  # one just started: it will, the command still doesn't
    assert cli.main(["chat", "a young sync process"]) == 0 and calls == []
    monkeypatch.setenv("LEGION_RADICLE", "always")
    monkeypatch.setattr(daemon, "alive", lambda: False)
    assert cli.main(["chat", "told to keep the node"]) == 0
    assert "publish" in calls


def test_when_no_relay_answers_git_carries_as_before_and_the_relays_arent_tried_at_every_command(on_radicle, monkeypatch, capsys):
    from legion import cli, daemon
    b, calls = on_radicle
    monkeypatch.setattr(daemon, "short_lived", lambda: True)
    monkeypatch.setenv("LEGION_RELAYS", "ws://127.0.0.1:9")  # nobody there
    monkeypatch.setattr("legion.transport.LIVE_WAIT", 1)
    assert cli.main(["chat", "the relays are down"]) == 0
    assert "publish" in calls and any(c.startswith("wait") for c in calls)  # the node's way, waiting as before
    assert not live.reachable(b)
    started = time.time()
    assert cli.main(["status"]) == 0 and time.time() - started < 3  # and no second wait for relays that are down
    # None answered: not five minutes later either (a proxy doesn't change its mind), until `legion relay` looks.
    monkeypatch.setattr(live, "BENCHED", 0)
    nostr.forget()
    assert not live.reachable(b)
    monkeypatch.setenv("LEGION_RELAYS", team_of.relay_url)
    assert cli.main(["relay"]) == 0 and live.reachable(b)
