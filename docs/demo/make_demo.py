"""Record the landing page's demo: a simulated legion run on a made-up attack on the Happy Ending problem.

    python docs/demo/make_demo.py

It writes real, signed legion events (every one of them valid: the script checks the state ignores none),
then records what the viewer shows at each mark of the timeline into docs/demo/happy-ending.json, which
docs/viewer.html replays without a server (viewer.html?demo=demo/happy-ending.json). It also refreshes
docs/viewer.html from legion's template, so the demo always uses the current viewer.

The run is invented: the problem is real (Erdős–Szekeres for heptagons is open), the agents and their
progress are not.
"""

import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from legion import events as E  # noqa: E402
from legion import state as S  # noqa: E402
from legion import viz  # noqa: E402
from legion.identity import Identity  # noqa: E402

OUT = Path(__file__).parent / "happy-ending.json"
T0 = 1_793_000_000.0  # timestamps start somewhere in 2026; only their order and gaps matter


class Run:
    def __init__(self):
        self.events, self.ts, self.lamport = [], T0, 0
        self.people = {}

    def who(self, name, kind=None, model=None):
        if name not in self.people:
            self.people[name] = (Identity.generate(name), kind, model)
        return self.people[name]

    def add(self, name, type, minutes=3.0, **payload):
        ident, kind, model = self.who(name)
        os.environ.pop("LEGION_AGENT", None)
        os.environ.pop("LEGION_MODEL", None)
        for var in [k for k in os.environ if k.startswith(("CLAUDE", "CODEX_", "OPENCODE", "GEMINI_CLI"))]:
            os.environ.pop(var)  # don't let the machine running this script leak into the agents' events
        if kind:
            os.environ["LEGION_AGENT"] = kind
        if model:
            os.environ["LEGION_MODEL"] = model
        self.lamport += 1
        self.ts += minutes * 60
        ev = E.create(ident, type, self.lamport, **payload)
        ev = dict(ev, ts=self.ts)  # a story told over hours, not the seconds this script takes
        body = E.canonical(E._body(ev))
        ev["id"] = hashlib.sha256(body).hexdigest()[:16]
        ev["sig"] = ident.sign(body)
        self.events.append(ev)
        return ev

    def task(self, key, title, **spec):
        spec.setdefault("zone", [f"src/{key}/"])
        spec.setdefault("accept", f"python -m pytest tests/test_{key.replace('-', '_')}.py")
        return S.task_id(self.add("paul", "task.created", minutes=1, key=key, title=title, **spec))

    def claim(self, who, tid, minutes=4):
        return self.add(who, "task.claimed", minutes=minutes, task=tid, ttl=86400)

    def patch(self, who, tid, n=1, minutes=25):
        return self.add(who, "patch.submitted", minutes=minutes, task=tid, commit=f"c{self.lamport}",
                        branch=f"refs/legion/patches/{tid}/{who}-{n}")

    def accept(self, p, minutes=4, **extra):
        self.main = f"m{self.lamport}"
        return self.add("paul", "patch.accepted", minutes=minutes, patch=p["id"], task=p["task"],
                        merge_commit=self.main, **extra)

    def reject(self, p, reason, code, minutes=4):
        return self.add("paul", "patch.rejected", minutes=minutes, patch=p["id"], task=p["task"], reason=reason, code=code)

    def report(self, who, tid, outcome, notes, minutes=2):
        return self.add(who, "attempt.reported", minutes=minutes, task=tid, outcome=outcome, notes=notes)

    def solve(self, who, tid, notes, minutes=30):
        self.claim(who, tid)
        p = self.patch(who, tid, minutes=minutes)
        self.accept(p)
        self.report(who, tid, "success", notes)

    def children(self, parent_tid, parent_key, origin, kids):
        ids = {}
        for key, title, deps in kids:
            ev = self.add("paul", "task.created", minutes=0.5, key=f"{parent_key}/{key}", title=title,
                          zone=[f"src/{parent_key}/{key}.py"],
                          accept=f"python -m pytest legion_sketches/{parent_tid}/test_{key.replace('-', '_')}.py",
                          size="M", parent=parent_tid, deps=[ids[d] for d in deps], **origin)
            ids[key] = S.task_id(ev)
        return ids


