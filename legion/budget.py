"""How much of its person's credits an agent may spend here, and a pause, for real, when it's spent.

Each coding agent is limited its own way: Claude's and Codex's plans count use in windows (five hours, a week; some
Codex plans a month) and show what share of each is gone; Gemini CLI counts requests a day, and doesn't show them;
an API key pays per token. So the question its person is asked depends on the agent (QUESTIONS), and so does what
measures the answer (METERS):

  claude   the share of the 5-hour and weekly windows used, from what Claude Code hands its status line
           (`budget statusline`, installed in the clone's .claude/settings.local.json, records it)
  codex    the same windows, from the rate limits Codex writes in its session files (~/.codex/sessions)
  others   the hours the agent worked today, as `legion run` measures them

The answer stays in the clone (.git/legion-budget.json). Once a limit is reached: `claim` hands out no more work (exit
code 8, with when it resets), Claude Code's hooks tell the agent mid-session to save and end it and stop pushing it
on, and `legion run` stops the agent after a grace period and starts it again only when the window has reset.
"""

import json
import os
import time
from pathlib import Path

from . import coordgit
from .identity import home

# name: (minutes, what a person calls it, the `budget set` option)
WINDOWS = {"five_hour": (300, "5-hour window", "--five-hour"),
           "seven_day": (10080, "weekly limit", "--week"),
           "month": (43200, "monthly limit", "--month")}
HOURS = "hours_per_day"
PAUSED, ASK = 8, 9  # claim's exit codes: the person's limit is reached; nobody asked the person yet
GRACE = 300  # seconds `legion run` gives an agent over its limit to save before it stops it
RECHECK = 600  # seconds between looks when what's over has no known reset (a reading not refreshed yet)
STALE = 6 * 3600  # a Claude reading older than this, from a window still open, may be far behind: said so
TAIL = 512 * 1024  # bytes read from the end of a Codex session file

CLAUDE_Q = ("Claude counts your use in two windows: the 5-hour one and the weekly one. Up to what percent of each "
            "may legion's agents use? When either is reached they save their work and pause until it resets "
            "(for example: 80% of the 5 hours, 50% of the week).")
CLAUDE_KEY_Q = ("You pay Claude per token (an API key), and legion can't read what you've spent. How many hours a day "
                "may your agent work for legion? When they're used it saves its work and pauses until tomorrow.")
CODEX_Q = ("Codex counts your use in windows: on most ChatGPT plans a 5-hour one and a weekly one (on some, one "
           "a month). Up to what percent of each may legion's agents use? When one is reached they save their "
           "work and pause until it resets (for example: 80% of the 5 hours, 50% of the week).")
GEMINI_Q = ("Gemini CLI limits requests per day (and per minute), and legion can't read how many you've used. How "
            "many hours a day may your agent work for legion? When they're used it saves its work and pauses "
            "until tomorrow.")
OTHER_Q = ("legion can't read your agent's usage limits. How many hours a day may it work for legion? When "
           "they're used it saves its work and pauses until tomorrow.")


def harness_of(name: str | None) -> str:
    name = (name or "").lower()
    if name in ("claude", "codex", "gemini"):
        return name
    return name or "other"


def question(harness: str) -> tuple[str, str]:
    """What to ask this agent's person, and how the answer is recorded."""
    h = harness_of(harness)
    hours = "python -m legion budget set --hours-per-day 4"
    if h == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        return CLAUDE_Q, "python -m legion budget set --five-hour 80 --week 50"
    if h == "codex":
        return CODEX_Q, "python -m legion budget set --five-hour 80 --week 50 [--month 50]"
    return {"claude": CLAUDE_KEY_Q, "gemini": GEMINI_Q}.get(h, OTHER_Q), hours


def ask_text(harness: str) -> str:
    q, how = question(harness)
    return (f"Before any work: ask your person this, in their language, and wait for the answer:\n\n  {q}\n\n"
            f"Then record what they say: `{how}` (each limit is optional; `--no-limit` if they want none). Then "
            "claim again. Under `legion run`, nobody needs to ask: run asks the person itself.")


# --- what's stored ------------------------------------------------------------------------------------------------

def _file(repo) -> Path:
    return coordgit.common_dir(repo) / "legion-budget.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    for _ in range(5):  # Windows can't replace a file another process is reading: a moment later it can
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
            return
        except OSError:
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)


def load(repo) -> dict:
    return _read(_file(repo))


def is_set(repo) -> bool:
    b = load(repo)
    return bool(b.get("none") or b.get("limits"))


def save(repo, harness: str, limits: dict, none: bool = False) -> dict:
    b = load(repo)
    clean = {k: float(v) for k, v in limits.items() if v is not None}
    for k, v in clean.items():
        if k in WINDOWS and not 0 < v <= 100:
            raise ValueError(f"{WINDOWS[k][2]} is a percent: between 1 and 100")
        if k == HOURS and not 0 < v <= 24:
            raise ValueError("--hours-per-day is between 0 and 24")
    b.update({"harness": harness_of(harness), "limits": {} if none else clean, "none": bool(none),
              "set_at": round(time.time(), 1)})
    _write(_file(repo), b)
    return b


