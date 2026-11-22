import io
import json
import sys
import time

import pytest

from legion import budget, cli, coordgit, harness
from test_queue import as_, make_project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LEGION_RUN", raising=False)
    return make_project(tmp_path, monkeypatch)


def claude_says(used5, used7=10.0, reset5=None, now=None):
    now = now or time.time()
    budget.record_claude({"rate_limits": {
        "five_hour": {"used_percentage": used5, "resets_at": reset5 or now + 3600},
        "seven_day": {"used_percentage": used7, "resets_at": now + 5 * 86400}}})


def test_the_question_depends_on_the_agent(monkeypatch):
    """Claude and Codex count use in windows and show it; Gemini counts requests a day and doesn't; an API key
    pays per token. Each person is asked what their agent's limits are made of."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    q, how = budget.question("claude")
    assert "5-hour" in q and "weekly" in q and "--five-hour" in how and "--week" in how
    q, how = budget.question("codex")
    assert "month" in q and "--month" in how
    q, how = budget.question("gemini")
    assert "requests per day" in q and "--hours-per-day" in how
    assert "--hours-per-day" in budget.question("opencode")[1]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    q, how = budget.question("claude")
    assert "per token" in q and "--hours-per-day" in how


def test_an_agent_asks_its_person_first_and_pauses_at_the_limit(project, capsys):
    repo, tids, patch = project
    as_(repo, "luis")
    coordgit.git(repo, "config", "legion.budgetask", "true")  # what join leaves when an agent ran it
    assert cli.main(["claim"]) == budget.ASK
    said = capsys.readouterr().out
    assert "ask your person" in said and "budget set" in said
    assert cli.main(["budget", "set", "--five-hour", "80", "--week", "50", "--harness", "claude"]) == 0
    settings = json.loads((repo / ".claude" / "settings.local.json").read_text())
    assert "legion budget statusline" in settings["statusLine"]["command"]  # where Claude Code shows its use
    claude_says(85.0)
    assert cli.main(["claim"]) == budget.PAUSED
    said = capsys.readouterr().out
    assert "85% of the 5-hour window" in said and "save" in said and "end your session" in said
    assert cli.main(["budget", "check"]) == budget.PAUSED
    claude_says(85.0, reset5=time.time() - 1)  # the window reset since that reading
    assert cli.main(["budget", "check"]) == 0
    assert cli.main(["claim"]) == 0
    assert "limit is reached" not in capsys.readouterr().out


def test_a_limit_out_of_range_is_refused(project):
    with pytest.raises(SystemExit):
        cli.main(["budget", "set", "--week", "abc"])
    assert cli.main(["budget", "set", "--week", "150"]) == 2
    assert cli.main(["budget", "set"]) == 2
    assert cli.main(["budget", "set", "--no-limit"]) == 0 and budget.is_set(project[0])
    assert budget.over(project[0]) is None


def test_codex_limits_come_from_its_session_files(project):
    now = time.time()
    day = project[0].parent / "codex" / "sessions" / "2026" / "11" / "22"
    day.mkdir(parents=True)
    rl = {"primary": {"used_percent": 91.0, "window_minutes": 300, "resets_at": now + 600},
          "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": now + 86400}}
    (day / "rollout-1.jsonl").write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": None}}}) + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rl}}) + "\n")
    got = budget.read_codex(now)
    assert got["five_hour"]["used"] == 91.0 and got["seven_day"]["used"] == 40.0
    budget.save(project[0], "codex", {"five_hour": 90, "seven_day": 50})
    res = budget.over(project[0], now)
    assert [o["name"] for o in res["over"]] == ["five_hour"] and budget.resume_at(res, now) == now + 600
    assert budget.over(project[0], now + 601) is None  # past its reset: used up no more


def test_hours_a_day_are_what_legion_run_measured(project):
    repo = project[0]
    now = time.time()
    start = max(budget._midnight(now), now - 3 * 3600)
    budget.run_started(repo, start)
    assert abs(budget.hours_today(repo, now) - (now - start) / 3600) < 0.01
    budget.run_ended(repo, now)
    budget.save(repo, "gemini", {budget.HOURS: 0.001})
    res = budget.over(repo, now)
    assert res and res["over"][0]["resets_at"] == budget._midnight(now, 1)
    assert "hours a day" in budget.paused_text(res, under_run=True)


def test_claude_is_told_to_stop_mid_session_and_let_stop(project, monkeypatch, capsys):
    repo = project[0]
    as_(repo, "luis")
    budget.save(repo, "claude", {"five_hour": 80})

    def hook(event):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": event})))
        assert cli.main(["inbox", "--hook"]) == 0
        return capsys.readouterr().out

    assert hook("PostToolUse") == ""
    claude_says(95.0)
    said = json.loads(hook("PostToolUse"))["hookSpecificOutput"]["additionalContext"]
    assert "limit is reached" in said and "95%" in said
    assert hook("Stop") == ""  # not pushed on: it stops


def test_the_status_line_keeps_the_limits_and_shows_what_it_showed(project, monkeypatch, capsys):
    repo = project[0]
    config = repo.parent / "claude-config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f'"{sys.executable}" -c "print(6*7)"'}}))
    harness.install_statusline(repo)
    harness.install_statusline(repo)  # once, however often
    assert budget.load(repo)["statusline_prev"].endswith('print(6*7)"')
    payload = {"rate_limits": {"five_hour": {"used_percentage": 12.5, "resets_at": time.time() + 99}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["budget", "statusline"]) == 0
    assert capsys.readouterr().out.strip() == "42"
    assert budget.read_claude(time.time())["five_hour"]["used"] == 12.5


def test_run_stops_an_agent_over_its_limit_and_waits_for_the_reset(project, monkeypatch, capsys):
    repo = project[0]
    as_(repo, "luis")
    budget.save(repo, "claude", {"five_hour": 80})
    claude_says(99.0)
    monkeypatch.setattr(cli, "BUDGET_LOOK", 0.2)
    monkeypatch.setattr(budget, "GRACE", 0)
    monkeypatch.setitem(cli.RUN_BUDGET, "repo", repo)
    t = time.time()
    code = cli.run_agent([sys.executable, "-c", "import time; time.sleep(60)"])
    assert code != 0 and time.time() - t < 30
    assert "stopping the agent" in capsys.readouterr().out
    ran = []
    monkeypatch.setattr(cli, "run_agent", lambda argv: ran.append(argv) or 0)

    def asleep(seconds):
        raise KeyboardInterrupt  # it waits for the reset: stop the test there

    monkeypatch.setattr(cli.time, "sleep", asleep)
    assert cli.main(["run", "--", "my-agent"]) == 130 and ran == []
    assert "paused" in capsys.readouterr().out