def story() -> list[dict]:
    r = Run()
    r.who("paul")
    for name, kind, model in [("ada#1", "claude", "claude-opus-5-5"), ("alan#1", "codex", "gpt-6"),
                              ("emmy#1", "gemini", "gemini-3-pro"), ("kurt#1", "deepseek", "deepseek-v4"),
                              ("sofia#1", "muse", None), ("grace#1", "instinct", None),
                              ("john#1", "opencode", "qwen-4"), ("hilbert#1", "mistral", "mistral-large-3")]:
        r.who(name, kind, model)

    r.add("paul", "project.init", minutes=0, project="The Happy Ending problem, n = 7", integrators=[r.people["paul"][0].author],
          description="The Happy Ending problem, n = 7. Erdős and Szekeres proved that every large enough set of "
                      "points in general position contains a convex polygon with n vertices, and conjectured that "
                      "2^(n-2) + 1 points always suffice. It's known up to hexagons (17 points, by computer in 2006). "
                      "This project goes for heptagons: every 33 points in general position contain a convex heptagon. "
                      "The plan: encode it as a SAT problem, break its symmetries, prove it unsatisfiable, and check "
                      "the proof independently. Meanwhile, a hunt for 32 points with no convex heptagon, which would "
                      "show 33 is the best possible.")
    statement = r.task("statement", "State the problem precisely", size="S",
                       body="General position, convexity, what a 7-cup and a 7-cap are, as code and tests.")
    encode = r.task("encode", "Encode \"no convex heptagon\" as SAT", size="L", deps=[statement],
                    body="Variables for the orientation of every triple (signature encoding).")
    symmetry = r.task("symmetry", "Break the symmetries", deps=[encode])
    solve = r.task("solve-33", "Prove UNSAT for 33 points", size="L", deps=[symmetry])
    check = r.task("check-proof", "Check the proof independently", deps=[solve])
    lean = r.task("lean-statement", "State the theorem in Lean", size="S", zone=["lean/"],
                  accept="lake build HappyEnding.Statement")
    hunt = r.task("hunt-32", "Find 32 points with no convex heptagon", mode="competitive", zone=["src/hunt/"],
                  metric="python src/hunt/score.py", goal="max",
                  body="Points in general position with no convex heptagon: the more, the closer to proving 33 optimal.")

    for name in ("ada#1", "alan#1", "emmy#1", "kurt#1", "sofia#1", "grace#1", "john#1", "hilbert#1"):
        r.add(name, "agent.joined", minutes=1)

    # The statement and the Lean statement go first, in parallel.
    r.claim("ada#1", statement)
    r.claim("grace#1", lean, minutes=1)
    p = r.patch("ada#1", statement, minutes=20)
    r.accept(p)
    r.report("ada#1", statement, "success", "Orientation predicate with exact integer arithmetic; cups and caps as code.")
    p = r.patch("grace#1", lean, minutes=12)
    r.reject(p, "the acceptance command fails: unknown identifier 'Convex.IsPolygon'", "tests")
    r.report("grace#1", lean, "failure", "Used a Mathlib name that doesn't exist; defining convex position by hand.")
    p = r.patch("grace#1", lean, n=2, minutes=18)
    r.accept(p)

    # The encoding is big: Alan splits it into three pieces, and three agents take them.
    r.claim("alan#1", encode)
    k = r.add("alan#1", "sketch.proposed", minutes=15, task=encode, commit="sk1", branch=f"refs/legion/patches/{encode}/alan-s1",
              notes="Three independent families of clauses over the same orientation variables.",
              children=[{"key": k_, "title": t_, "zone": [f"src/encode/{k_}.py"],
                         "accept": f"python -m pytest legion_sketches/{encode}/test_{k_.replace('-', '_')}.py"}
                        for k_, t_ in (("orientations", "Orientation variables and their axioms"),
                                       ("cups-caps", "Forbid 7-cups and 7-caps"),
                                       ("general-position", "No three points on a line"))])
    r.add("paul", "sketch.accepted", minutes=3, sketch=k["id"], task=encode, merge_commit="msk1")
    kids = r.children(encode, "encode", {"sketch": k["id"]},
                      [("orientations", "Orientation variables and their axioms", []),
                       ("cups-caps", "Forbid 7-cups and 7-caps", []),
                       ("general-position", "No three points on a line", [])])
    r.claim("emmy#1", kids["orientations"])
    r.claim("kurt#1", kids["cups-caps"], minutes=1)
    r.claim("john#1", kids["general-position"], minutes=1)
    p = r.patch("john#1", kids["general-position"], minutes=14)
    r.reject(p, "touches files outside its zone (src/encode/general-position.py): README.md", "zone")
    r.report("john#1", kids["general-position"], "failure", "Also edited the README; resubmitting without it.")
    p = r.patch("emmy#1", kids["orientations"], minutes=6)
    r.accept(p)
    p = r.patch("john#1", kids["general-position"], n=2, minutes=5)
    r.accept(p)
    p = r.patch("kurt#1", kids["cups-caps"], minutes=20)
    r.accept(p)
    r.report("kurt#1", kids["cups-caps"], "success", "7-cups and 7-caps via the signature trick: O(n^4) clauses.")
    r.add("paul", "task.resolved", minutes=6, task=encode, sketch=k["id"])

    # Symmetries; the competitive hunt runs alongside.
    r.claim("ada#1", symmetry)
    p = r.add("sofia#1", "patch.submitted", minutes=8, task=hunt, commit="h1", branch=f"refs/legion/patches/{hunt}/sofia-1")
    r.accept(p, metric=28)
    p = r.add("hilbert#1", "patch.submitted", minutes=15, task=hunt, commit="h2", branch=f"refs/legion/patches/{hunt}/hilbert-1")
    r.accept(p, metric=30)
    p = r.patch("ada#1", symmetry, minutes=20)
    r.accept(p)
    r.report("ada#1", symmetry, "success", "Lexicographic leader on the radial order around the lowest point: 33x fewer cases.")
    p = r.add("sofia#1", "patch.submitted", minutes=10, task=hunt, commit="h3", branch=f"refs/legion/patches/{hunt}/sofia-2")
    r.accept(p, metric=31)

    # The big solve gets stuck.
    r.claim("kurt#1", solve)
    r.report("kurt#1", solve, "failure", "CaDiCaL on the whole instance: no answer after 48 hours.", minutes=60)
    r.report("alan#1", solve, "failure", "Kissat with the symmetry clauses: memory runs out at 120 GB.", minutes=40)
    r.report("emmy#1", solve, "failure", "Tried dropping half the cup clauses as redundant: they aren't, the solver finds a bogus model.", minutes=30)
    p1 = r.add("ada#1", "path.proposed", minutes=8, task=solve,
               notes="One solver run on the whole thing won't finish. Cube and conquer: split the instance into cubes "
                     "by the first decisions and solve each one separately.",
               children=[{"key": k_, "title": t_, "zone": [f"src/solve-33/{k_}.py"],
                          "accept": f"python -m pytest legion_sketches/{solve}/test_{k_.replace('-', '_')}.py"}
                         for k_, t_ in (("cubes-a", "Cubes 1 to 512"), ("cubes-b", "Cubes 513 to 1024"),
                                        ("cubes-c", "Cubes 1025 to 1536"), ("cubes-d", "Cubes 1537 to 2048"))])
    p2 = r.add("grace#1", "path.proposed", minutes=3, task=solve,
               notes="Before 33 points, reproduce the solved case: hexagons on 17 points. If our encoding can't "
                     "redo that quickly, it won't do heptagons.",
               children=[{"key": "hexagons-17", "title": "Reproduce the hexagon case (17 points)",
                          "zone": ["src/solve-33/hexagons.py"],
                          "accept": f"python -m pytest legion_sketches/{solve}/test_hexagons_17.py"}])
    r.add("paul", "path.accepted", minutes=2, path=p1["id"], task=solve)
    cubes = r.children(solve, "solve-33", {"path": p1["id"]},
                       [("cubes-a", "Cubes 1 to 512", []), ("cubes-b", "Cubes 513 to 1024", []),
                        ("cubes-c", "Cubes 1025 to 1536", []), ("cubes-d", "Cubes 1537 to 2048", [])])
    r.add("paul", "path.accepted", minutes=2, path=p2["id"], task=solve)
    hexa = r.children(solve, "solve-33", {"path": p2["id"]}, [("hexagons-17", "Reproduce the hexagon case (17 points)", [])])
    r.claim("grace#1", hexa["hexagons-17"])
    for who, key in (("alan#1", "cubes-a"), ("emmy#1", "cubes-b"), ("john#1", "cubes-c"), ("hilbert#1", "cubes-d")):
        r.claim(who, cubes[key], minutes=1)
    p = r.patch("grace#1", hexa["hexagons-17"], minutes=35)
    r.accept(p)
    r.report("grace#1", hexa["hexagons-17"], "success", "17 points, hexagons: UNSAT in 41 minutes. The encoding holds up.")
    p = r.patch("alan#1", cubes["cubes-a"], minutes=50)
    r.accept(p)
    p = r.patch("emmy#1", cubes["cubes-b"], minutes=20)
    r.reject(p, "the acceptance command fails: the DRAT proof for cube 877 doesn't verify", "tests")
    r.report("emmy#1", cubes["cubes-b"], "failure", "A solver bug on cube 877; rerunning it with another solver.")
    p = r.add("kurt#1", "patch.submitted", minutes=12, task=hunt, commit="h4", branch=f"refs/legion/patches/{hunt}/kurt-1")
    r.reject(p, "does not improve the metric: 31 vs 31", "no_gain")
    p = r.patch("john#1", cubes["cubes-c"], minutes=30)
    r.accept(p)
    p = r.patch("emmy#1", cubes["cubes-b"], n=2, minutes=40)
    r.accept(p)
    return r.events


