"""Record Erdős #902's real run for the landing page: the viewer, replaying the project's own log.

    python docs/demo/make_erdos.py <clone of the Erdős #902 project>

Writes docs/demo/erdos902.json, which docs/viewer.html replays (viewer.html?demo=demo/erdos902.json). Only the
events that did something are steps of the replay: a task created, a patch sent, accepted or rejected, a result,
a path or a sketch taken. Claims, heartbeats, releases, saves, notes and messages, most of the log, are not, though
the state each step shows is the true one, built from every event. Agents are renamed after the kind of agent
they are (instinct#4, claude#2); the project's founder and its verifier are "founder" and "verifier".

Each step is stored as what changed since the one before, without the tasks' long texts: a month of a project
with hundreds of tasks stays a small file. It also refreshes docs/viewer.html from legion's template.
"""

import base64
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from legion import coordgit  # noqa: E402
from legion import state as S  # noqa: E402
from legion import viz  # noqa: E402

OUT = Path(__file__).parent / "erdos902.json"
KEEP = {"project.init", "task.created", "patch.submitted", "patch.accepted", "patch.rejected", "result.submitted",
        "path.accepted", "sketch.accepted", "task.resolved"}
SPECIAL = {"jose": "founder", "jose#9": "verifier"}
KNOWN = ("instinct", "muse", "claude", "opencode", "codex", "gemini", "cursor", "deepseek")
LEFT = re.compile(r"jose|pepe|sergi|peper", re.I)  # a person's handle, if any survived
WORD = re.compile(r"[A-Za-z0-9_-]*(?:jose|pepe|sergi|peper)[A-Za-z0-9_-]*", re.I)  # the word that holds one


def slim(t: dict) -> dict:
    return {**t, "body": "", "zone": [], "accept": "", "refute": "", "ideas": []}


def names_map(events: list[dict]) -> dict[str, str]:
    kinds, order = {}, []
    for ev in events:
        n = ev["name"]
        if n not in kinds:
            order.append(n)
            kinds[n] = None
        if not kinds[n] and ev.get("agent"):
            kinds[n] = ev["agent"].lower()
    out, count = {}, {}
    for n in order:
        if n in SPECIAL:
            out[n] = SPECIAL[n]
            continue
        k = kinds[n] if kinds[n] in KNOWN else next((x for x in KNOWN if n.lower().startswith(x)), "agent")
        count[k] = count.get(k, 0) + 1
        out[n] = f"{k}#{count[k]}"
    for n in order:  # what the viewer reads as the owner, name#number -> name: the kind alone
        owner = n.rpartition("#")[0] or n
        if owner not in out:
            out[owner] = out[n].rpartition("#")[0] or out[n]
    return out


def rename(text: str, mapping: dict[str, str]) -> str:
    for old in sorted(mapping, key=len, reverse=True):
        text = re.sub(r"(?<![\w#-])" + re.escape(old) + r"(?![\w#-])", mapping[old], text)
    return text


def scrub(text: str) -> str:
    """What the renaming can't reach, a handle inside an agent's own words (a machine's name, a path): the word goes."""
    return WORD.sub("…", text)


def main(clone: str) -> None:
    full = S.build(coordgit.read_events(Path(clone)), trusted=True)
    events = full.events
    kept = [j + 1 for j, ev in enumerate(events) if ev["type"] in KEEP]
    st, at, frames = S.State(), 0, []
    prev_t, prev_a, prev_x = {}, {}, {}
    for n, j in enumerate(kept):
        S.extend(st, events[at:j], trusted=True)
        at = j
        snap = viz.snapshot(events, j, st=st, research=False)
        tasks = {t["id"]: slim(t) for t in snap.pop("tasks")}
        agents = {a["id"]: {**a, "last": a.get("last", "")[:160]} for a in snap.pop("agents")}
        for k in ("index", "total", "live"):
            snap.pop(k, None)
        snap["recent"] = []
        f = {}
        dt = {i: t for i, t in tasks.items() if prev_t.get(i) != t}
        dt.update({i: 0 for i in prev_t if i not in tasks})
        da = {i: a for i, a in agents.items() if prev_a.get(i) != a}
        da.update({i: 0 for i in prev_a if i not in agents})
        dx = {k: v for k, v in snap.items() if prev_x.get(k) != v}
        if dt:
            f["t"] = dt
        if da:
            f["a"] = da
        if dx:
            f["x"] = dx
        frames.append(f)
        prev_t, prev_a, prev_x = tasks, agents, snap
        if n % 100 == 0:
            print(f"  step {n} of {len(kept)}", flush=True)
    timeline = [{"i": k + 1, **viz.item(events[j - 1], full)} for k, j in enumerate(kept)]
    logos = {}
    for kind in ("muse", "instinct"):
        f = ROOT / "legion" / "static" / "logos" / f"{kind}.png"
        logos[kind] = "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
    text = json.dumps({"total": len(kept), "timeline": timeline, "frames": frames,
                       "base": {"research": {"maps": [], "links": [], "pulse": {}}, "people": {}, "recent": []},
                       "history": {}, "hub": {"chat": [], "literature": []}, "logos": {}},
                      ensure_ascii=False, separators=(",", ":"))
    text = rename(text, names_map(events))
    left = sorted({text[max(0, m.start() - 30):m.end() + 30] for m in LEFT.finditer(text)})
    if left:
        print(f"handles left in the recording ({len(left)}):", *left[:12], sep="\n  ")
    data = json.loads(text)
    data["logos"] = logos  # after the renaming: base64 could hold any letters
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    shutil.copyfile(ROOT / "legion" / "templates" / "viz.html", OUT.parent.parent / "viewer.html")
    print(f"{len(events)} events, {len(kept)} steps -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