def remember(repo, **fields) -> None:
    b = load(repo)
    b.update(fields)
    _write(_file(repo), b)


def asked_for(repo) -> bool:
    """Whether this clone's agent must ask its person before any work: a clone `join` made without a person at the
    keyboard to ask (an agent ran it). An integrator's clone, or one whose person answered, isn't asked."""
    p = coordgit.run(Path(repo), "config", "--get", "legion.budgetask")
    return p.returncode == 0 and p.stdout.decode().strip() == "true"


# --- meters -------------------------------------------------------------------------------------------------------

def claude_file() -> Path:
    return home() / "usage-claude.json"


def record_claude(data: dict, now: float | None = None) -> None:
    """Keep the rate limits Claude Code hands its status line (rate_limits.five_hour / seven_day, each with
    used_percentage and resets_at; only for subscribers, and only once a session had an answer)."""
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        return
    got = {}
    for name in ("five_hour", "seven_day"):
        w = rl.get(name)
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
            got[name] = {"used": float(w["used_percentage"]), "resets_at": w.get("resets_at")}
    if got:  # a window it leaves out (its reset passed) keeps its last reading: read_claude sees it reset
        _write(claude_file(), {**_read(claude_file()), "ts": now or time.time(), **got})


def read_claude(now: float) -> dict:
    data = _read(claude_file())
    out = {}
    for name in ("five_hour", "seven_day"):
        w = data.get(name)
        if isinstance(w, dict) and isinstance(w.get("used"), (int, float)):
            resets = w.get("resets_at") if isinstance(w.get("resets_at"), (int, float)) else None
            if resets is not None and resets <= now:
                out[name] = {"used": 0.0, "resets_at": None, "ts": data.get("ts")}  # the window reset since
            else:
                out[name] = {"used": float(w["used"]), "resets_at": resets, "ts": data.get("ts")}
    return out


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def read_codex(now: float, files: int = 8) -> dict:
    """The latest rate limits Codex wrote in its session files: token_count events carry rate_limits.primary and
    .secondary, each with used_percent, window_minutes and resets_at."""
    root = codex_dir()
    try:
        paths = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:files]
    except OSError:
        return {}
    for path in paths:
        try:  # its last events are enough, and a long session's file runs to megabytes
            with open(path, "rb") as f:
                f.seek(max(0, f.seek(0, 2) - TAIL))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            rl = _find(ev, "rate_limits")
            if not isinstance(rl, dict):
                continue
            out = {}
            for w in (rl.get("primary"), rl.get("secondary")):
                if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
                    continue
                name = min(WINDOWS, key=lambda k: abs(WINDOWS[k][0] - (w.get("window_minutes") or 0)))
                resets = w.get("resets_at") if isinstance(w.get("resets_at"), (int, float)) else None
                used = 0.0 if resets is not None and resets <= now else float(w["used_percent"])
                out[name] = {"used": used, "resets_at": resets if used else None}
            if out:
                return out
    return {}


def _find(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            if isinstance(v, (dict, list)) and (found := _find(v, key)) is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            if (found := _find(v, key)) is not None:
                return found
    return None


def _runtime_file(repo) -> Path:
    return coordgit.common_dir(repo) / "legion-runtime.json"


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def run_started(repo, now: float | None = None) -> None:
    now = now or time.time()
    r = _read(_runtime_file(repo))
    if r.get("day") != _day(now):
        r = {"day": _day(now), "seconds": 0.0}
    r["running"] = now
    _write(_runtime_file(repo), r)


def run_ended(repo, now: float | None = None) -> None:
    now = now or time.time()
    r = _read(_runtime_file(repo))
    start = r.pop("running", None)
    if r.get("day") != _day(now):
        r = {"day": _day(now), "seconds": 0.0}
        start = max(start or now, _midnight(now))
    if isinstance(start, (int, float)):
        r["seconds"] = r.get("seconds", 0.0) + max(0.0, now - max(start, _midnight(now)))
    _write(_runtime_file(repo), r)


def _midnight(now: float, ahead: int = 0) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + ahead, 0, 0, 0, 0, 0, -1))


def hours_today(repo, now: float) -> float:
    r = _read(_runtime_file(repo))
    if r.get("day") != _day(now):
        secs = 0.0
        if isinstance(r.get("running"), (int, float)):
            secs = now - max(r["running"], _midnight(now))
        return max(0.0, secs) / 3600
    secs = r.get("seconds", 0.0)
    if isinstance(r.get("running"), (int, float)):
        secs += max(0.0, now - max(r["running"], _midnight(now)))
    return secs / 3600


def readings(repo, harness: str, now: float) -> dict:
    h = harness_of(harness)
    got = read_claude(now) if h == "claude" else read_codex(now) if h == "codex" else {}
    got[HOURS] = {"used": hours_today(repo, now), "resets_at": _midnight(now, 1)}
    return got