def main():
    events = story()
    st = S.build(events)
    if st.ignored:
        for ev, why in st.ignored:
            print("IGNORED", ev["type"], ev.get("key") or ev.get("task"), "->", why)
        raise SystemExit("the demo has invalid events")
    events = st.events
    timeline = viz.timeline(events)
    points = sorted({m["i"] for m in timeline} | {len(events)})
    snapshots = [viz.snapshot(events, i) for i in points]
    final = viz.snapshot(events, None)
    nodes = [t["id"] for t in final["tasks"]] + [a["id"] for a in final["agents"]] + ["__goal__"]
    history = {n: viz.history(events, n, None) for n in nodes}
    logos = {}
    for kind in ("muse", "instinct"):
        f = ROOT / "legion" / "static" / "logos" / f"{kind}.png"
        logos[kind] = "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
    OUT.write_text(json.dumps({"total": len(events), "timeline": timeline, "snapshots": snapshots,
                               "history": history, "hub": viz.hub(events, None), "logos": logos},
                              separators=(",", ":")), encoding="utf-8")
    shutil.copyfile(ROOT / "legion" / "templates" / "viz.html", OUT.parent.parent / "viewer.html")
    print(f"{len(events)} events, {len(timeline)} marks, {len(snapshots)} snapshots -> {OUT} "
          f"({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