# --- the verdict --------------------------------------------------------------------------------------------------

def check(repo, now: float | None = None) -> dict:
    """{set, none, harness, limits, readings, over: [{name, used, limit, resets_at}], unmeasured: [names]}."""
    now = now or time.time()
    b = load(repo)
    limits = b.get("limits") or {}
    res = {"set": bool(b.get("none") or limits), "none": bool(b.get("none")), "harness": b.get("harness"),
           "limits": limits, "readings": {}, "over": [], "unmeasured": []}
    if not limits:
        return res
    got = res["readings"] = readings(repo, b.get("harness"), now)
    for name, limit in limits.items():
        r = got.get(name)
        if r is None:
            res["unmeasured"].append(name)
        elif r["used"] >= limit:
            res["over"].append({"name": name, "used": r["used"], "limit": limit, "resets_at": r.get("resets_at")})
    return res


def over(repo, now: float | None = None) -> dict | None:
    res = check(repo, now)
    return res if res["over"] else None


def resume_at(res: dict, now: float | None = None) -> float:
    """When every limit that's reached will have reset: the latest of their resets (a reading with none known is
    looked at again in RECHECK seconds)."""
    now = now or time.time()
    times = [o["resets_at"] for o in res["over"]]
    if any(t is None for t in times) or not times:
        return now + RECHECK
    return max(times)


def label(name: str) -> str:
    return WINDOWS[name][1] if name in WINDOWS else "hours today"


def describe(o: dict) -> str:
    if o["name"] == HOURS:
        return f"{o['used']:.1f} of the {o['limit']:g} hours a day your person allowed"
    return f"{o['used']:.0f}% of the {label(o['name'])} used (your person allowed {o['limit']:g}%)"


def when(t: float | None) -> str:
    if not t:
        return "when the next reading shows it reset"
    return time.strftime("%a %H:%M" if t - time.time() > 20 * 3600 else "%H:%M", time.localtime(t))


def paused_text(res: dict, under_run: bool) -> str:
    what = "; ".join(describe(o) for o in res["over"])
    reset = resume_at(res) if all(o["resets_at"] for o in res["over"]) else None
    return (f"Your person's limit is reached: {what}. It resets {'at ' if reset else ''}{when(reset)}. "
            "Start nothing new: save what you have (`python -m legion save <task-id> --notes \"...\"`) and end your "
            "session now. " + ("`legion run` starts you again once it has reset." if under_run else
                              "Tell your person, and stop until then."))


def show(repo, now: float | None = None) -> str:
    now = now or time.time()
    res = check(repo, now)
    if not res["set"]:
        return "No limit set for this clone's agent yet: `python -m legion budget set ...` (`budget ask` shows the question)."
    if res["none"]:
        return f"Your person set no limit for this clone's agent ({res['harness']})."
    lines = [f"Limits for this clone's agent ({res['harness']}):"]
    for name, limit in res["limits"].items():
        r = res["readings"].get(name)
        unit = "h" if name == HOURS else "%"
        if r is None:
            used = "not measured yet" + (" (Claude Code reports it to its status line once a session has had an answer)"
                                         if res["harness"] == "claude" else "")
        else:
            used = f"{r['used']:.1f}{unit} used" + (f", resets {when(r['resets_at'])}" if r.get("resets_at") else "")
            if r.get("ts") and now - r["ts"] > STALE:
                used += f" (reading from {time.strftime('%a %H:%M', time.localtime(r['ts']))})"
        lines.append(f"  {label(name)}: limit {limit:g}{unit}, {used}")
    lines.append("PAUSED: " + "; ".join(describe(o) for o in res["over"]) if res["over"] else "Under every limit.")
    return "\n".join(lines)


def ask(repo, harness: str, read=input, say=print) -> dict:
    """Ask the person at the keyboard (join, run) and record the answer."""
    h = harness_of(harness)
    q, _ = question(h)
    say(f"\n{q}")
    limits = {}
    if h in ("claude", "codex") and not (h == "claude" and os.environ.get("ANTHROPIC_API_KEY")):
        names = ["five_hour", "seven_day"] + (["month"] if h == "codex" else [])
        for name in names:
            limits[name] = _number(read(f"  Percent of the {label(name)} (Enter: no limit): "), 100)
    else:
        limits[HOURS] = _number(read("  Hours a day (Enter: no limit): "), 24)
    limits = {k: v for k, v in limits.items() if v is not None}
    b = save(repo, h, limits, none=not limits)
    say("Recorded: " + ("no limit." if b["none"] else ", ".join(
        f"{label(k)} {v:g}{'h' if k == HOURS else '%'}" for k, v in b["limits"].items()) + ".")
        + " `python -m legion budget set ...` changes it.")
    return b


def _number(text: str, top: float) -> float | None:
    text = (text or "").strip().rstrip("%h ").replace(",", ".")
    try:
        v = float(text)
    except ValueError:
        return None
    return v if 0 < v <= top else None
