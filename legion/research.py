"""The research cycle: how agents work together on a task that is an open problem (state.METHODS has the rules).

A task whose statement nobody proves in a session can't be handed out whole: every attempt ends in "failure", the
agents learn nothing from one another's, and after a while they only check whether anything changed. Opened as a
research GOAL, it is worked on in rounds of an attack (attack.py) that nobody has to lead:

- PROPOSE. An agent with nothing to take proposes a line for the next round: one agent's work for one session, with
  a definite deliverable, on a FRONT (a direction), by a METHOD, with a MOVE. Each agent is handed a different move,
  and decides its line before it is shown the others' (agents shown each other's ideas first converge on them). A
  line on a front that has lines already says how it differs, or is refused.
- LAUNCH. Once there are enough proposals, any trusted agent's `claim` makes the round from them: the fronts least
  explored first, no method taking more than half of it. Its pieces are ordinary subtasks: a line, its check by
  another agent, one synthesis.
- FIND. What a line reports becomes FINDINGS on the goal (a precise statement, its status, where the evidence is)
  when its patch is submitted, and its checker's verdicts are recorded on them the same way. Dead ends are findings
  too. Together they are the goal's MAP: what the next round's proposals start from, in place of a text that
  someone has to keep rewriting.
- TURN. Rounds that don't move the goal are counted: nothing confirmed, or, said by the round's synthesis against
  the goal's text of what counts as progress, nothing that does. After a few, the only moves handed out are the
  ones that change the question; after a few more the goal is spent, and its agents stop instead of going round,
  until someone reopens it with something new.
"""

import hashlib
import json
import re
import shutil
import time
from collections import Counter, defaultdict

from . import attack as A
from . import state as S
from .ctx import Ctx, Fail

ACTIVE = 2 * 3600  # agents seen in this long count towards the size of a round
# A round waits this long, from its first proposal, for more, once no line of an earlier round is left for anyone:
# work for idle agents matters more than full rounds (an hour left three goals with nothing to hand out).
PROPOSE_WINDOW = 1200
NEXT_LINES = "next_lines.json"  # what a round's synthesis proposes for the next round (see propose_next)
PROGRESS = "progress.json"  # whether the round moved its goal, its synthesis says (see assess and State.moved)
MAX_ROUND_LINES = 6
LINE_WAIT = 12 * 3600  # a round's line nobody took in this long no longer holds the next round back
RELAUNCH = -1  # cycle_work launched a round: claim looks again, its pieces are open now

MOVE_TEXT = {
    "extend": "take the most informative confirmed finding one definite step further",
    "obstruct": "prove WHY a family of failed attempts can't work: a barrier that rules the whole family out, so "
                "nobody tries its members one by one",
    "specialise": "settle the smallest case still undecided, exactly, and say what its answer suggests in general",
    "weaken": "prove a weaker statement than the goal (a worse constant, an extra hypothesis, a special family) "
              "that nobody has, stated precisely, with what separates it from the goal",
    "transfer": "bring a technique or a theorem from another field or from the literature (add the reference with "
                "`lit add`) and test it on a definite piece of the problem",
    "invert": "attack the negation: look for a counterexample, the dual problem, or the extremal object the "
              "statement forbids, and say what its absence would prove",
    "recombine": "combine two confirmed findings from different fronts into something neither gives alone",
    "wild": "open a front, by a method, that nobody has tried on this goal: a real attempt, not a variation",
    "exemplar": "find the smallest or the most symmetric objects that exist for a case of the problem (a SAT search "
                "with `python -m legion sat`, a search under an assumed symmetry), and describe their structure: "
                "what a general construction could copy from them",
}

BARRIERS = "barriers.json"  # what a round's synthesis says is now known not to work (see State.barriers_for)

# Where the ideas of an idea round are to come from: each agent is handed one, the ones a goal's earlier idea rounds
# used least first. Told only "a new idea", agents propose the variation of the map they were going to propose anyway.
FIELDS = {
    "coding theory": "codes and their bounds and constructions: linear, concatenated and algebraic-geometry codes, "
                     "list decoding, covering codes",
    "extremal set theory": "intersecting families, transversals and covers, shadows and Kruskal-Katona, the "
                           "sunflower lemma, the delta-system method",
    "algebraic constructions": "finite fields, quadratic residues and characters, Cayley graphs and tournaments, "
                               "character sum bounds (Weil), explicit pseudorandom objects",
    "the polynomial method": "the Combinatorial Nullstellensatz, slice rank, the cap-set method, Alon-Furedi, "
                             "polynomials vanishing on the objects you count",
    "entropy": "Shearer's lemma, entropy compression, information-theoretic counting and its stability versions",
    "hypergraph containers": "container lemmas, counting independent sets, supersaturation, the method of "
                             "graph containers",
    "the probabilistic method": "beyond the first moment: alterations, the Lovasz Local Lemma, the Rodl nibble, "
                                "dependent random choice, Janson and Talagrand, random greedy processes",
    "spectral methods": "eigenvalues and expanders, the expander mixing lemma, interlacing, quasirandomness",
    "Fourier analysis": "Boolean functions on the hypercube, hypercontractivity, influences, KKL, Fourier on finite "
                        "abelian groups",
    "design theory": "block designs, Steiner systems, finite geometries and their incidence structures, Latin "
                     "squares, absorbing constructions",
    "LP and SDP duality": "Delsarte's linear programming bound, flag algebras, the Lovasz theta function, "
                          "dual certificates for extremal problems",
    "topological methods": "Borsuk-Ulam, Kneser-type colouring arguments, nerve theorems, fixed points",
    "computational exemplars": "the smallest or most symmetric objects that exist for small cases, found by SAT "
                               "(`python -m legion sat`) or by a search under an assumed symmetry, and a "
                               "construction read off their structure",
}

IDEAS_SYNTHESIS = ("This was a round of IDEAS: `python -m legion attack digest {parent} --round {round}` shows each "
                   "idea and its judgement. (1) Rank them: an idea whose kill test killed it, or of novelty 0, is out; "
                   "then by novelty plus plausibility, and whether its escapes hold. Say in your report which you "
                   "keep and why, and what the others taught. (2) Make DEEP LINES of the best, at most "
                   f"{S.MAX_DEEP}: write {NEXT_LINES} in the synthesis's folder, one line per kept idea, each with "
                   '"deep": true, its kill test as the first thing the text asks, then its first step, its "escapes" '
                   'as the idea gave them and the judge confirmed, "move": "wild" or "transfer", and a new "front" '
                   "(as `python -m legion help research` describes a line). On submit they are proposed for the "
                   "next round, as yours. (3) What a killed idea showed CAN'T work, if it bars a whole family of "
                   f"arguments: write {BARRIERS} in the folder, a JSON list of objects with \"name\" (a short key) and "
                   '"statement" (which family, and what stops it). Write no progress verdict: a round of ideas '
                   "neither moves its goal nor counts against it.")


def barriers_text(st: S.State, t: S.Task, width: int = 4000) -> str:
    """A goal's barriers in words: what is known not to work on it, that a line says it escapes."""
    bars = st.barriers_for(t.id)
    if not bars:
        return ""
    per = max(160, min(600, width // max(1, len(bars))))
    return ("BARRIERS (what is known not to work on this goal: a line says, in its escapes, why it doesn't fall into "
            "them):\n" + "\n".join(f"  {b['barrier']}: {_cut(b['statement'], per)}"
                                   + (f" [{', '.join(x[:8] for x in b.get('from') or [])}]" if b.get("from") else "")
                                   for b in bars))


def move_for(st: S.State, goal: str, name: str) -> str:
    """The move an agent is handed for the goal's next round: a different one for each, the same on every node,
    and another one next round. Once rounds stop confirming anything, only the moves that change the question."""
    moves = list(S.REFRAME if st.dry(goal) >= S.DRY_REFRAME else S.MOVES)
    # extend and recombine build on findings that stand: where none does yet (a map just seeded, every finding
    # still its author's word), an agent handed recombine had nothing confirmed to combine.
    stand = [f for f in st.findings_for(goal) if st.finding_state(f) == "confirmed" and f["ev"]["status"] != "dead end"]
    if not stand:
        moves.remove("extend") if "extend" in moves else None
    if len({f["front"] for f in stand}) < 2 and "recombine" in moves:
        moves.remove("recombine")
    n = len(st.research_rounds(goal))
    return moves[int(hashlib.sha256(f"{name}|{goal}|{n}".encode()).hexdigest(), 16) % len(moves)]


def lacking(t: S.Task) -> list[str]:
    """The programs a task needs that this machine doesn't have."""
    return [n for n in t.needs if not shutil.which(n)]


def active_agents(st: S.State, now: float) -> set[str]:
    return {a for a, ts in st.last_seen.items() if now - ts <= ACTIVE}


def round_size(st: S.State, now: float) -> int:
    """How many lines a round takes: each wants someone to explore it and someone else to check it."""
    return max(2, min(MAX_ROUND_LINES, len(active_agents(st, now)) // 2))


def free_lines(st: S.State, goal: str, now: float) -> list[S.Task]:
    """The lines of the goal's rounds that are open with nobody on them."""
    return [t for t in st.tasks.values() if t.parent == goal and t.attack.get("role") == "line" and not t.cancelled
            and st.status(t.id, now) == "open" and st.holder(t.id, now) is None]


def select(st: S.State, goal: str, props: list[dict], size: int) -> list[dict]:
    """Which proposals make the round: one per front, the fronts with the fewest lines so far first (what's
    unexplored before what's promising: a rating picks what looks typical), then the best backed; round again
    while there's room; and no method takes more than half of it while another is on offer."""
    ran, by = Counter(x["ev"]["front"] for x in st.lines_for(goal) if x["piece"]), defaultdict(list)
    for p in sorted(props, key=lambda p: (-len(p["backed"]), p["ev"]["lamport"], p["ev"]["id"])):
        by[p["ev"]["front"]].append(p)
    fronts = sorted(by, key=lambda f: (ran[f], -len(by[f][0]["backed"]), by[f][0]["ev"]["lamport"], f))
    chosen, methods, cap = [], Counter(), (size + 1) // 2
    for capped in (True, False):  # with the cap on methods first; without it only to fill what's left
        took = True
        while took and len(chosen) < size:
            took = False
            for f in fronts:
                p = next((p for p in by[f] if not any(p is q for q in chosen)
                          and not (capped and methods[p["ev"]["method"]] >= cap)), None)
                if p is not None and len(chosen) < size:
                    chosen.append(p)
                    methods[p["ev"]["method"]] += 1
                    took = True
    return chosen


def due(st: S.State, goal: str, now: float) -> list[dict] | None:
    """The lines of the round to launch on the goal now, or None: there are proposals, no line of an earlier round
    is still waiting for someone (rounds answer demand), and either enough agents proposed to fill one or the
    first proposal has waited PROPOSE_WINDOW."""
    props = st.open_lines(goal)
    # A line still on offer means there's work for whoever asks. Not one that has waited LINE_WAIT: nobody here
    # can take it (a program its machines lack, or every agent already let it go), and it would stop the cycle.
    if (not props or st.spent(goal) or goal not in st.goals
            or any(now - t.ts < LINE_WAIT for t in free_lines(st, goal, now))):
        return None
    size, active = round_size(st, now), active_agents(st, now)
    authors = len({p["ev"]["author"] for p in props})
    enough = len(props) >= size and authors >= min(2, max(1, len(active)))
    if not enough and now - min(p["ev"]["ts"] for p in props) < PROPOSE_WINDOW:
        return None
    return select(st, goal, props, size)


def wanted(st: S.State, goal: str, now: float) -> int:
    """How many more lines the goal's next round would take: what a full round holds, less what's proposed."""
    return max(0, round_size(st, now) - len(st.open_lines(goal)))


def line_key(p: dict) -> str:
    return f"{p['ev']['front']}-{p['ev']['id'][:4]}"


def spec_for(st: S.State, parent: S.Task, chosen: list[dict]) -> dict:
    """The attack's spec that makes a round from proposed lines: what every piece shares is the goal and its map."""
    lines = []
    for p in chosen:
        ev = p["ev"]
        task = (f"{ev['text'].strip()}\n\nMETHOD: {ev['method']}. MOVE: {ev['move']} ({MOVE_TEXT[ev['move']]}).\n"
                f"IF IT FAILS, THE PROJECT LEARNS: {ev['learn'].strip()}"
                + (f"\nHOW IT DIFFERS from earlier lines on this front: {ev['differs'].strip()}"
                   if ev.get("differs", "").strip() else "")
                + (f"\nWHY IT ESCAPES the goal's barriers: {ev['escapes'].strip()}" if ev.get("escapes", "").strip()
                   else "")
                + ("\nIT BUILDS ON the findings " + ", ".join(x[:8] for x in ev["from"])
                   + f" (`python -m legion map {parent.key}`)." if ev.get("from") else "")
                + f"\nProposed by {ev['name']}. Need something from another agent on the way (a lemma, a "
                  f"computation, a reference, a check with a tool you lack)? Ask for it, and go on: `python -m legion "
                  f"ask {parent.key} \"...\"` is handed out before anything else.")
        lines.append({"key": line_key(p), "title": ev["title"].strip(), "front": ev["front"], "task": task[:A.MAX_TEXT],
                      **({"needs": ev["needs"]} if ev.get("needs") else {}), "proposal": ev["id"],
                      **({"deep": True} if ev.get("deep") is True else {})})
    words = (st.goals[parent.id].get("text") or "").strip() if parent.id in st.goals else ""
    return {"goal": (f"A round of the research cycle on {parent.key} ({parent.title}): lines its agents proposed, "
                     "each on a front of its own where it could. A line that fails cleanly, and says why, moves "
                     "the map as much as one that succeeds." + (f"\n{words[:3000]}" if words else "")),
            "common": map_text(st, parent, 14000),
            "synthesis": ("Say which fronts this round closed and which it opened. Then write "
                          f"{PROGRESS} in the synthesis's folder, the round's verdict on the goal in five fields: "
                          '{"progress": true or false, "why": "...", "eliminated": "...", "open": "...", '
                          '"next": "..."}. ELIMINATED: the region of the problem this round ruled out, stated exactly '
                          "(which objects, which parameters, which family of arguments) and by which confirmed "
                          "result; \"nothing\" if it ruled nothing out. OPEN: where the answer can still be, as "
                          "precisely as the map allows. NEXT: the one thing to do about that region now. Each at "
                          f"most {S.MAX_FINDING} characters. They head the goal's map, so the next round starts from "
                          "what is left instead of from a list of lemmas. PROGRESS: true only if a confirmed result "
                          "of this round "
                          "is progress as the goal's text above defines it (a bound improved, a construction found, "
                          "a case decided, a barrier that rules out a whole named family): narrow claims that are "
                          "correct but leave the goal where it was are false. Rounds that don't move the goal "
                          f"are counted: after {S.DRY_REFRAME} the next rounds change the question, after "
                          f"{S.DRY_SPENT} the goal is spent; no {PROGRESS} counts as false; WHY says which result, "
                          "or why none. Then propose the next round's lines, the first of them your NEXT: write "
                          f"{NEXT_LINES} in the synthesis's folder, a JSON list of one to three "
                          "lines, each an object with front, method, title, text, learn (and differs, on a front "
                          "that has lines already; escapes, on a goal with barriers; \"deep\": true for one approach "
                          "to work on over several sessions, its kill test first), as `python -m legion help "
                          "research` describes a line. On submit they are proposed for the next round, as yours; "
                          "whatever the rules refuse is said, and the patch goes in all the same. And if this round "
                          "showed that a whole family of arguments or objects can't work: write " + BARRIERS + " too, "
                          "a JSON list of objects with \"name\" (a short key) and \"statement\" (which family, and "
                          "what stops it): the goal's barriers, which every later line must say it escapes."),
            "assess": True,
            "lines": lines}


def fields_for(st: S.State, goal: str, n: int) -> list[str]:
    """The fields the next idea round's agents are handed: those its earlier idea rounds used least, in an order
    of the goal's own."""
    used = Counter(t.attack.get("field") for t in st.tasks.values() if t.parent == goal
                   and t.attack.get("kind") == "idea" and t.attack.get("role") == "line")
    k = len(st.research_rounds(goal))
    order = sorted(FIELDS, key=lambda f: (used[f], hashlib.sha256(f"{goal}|{k}|{f}".encode()).hexdigest()))
    return order[:n]


def idea_spec(st: S.State, parent: S.Task, now: float) -> dict:
    """The spec of a round of ideas on a goal: one idea per field handed out, each judged; the round's common text
    is the goal and its barriers, not its map (see State.ideas_due)."""
    n = max(3, round_size(st, now))
    words = _goal_words(st, parent, 3000)
    bars = barriers_text(st, parent, 8000) or ("BARRIERS: none named yet. What the rounds so far ruled out is on the "
                                               "map, which you read only after writing your idea.")
    lines = []
    for f in fields_for(st, parent.id, n):
        key = re.sub(r"[^a-z0-9]+", "-", f.lower()).strip("-")[:30]
        lines.append({"key": f"idea-{key}", "kind": "idea", "field": f, "field_text": FIELDS[f],
                      "title": f"a new approach from {f}", "task": FIELDS[f], "front": "ideas"})
    return {"goal": (f"{parent.key} ({parent.title}). " + (words or parent.title)),
            "common": bars, "synthesis": IDEAS_SYNTHESIS.format(parent=parent.key, round="{round}"), "lines": lines}


def launch_ideas(c: Ctx, parent: S.Task, now: float) -> str:
    """Make a round of ideas on the goal: its pieces are subtasks, as any round's."""
    spec = idea_spec(c.st, parent, now)
    rnd = A.next_round(c.st, parent)
    spec["synthesis"] = spec["synthesis"].replace("{round}", rnd)
    rnd, pieces = A.plan(c.st, parent, dict(spec, round=rnd), c.ident.name)
    for p in pieces:
        c.emit("task.created", **p, split=True)
    A.announce(c, parent, rnd, pieces, now)
    c.sync()
    return rnd


def may_launch(st: S.State, parent: S.Task, author: str, now: float) -> bool:
    return author in st.integrators or st.trusted(author) and st.took_part(parent.id, author, now)


def launch(c: Ctx, parent: S.Task, chosen: list[dict], now: float) -> str:
    """Make the round: its pieces are subtasks of the goal, created by whoever's claim found it due."""
    rnd, pieces = A.plan(c.st, parent, spec_for(c.st, parent, chosen), c.ident.name)
    for p in pieces:
        c.emit("task.created", **p, split=True)
    A.announce(c, parent, rnd, pieces, now)
    c.sync()
    return rnd


# --- the map ---------------------------------------------------------------------------------------------------------

def map_data(st: S.State, t: S.Task, now: float) -> dict:
    """A task's map, ready to print or draw: its fronts, every line with where it stands, its findings with their
    verdicts, and its rounds with what each yielded."""
    agent = lambda author: "a-" + author[8:20]  # noqa: E731 (viz.agent_id)
    pieces = {x.id: x for x in st.tasks.values() if x.parent == t.id and x.attack}
    checks = {x.attack.get("round", "") + "/" + x.attack.get("line", ""): x for x in pieces.values()
              if x.attack.get("role") == "check"}
    lines, seen = [], set()

    def who(piece):
        """Who a piece is with: its holder, or whoever's patch closed it or waits on it."""
        h = st.holder(piece.id, now)
        ps = st.patches_for(piece.id)
        src = h or next((p for p in reversed(ps) if p.verdict in (None, "accepted")), None)
        return (src.name, agent(src.author)) if src else (None, None)

    def entry(piece, x=None):
        ev = x["ev"] if x else None
        row = {"id": ev["id"] if ev else piece.id, "front": ev["front"] if ev else st.line_front(piece),
               "method": ev["method"] if ev else "other", "move": ev["move"] if ev else "",
               "title": ev["title"] if ev else piece.title, "text": (ev["text"] if ev else "")[:1500],
               "learn": ev["learn"] if ev else "", "differs": ev.get("differs", "") if ev else "",
               "by": ev["name"] if ev else None, "agent": agent(ev["author"]) if ev else None,
               "backed": sorted(x["backed"].values()) if x else [], "state": "proposed", "round": None,
               "piece": None, "worker": None, "worker_agent": None, "checker": None, "checker_agent": None,
               "check": None, "needs": (ev.get("needs") or []) if ev else list(piece.needs),
               "ts": ev["ts"] if ev else 0}
        if piece is not None:
            ck = checks.get(piece.attack.get("round", "") + "/" + piece.attack.get("line", ""))
            row.update(state=st.status(piece.id, now), round=piece.attack.get("round"), piece=piece.key,
                       piece_id=piece.id)
            row["worker"], row["worker_agent"] = who(piece)
            # What was done on it, whoever holds it now: a line let go of with work saved isn't one nobody touched.
            saves, tried = st.saved.get(piece.id, []), [a for a in st.attempts if a.get("task") == piece.id]
            took = {c.name: c.ts for c in st.claims.get(piece.id, ())}
            row["hands"] = sorted(set(took) | {w["name"] for w in saves})
            row["saves"] = [{"name": w["name"], "agent": agent(w["author"]), "notes": w["notes"][:600], "ts": w["ts"]}
                            for w in saves[-6:]]
            row["reports"] = [{"name": a["name"], "agent": agent(a["author"]), "outcome": a.get("outcome"),
                               "notes": a.get("notes", "")[:600], "ts": a["ts"]} for a in tried[-6:]]
            row["n_saves"], row["n_reports"] = len(saves), len(tried)
            row["active"] = max([row["ts"], *took.values(), *(w["ts"] for w in saves), *(a["ts"] for a in tried)])
            if ck is not None:
                row["check"] = st.status(ck.id, now)
                row["checker"], row["checker_agent"] = who(ck)
        return row

    for x in st.lines_for(t.id):
        piece = pieces.get(x["piece"]) if x["piece"] else None
        seen.add(x["piece"])
        lines.append(entry(piece, x))
    for piece in sorted(pieces.values(), key=lambda p: p.lamport):  # rounds launched by hand, from a spec
        if piece.attack.get("role") == "line" and piece.id not in seen:
            lines.append(entry(piece))
    key_of = {r.get("piece_id"): r["id"] for r in lines if r.get("piece_id")}
    findings = []
    for f in st.findings_for(t.id):
        ev = f["ev"]
        findings.append({"id": ev["id"], "cid": ev.get("cid", ""), "statement": ev["statement"],
                         "status": ev["status"], "kind": ev.get("kind", ""), "state": st.finding_state(f),
                         "solidity": solidity(f, st.independent(f)),
                         "independent": st.independent(f), "front": f["front"], "line": key_of.get(f["line"]),
                         "by": ev["name"], "agent": agent(ev["author"]), "evidence": ev.get("evidence", ""),
                         "from": list(ev.get("from") or []), "ts": ev["ts"],
                         "verdicts": [{"name": v["name"], "agent": agent(v["author"]), "verdict": v["verdict"],
                                       "reason": v["reason"]} for v in f["verdicts"].values()]})
    fronts = {}
    for r in lines:
        fr = fronts.setdefault(r["front"], {"front": r["front"], "methods": [], "lines": 0, "ran": 0, "confirmed": 0,
                                            "refuted": 0, "dead": 0, "unchecked": 0})
        fr["lines"] += 1
        fr["ran"] += r["state"] != "proposed"
        if r["method"] not in fr["methods"]:
            fr["methods"].append(r["method"])
    for f in findings:
        fr = fronts.setdefault(f["front"] or "-", {"front": f["front"] or "-", "methods": [], "lines": 0, "ran": 0,
                                                   "confirmed": 0, "refuted": 0, "dead": 0, "unchecked": 0})
        if f["status"] == "dead end":
            fr["dead"] += 1
        elif f["state"] in ("confirmed", "refuted"):
            fr[f["state"]] += 1
        else:
            fr["unchecked"] += 1
    rounds = [{"round": r["round"], "lines": len(r["lines"]), "confirmed": r["confirmed"], "closed": r["closed"],
               "progress": r["progress"], **{k: r[k] for k in S.ASSESSMENT}, "moved": r["closed"] and st.moved(r)}
              for r in st.research_rounds(t.id)]
    used = Counter(r["method"] for r in lines if r["state"] != "proposed")
    return {"task": t.key, "id": t.id, "title": t.title, "goal": t.id in st.goals,
            "text": (st.goals[t.id].get("text") or "") if t.id in st.goals else "",
            "dry": st.dry(t.id), "spent": st.spent(t.id), "fronts": list(fronts.values()), "lines": lines,
            "barriers": [{"name": b["barrier"], "statement": b["statement"], "from": list(b.get("from") or []),
                          "by": b["name"]} for b in st.barriers_for(t.id)],
            "findings": findings, "rounds": rounds, "methods": dict(used),
            "untried": [m for m in S.METHODS if m != "other" and not used[m]]}


# How much a finding that stands can carry, from what a checker's leaning can't fake: a Lean file it cites (the
# compiler judged it), a computation anyone reruns, a re-derivation by another owner's agent, or one agent's word
# on paper. Where checkers confirm nearly everything, a verdict alone no longer tells them apart.
SOLIDITY = ("Lean", "certified computation", "checked by another owner", "paper")
LEAN_FILE = re.compile(r"\.lean\b")


def solidity(f: dict, independent: bool) -> int:
    """Index in SOLIDITY of a finding: 0 the most solid."""
    ev = f["ev"]
    if ev["status"] == "proved" and LEAN_FILE.search(ev.get("evidence") or ""):
        return 0
    if ev["status"] == "checked by computation":
        return 1
    return 2 if independent else 3


def _cut(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def map_text(st: S.State, t: S.Task, width: int = 6000, now: float | None = None) -> str:
    """The map in words: what stands, what fell, what nobody checked, what failed, and where the fronts are. It
    is computed from the findings and their verdicts, so nobody has to rewrite it, and it can't drift from them."""
    d = map_data(st, t, now or time.time())
    if not d["findings"] and not d["lines"] and not st.barriers_for(t.id):
        return (f"(The map of {t.key} is empty: no findings and no lines yet. Its state of the art, if someone "
                f"wrote one: `python -m legion brief {t.key}`.)")
    per = max(160, min(700, width // max(8, len(d["findings"]) + len(d["fronts"]) + 4)))

    def row(f):
        checked = "; ".join(f"{v['name']}: {v['verdict']}" for v in f["verdicts"])
        return (f"  [{f['id'][:8]}] ({f['status']}; front {f['front'] or '-'}; by {f['by']}"
                + (f"; {checked}" if checked else "") + (", another owner's check" if f["independent"] else "")
                + f") {_cut(f['statement'], per)}" + (f" [{_cut(f['evidence'], 90)}]" if f["evidence"] else ""))

    real = [f for f in d["findings"] if f["status"] != "dead end"]
    stands = sorted((f for f in real if f["state"] == "confirmed"), key=lambda f: f["solidity"])
    parts = [("ESTABLISHED (confirmed by an agent other than its author), the most solid first: build on the first "
              "kinds before the last, since a claim on paper one agent confirmed can still be wrong",
              [dict(f, statement=f"[{SOLIDITY[f['solidity']]}] {f['statement']}") for f in stands]),
             ("REFUTED (don't build on these)", [f for f in real if f["state"] == "refuted"]),
             ("CONTESTED (confirmed by one checker, refuted by another: settle it)",
              [f for f in real if f["state"] == "contested"]),
             ("NOT CHECKED (their author's word only: check before you build on one)",
              [f for f in real if f["state"] == "unchecked"]),
             ("DEAD ENDS (what was tried and didn't work, and why. One nobody PROVED impossible is weak: the same "
              "target by another mechanism may still be worth a line)", [f for f in d["findings"] if f["status"] == "dead end"])]
    out = [f"THE MAP OF {t.key} (`python -m legion map {t.key}` prints it whole):"]
    # Where the goal stands, by the latest round whose synthesis said: what is ruled out, what is left, what next.
    said = next((r for r in reversed(d["rounds"]) if r["progress"] is not None), None)
    if said and any(said[k] for k in ("eliminated", "open", "next")):
        out.append(f"WHERE IT STANDS (round {said['round']}'s synthesis; the goal "
                   + ("moved" if said["progress"] else "didn't move") + "):")
        out += [f"  {label}: {_cut(said[k], 700)}" for k, label in
                (("eliminated", "ELIMINATED"), ("open", "WHERE THE ANSWER CAN STILL BE"), ("next", "NEXT"))
                if said[k]]
    bars = barriers_text(st, t, max(1500, width // 4))
    if bars:
        out.append(bars)
    for title, fs in parts:
        if fs:
            out.append(f"{title}:")
            out += [row(f) for f in fs]
    if d["fronts"]:
        out.append("FRONTS (directions tried so far):")
        for fr in sorted(d["fronts"], key=lambda fr: -fr["lines"]):
            last = next((r for r in reversed(d["lines"]) if r["front"] == fr["front"]), None)
            out.append(f"  {fr['front']} ({', '.join(fr['methods']) or '-'}): {fr['ran']} line(s) run, "
                       f"{fr['confirmed']} confirmed, {fr['refuted']} refuted, {fr['dead']} dead end(s)"
                       + (f"; latest: {_cut(last['title'], 80)} [{last['state']}]" if last else ""))
    if d["untried"]:
        out.append("METHODS nobody has run a line by here: " + ", ".join(d["untried"]) + ".")
    if d["rounds"]:
        out.append("ROUNDS: " + ", ".join(f"{r['round']} ({r['lines']} lines, {r['confirmed']} confirmed"
                                           + ("" if r["closed"] else ", under way")
                                           + {True: ", moved the goal", False: ", goal unmoved"}.get(r["progress"], "")
                                           + ")" for r in d["rounds"])
                   + (f". The last {d['dry']} closed round(s) didn't move the goal." if d["dry"] else "."))
    text = "\n".join(out)
    return text if len(text) <= width else text[:width - 60].rstrip() + f"\n… (cut: `python -m legion map {t.key}`)"


# --- what claim hands out ------------------------------------------------------------------------------------------

def _hand(c: Ctx, args, kind: str, t: S.Task, text: str) -> int:
    from .cli import out, verify_first
    out(args, {"won": False, "task": None, "work": {"kind": kind, "task": t.key}},
        verify_first(c) + text + "\nThen claim again.")
    return 7


def _goal_words(st: S.State, t: S.Task, width: int) -> str:
    words = (st.goals[t.id].get("text") or "").strip() if t.id in st.goals else ""
    return words if len(words) <= width else words[:width - 1].rstrip() + "…"


def propose_text(st: S.State, t: S.Task, name: str, mine: int) -> str:
    move, dry = move_for(st, t.id, name), st.dry(t.id)
    return (f"{t.key} is a research goal: an open problem nobody closes in one session, so it isn't handed out "
            "whole. It moves in rounds of lines that its agents propose, explore and check (`python -m legion help "
            f"research`). Propose ONE line for the next round"
            + (f" (you have {mine} waiting; you may hold {S.MAX_PROPOSALS})" if mine else "") + ".\n\n"
            # The goal's own words: what it's after and what counts as progress. Shown only in the bodies of rounds
            # launched after them, a goal reopened with new words reached nobody proposing lines for it.
            + (f"THE GOAL, in the words it was opened with:\n{_goal_words(st, t, 3000)}\n\n" if t.id in st.goals else "")
            + f"YOUR MOVE: {move}: {MOVE_TEXT[move]}.\nEach agent of a round is handed a different move, so the round "
            "doesn't make one move several times. Make this one if you can; if it truly can't be made here, say why "
            "in your line and make another.\n"
            + (f"The last {dry} rounds didn't move the goal (nothing confirmed, or nothing that counts as progress "
               "by its text). That is the sign of going round the same ideas: this round's moves are only those "
               "that change the question.\n" if dry >= S.DRY_REFRAME else "")
            + "Decide your line BEFORE you look at the proposals of others: they aren't shown to you until you "
            "have yours, because agents shown each other's ideas first converge on them.\n\n"
            + map_text(st, t, 7000) + "\n\n"
            "A line is one agent's work for one session (about 80 steps) with a definite deliverable: something "
            "decided, proved, computed or refuted. Not \"explore X\". Say:\n"
            "  --front    its direction: one of the fronts above, or a new one (a short key, like sat-small-n)\n"
            f"  --method   one of {', '.join(S.METHODS)}\n"
            "  --title, --file FILE   exactly what to do, what to read first, what counts as a result, and where "
            f"you expect it to get hard (the file: at most {S.MAX_LINE} characters; longer is refused)\n"
            "  --learn    what the project learns if it FAILS: a line that teaches nothing when it fails isn't "
            f"worth a round (at most {S.MAX_FINDING} characters, as --differs)\n"
            "  --differs  on a front that has lines already: what yours does that they didn't (required there)\n"
            + (f"  --escapes  which of the goal's barriers your line could fall into, and why it doesn't "
               f"(required here: the goal has barriers; {S.MIN_ESCAPES} characters or more)\n"
               if st.barriers_for(t.id) else "")
            + "  --deep     one approach to work on over several sessions, its kill test first (at most "
            f"{S.MAX_DEEP} on a goal): for an idea that needs time, not for a computation\n"
            "  --needs    programs its work takes (lake, kissat...), so it goes to a machine that has them\n"
            "  --from     ids of the findings it builds on\n"
            f"`python -m legion line propose {t.key} --front F --method M --title \"...\" --file line.txt "
            "--learn \"...\"`\n")


def back_text(st: S.State, t: S.Task, others: list[dict]) -> str:
    return (f"Lines proposed for the next round on {t.key}, by other agents. Back the ones you would run (one say "
            "an owner; the round takes the fronts least explored first, then the best backed), and say what's wrong "
            "with one that rests on something false: `python -m legion note " + t.key + " \"...\"`.\n\n"
            + "\n".join(line_row(x, 400) for x in others[:8])
            + "\n\n`python -m legion line back <id>`")


def judge_text(st: S.State, f: dict) -> str:
    ev, t = f["ev"], st.tasks[f["task"]]
    said = "; ".join(f"{v['name']}: {v['verdict']} ({_cut(v['reason'], 160)})" for v in f["verdicts"].values())
    return (f"A finding on the map of {t.key} that nobody but its author stands behind yet. On a map, a finding "
            "stands once an agent other than its author confirms it, and the next round's lines are built on what "
            "stands. Judge this one:\n\n"
            f"  [{ev['id'][:8]}] ({ev['status']}; front {f['front'] or '-'}; stated by {ev['name']})\n"
            f"  {ev['statement']}\n"
            + (f"  evidence: {ev['evidence']}\n" if ev.get("evidence") else "")
            + (f"  how to check: {ev['check']}\n" if ev.get("check") else "")
            + (f"  said so far: {said}\n" if said else "")
            + "\nYOUR JOB IS TO FIND WHAT IS WRONG WITH IT. Don't take its word. If it rests on a patch in main, "
            "read the statement that patch proves (the file it names, in main) and say whether it is THIS statement, "
            "no stronger. If it is an argument, re-derive it and look for the gap. If it is a computation, recompute "
            "it with code of your own when you can. If it is a dead end, say whether what failed was the route or "
            "one attempt at it: a dead end refuted is a route open again. About 30 steps, no more.\n"
            "  confirmed       you checked it yourself, and it holds as stated\n"
            "  refuted         with the concrete error, the counterexample, or what it overstates\n"
            "  \"not verified\"  you couldn't settle it (a tool this machine lacks, a proof you can't follow): say "
            "what stopped you; it goes to another agent\n"
            f"`python -m legion finding judge {ev['id'][:8]} <verdict> --reason \"what you checked and found\"` "
            f"(the reason: at most {S.MAX_FINDING} characters)\n")


def line_row(x: dict, width: int = 240) -> str:
    ev = x["ev"]
    return (f"  [{ev['id'][:8]}] {ev['front']} / {ev['method']} / {ev['move']}, by {ev['name']}"
            + (f", backed by {', '.join(sorted(x['backed']))}" if x["backed"] else "")
            + f": {ev['title']}. {_cut(ev['text'], width)}"
            + (f"\n      differs: {_cut(ev['differs'], 200)}" if ev.get("differs", "").strip() else "")
            + f"\n      if it fails: {_cut(ev['learn'], 200)}")


def open_goals(st: S.State, me: str) -> list[S.Task]:
    """The research goals still open, in an order of this agent's own, so they don't all start on the same one."""
    return sorted((st.tasks[g] for g in st.goals if not st.tasks[g].cancelled and not st.done(g)
                   and not st.refuted(g)), key=lambda t: hashlib.sha256((me + t.id).encode()).hexdigest())


def launch_due(c: Ctx, now: float) -> bool:
    """Launch the rounds that are due, if this agent may: True if it launched one."""
    made = False
    for t in open_goals(c.st, c.me):
        if c.st.ideas_due(t.id) and may_launch(c.st, t, c.me, now):
            try:
                rnd = launch_ideas(c, t, now)
            except Fail as e:
                print(f"legion: the round of ideas due on {t.key} wasn't launched here: {e}", flush=True)
            else:
                print(f"legion: round {rnd} on {t.key} launched: a round of ideas, its rounds having stopped "
                      "moving it.", flush=True)
                made = True
                continue
        chosen = due(c.st, t.id, now)
        if chosen and may_launch(c.st, t, c.me, now):
            try:
                rnd = launch(c, t, chosen, now)
            except Fail as e:  # another agent's launch got there first, or the goal's zone has no folder for it
                print(f"legion: the round due on {t.key} wasn't launched here: {e}", flush=True)
                continue
            print(f"legion: round {rnd} on {t.key} launched from {len(chosen)} proposed line(s).", flush=True)
            made = True
    return made


def cycle_work(c: Ctx, args, now: float) -> int | None:
    """What the research goals want from an agent with no task to take: a round launched (RELAUNCH: claim looks
    again), a line proposed, others' lines backed. None if they want nothing from it now."""
    st, name = c.st, c.ident.name
    goals = [t for t in open_goals(st, c.me) if not st.spent(t.id)]
    if launch_due(c, now):  # a spent goal too: a round of ideas may be due on it
        return RELAUNCH
    for t in goals:
        waiting = st.open_lines(t.id)
        # With three rounds' worth of lines waiting already, more proposals aren't what the goal lacks.
        if not any(x["ev"]["author"] == c.me for x in waiting) and len(waiting) < 3 * MAX_ROUND_LINES:
            return _hand(c, args, "line", t, propose_text(st, t, name, 0))
    f = st.finding_for(c.me, name)  # its own line first, so it's decided before it reads much; then others' findings
    if f is not None:
        return _hand(c, args, "judge", st.tasks[f["task"]], judge_text(st, f))
    me = S.owner(name)
    for t in goals:
        others = [x for x in st.open_lines(t.id) if st.owner_now(x["ev"]) != me and me not in x["backed"]]
        if others:
            return _hand(c, args, "back", t, back_text(st, t, others))
    return None


def waiting_text(st: S.State, me: str, now: float) -> str:
    """What an agent with nothing to take is told of the research goals: what they wait for, or that they're spent."""
    out = []
    for t in open_goals(st, me):
        if st.spent(t.id):
            out.append(f"{t.key} is spent: its last {st.dry(t.id)} rounds didn't move it, so no more are launched "
                       "on it until someone brings something new and reopens it (`python -m legion goal "
                       f"{t.key} --text \"...\"`: a founder, or a trusted agent on it). Don't go round it.")
        else:
            props = st.open_lines(t.id)
            if props:
                left = PROPOSE_WINDOW - (now - min(p["ev"]["ts"] for p in props))
                out.append(f"{t.key} has {len(props)} line(s) proposed for its next round"
                           + (f", launched in about {max(1, left / 60):.0f} min or as soon as enough agents propose"
                              if left > 0 and not free_lines(st, t.id, now) else "") + ".")
    return ("\n".join(out) + "\n") if out else ""


# --- from a line's report to the map -------------------------------------------------------------------------------

def _load(repo, path: str, ref: str | None = None):
    """A JSON file of the working tree, or, with ref, as that commit has it."""
    try:
        if ref:
            from . import coordgit
            p = coordgit.run(repo, "show", f"{ref}:{path}")
            return json.loads(p.stdout.decode("utf-8")) if p.returncode == 0 else None
        return json.loads((repo / path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lift(c: Ctx, t: S.Task, ref: str | None = None) -> int:
    """Record on the task's map what a round's piece just submitted says: a line's claims and dead ends as
    findings, a check's verdicts on them. How many events it recorded. Never the reason a submit fails. With ref,
    the piece's files as that commit has them (relift)."""
    role = t.attack.get("role")
    parent = c.st.tasks.get(t.parent)
    if role == "synthesis" and parent is not None and t.zone and ref is None:
        d = t.zone[0].rstrip("/")
        return assess(c, parent, t, d) + propose_next(c, parent, d) + take_barriers(c, parent, d)
    if role not in ("line", "check") or parent is None or not t.zone or t.attack.get("kind") == "idea":
        return 0
    n, d = 0, t.zone[0].rstrip("/")
    try:
        if role == "line":
            report = _load(c.repo, f"{d}/{A.REPORT}", ref)
            if not isinstance(report, dict):
                return 0
            mine = {f["ev"].get("cid"): f for f in c.st.findings_for(parent.id, t.id) if f["ev"]["author"] == c.me}
            rows = [(str(x.get("id", ""))[:60], x) for x in report.get("claims") or [] if isinstance(x, dict)]
            dead = str(report.get("dead_ends") or "").strip()
            if len(dead) > 20:
                rows.append(("dead-ends", {"statement": dead, "kind": "dead end", "status": "dead end",
                                           "evidence": f"{d}/{A.REPORT}", "how_to_check": ""}))
            for cid, x in rows:
                statement = _cut(str(x.get("statement") or ""), S.MAX_FINDING)
                status = x.get("status") if x.get("status") in S.FINDING_STATUS else "unproved"
                old = mine.get(cid)
                if not statement or old and old["ev"]["statement"] == statement and old["ev"]["status"] == status:
                    continue
                c.emit("finding.stated", task=parent.id, line=t.id, cid=cid, statement=statement, status=status,
                       kind=str(x.get("kind") or "finding")[:30], evidence=_cut(str(x.get("evidence") or ""), S.MAX_FINDING),
                       check=_cut(str(x.get("how_to_check") or ""), S.MAX_FINDING),
                       **({"replaces": old["ev"]["id"]} if old else {}))
                n += 1
        else:
            line = next((c.st.tasks[a] for a in t.apart if a in c.st.tasks), None)
            verdicts = _load(c.repo, f"{d}/{A.VERDICTS_FILE}", ref)
            if line is None or not isinstance(verdicts, dict):
                return 0
            fs = {f["ev"].get("cid"): f for f in c.st.findings_for(parent.id, line.id)}
            for v in verdicts.get("verdicts") or []:
                f = fs.get(str(v.get("claim_id", ""))[:60]) if isinstance(v, dict) else None
                if f is None or f["ev"]["author"] == c.me or v.get("verdict") not in S.VERDICTS:
                    continue
                c.emit("finding.judged", task=parent.id, finding=f["ev"]["id"], verdict=v["verdict"],
                       reason=_cut(str(v.get("reason") or "-"), S.MAX_FINDING))
                n += 1
    except Fail as e:
        print(f"legion: the map of {parent.key} didn't take all of it ({e}); the patch is submitted all the same.",
              flush=True)
    return n


RELIFT_DAYS = 3


def relift(c: Ctx, now: float) -> int:
    """Record what this agent's own round pieces say that never reached the map: a line of its with a patch and
    no finding of its on the map, a check of its with no verdict of its on its line's findings, read from the
    patch's commit. Their findings were refused at submit (a daily cap counted them, until it no longer did) and
    nothing ever lifted them again: in Erdős #902 the lines on f(4), f(5) and 9/16 went in with empty maps, their
    checks had nothing to judge, and no round could count as moving its goal. How many events it recorded."""
    from . import coordgit
    st, n = c.st, 0
    for t in [t for t in st.tasks.values() if t.attack.get("role") in ("line", "check") and t.parent in st.goals
              and t.attack.get("kind") != "idea"]:
        mine = [p for p in st.patches_for(t.id) if p.author == c.me and p.verdict != "rejected"
                and now - p.ts < RELIFT_DAYS * 86400]
        if not mine:
            continue
        if t.attack["role"] == "line":
            if any(f["ev"]["author"] == c.me for f in st.findings_for(t.parent, t.id)):
                continue
        else:
            line = next((st.tasks[a] for a in t.apart if a in st.tasks), None)
            fs = st.findings_for(t.parent, line.id) if line is not None else []
            if not fs or any(c.me in f["verdicts"] for f in fs):
                continue
        ref = next((p.commit for p in reversed(mine) if coordgit.has_commit(c.repo, p.commit)), None)
        if ref is None and any(p.verdict == "accepted" for p in mine):
            ref = coordgit.base_ref(c.repo)
        if ref:
            got = lift(c, t, ref)
            st = c.st
            if got:
                print(f"legion: {got} finding(s) or verdict(s) of your {t.key} recorded on the map: they hadn't "
                      "reached it when you submitted.", flush=True)
            n += got
    return n


def assess(c: Ctx, goal: S.Task, t: S.Task, d: str) -> int:
    """What a round's synthesis says of whether the round moved its goal (PROGRESS in its folder), recorded and
    said. Without it the round counts as not having moved the goal (State.moved). Returns 0: what submit counts
    for a synthesis is the lines it proposed."""
    said = _load(c.repo, f"{d}/{PROGRESS}")
    if not isinstance(said, dict) or not isinstance(said.get("progress"), bool):
        if t.attack.get("assess"):
            print(f"legion: {d}/{PROGRESS} is missing or has no true/false progress: this round counts as not having "
                  f"moved {goal.key}.", flush=True)
        return 0
    try:
        c.emit("round.assessed", task=goal.id, round=t.attack.get("round", ""), progress=said["progress"],
               **{k: _cut(str(said.get(k) or ""), S.MAX_FINDING) for k in S.ASSESSMENT if said.get(k)})
    except Fail as e:
        print(f"legion: the round's assessment wasn't recorded: {e}", flush=True)
        return 0
    print(f"legion: round {t.attack.get('round', '')} recorded as {'having moved' if said['progress'] else 'not moving'} "
          f"{goal.key}.", flush=True)
    return 0


def take_barriers(c: Ctx, goal: S.Task, d: str) -> int:
    """The barriers a round's synthesis says it found (BARRIERS in its folder), as the goal's. How many went in."""
    if goal.id not in c.st.goals:
        return 0
    rows = _load(c.repo, f"{d}/{BARRIERS}")
    if not isinstance(rows, list):
        return 0
    n = 0
    for x in rows[:6]:
        if not isinstance(x, dict):
            continue
        name = re.sub(r"[^a-z0-9-]+", "-", str(x.get("name", "")).lower()).strip("-")[:40]
        try:
            c.emit("goal.barrier", task=goal.id, barrier=name, statement=_cut(str(x.get("statement") or ""), S.MAX_FINDING),
                   **({"from": [f for f in x["from"] if f in c.st.findings][:8]} if isinstance(x.get("from"), list)
                      else {}))
            n += 1
        except Fail as e:
            print(f"legion: a barrier of {d}/{BARRIERS} wasn't recorded: {e}", flush=True)
    if n:
        print(f"legion: {n} barrier(s) of {goal.key} recorded: every later line says why it escapes them.", flush=True)
    return n


def propose_next(c: Ctx, goal: S.Task, d: str) -> int:
    """The lines a round's synthesis proposes for the next round (NEXT_LINES in its folder), as its agent's own.
    How many went in. Its agent knows the round best: in Erdős #902 the syntheses named next steps that nobody
    proposed, and the next rounds were made of one line each."""
    if goal.id not in c.st.goals:
        return 0
    rows = _load(c.repo, f"{d}/{NEXT_LINES}")
    if not isinstance(rows, list):
        return 0
    n = 0
    for x in rows[:S.MAX_PROPOSALS]:
        if not isinstance(x, dict):
            continue
        try:
            c.emit("line.proposed", task=goal.id, front=str(x.get("front", "")), method=str(x.get("method", "")),
                   move=x.get("move") if x.get("move") in S.MOVES else move_for(c.st, goal.id, c.ident.name),
                   title=str(x.get("title", "")).strip(), text=str(x.get("text", "")).strip(),
                   learn=str(x.get("learn", "")).strip(),
                   **({"differs": str(x["differs"]).strip()} if x.get("differs") else {}),
                   **({"escapes": str(x["escapes"]).strip()} if x.get("escapes") else {}),
                   **({"deep": True} if x.get("deep") is True else {}))
            n += 1
        except Fail as e:
            print(f"legion: a line of {d}/{NEXT_LINES} wasn't proposed: {e}", flush=True)
    return n


def ask_for_line(c: Ctx, t: S.Task, now: float) -> str:
    """After a round's line or check is submitted: if the goal's next round would take more lines, and its agent
    has none waiting there, the ask to propose one now, when it knows that front best. "" otherwise."""
    goal = c.st.tasks.get(t.parent)
    if (goal is None or goal.id not in c.st.goals or c.st.spent(goal.id) or t.attack.get("role") not in ("line", "check")
            or t.attack.get("kind") == "idea"):
        return ""
    if any(x["ev"]["author"] == c.me for x in c.st.open_lines(goal.id)) or not wanted(c.st, goal.id, now):
        return ""
    move = move_for(c.st, goal.id, c.ident.name)
    return (f"The next round of {goal.key} has {len(c.st.open_lines(goal.id))} line(s) proposed and would take "
            f"{wanted(c.st, goal.id, now)} more. You know this front best right now: propose one, with the move "
            f"{move} ({MOVE_TEXT[move]}). `python -m legion line propose {goal.key} --front F --method M --title "
            f"\"...\" --file FILE --learn \"...\"` (`help research`). The map: `python -m legion map {goal.key}`.")


# --- the commands ----------------------------------------------------------------------------------------------------

def _find(pool: dict, ref: str, what: str) -> dict:
    hits = [x for k, x in pool.items() if k.startswith(ref)]
    if len(hits) != 1:
        raise Fail(f"there is no {what} {ref}" if not hits else f"{ref} is the start of several {what}s: give more of it", 3)
    return hits[0]


def _text(args) -> str:
    if getattr(args, "file", None):
        try:
            return open(args.file, encoding="utf-8").read().strip()
        except OSError as e:
            raise Fail(f"can't read {args.file}: {e}", 2)
    return (getattr(args, "text", None) or "").strip()


def cmd_goal(args):
    from .cli import out
    c = Ctx(sync=not args.no_sync)
    now = time.time()
    if not args.task:
        rows = []
        for t in open_goals(c.st, ""):
            d = map_data(c.st, t, now)
            rows.append({"task": t.key, "rounds": len(d["rounds"]), "dry": d["dry"], "spent": d["spent"],
                         "proposed": len(c.st.open_lines(t.id)), "findings": len(d["findings"]),
                         "confirmed": sum(f["state"] == "confirmed" for f in d["findings"])})
        out(args, {"goals": rows},
            "\n".join(f"  {r['task']:<32} {r['rounds']} round(s), {r['confirmed']} of {r['findings']} findings "
                      f"confirmed, {r['proposed']} line(s) proposed"
                      + (", SPENT" if r["spent"] else f", {r['dry']} dry round(s)" if r["dry"] else "") for r in rows)
            or "No task is a research goal. `python -m legion goal <task>` opens one (`help research`).")
        return 0
    t = c.task(args.task)
    text = _text(args)
    c.emit("goal.opened", task=t.id, **({"text": text} if text else {}), **({"closed": True} if args.close else {}))
    c.sync()
    print(f"{t.key} is no longer a research goal: claim hands it out like any task." if args.close else
          f"{t.key} is a research goal: claim no longer hands it out whole. Its agents propose lines (`line "
          f"propose`), rounds are made from them, and what they find is its map (`map {t.key}`).")
    return 0


def cmd_line(args):
    from .cli import out
    c = Ctx(sync=not args.no_sync)
    now = time.time()
    if args.action == "back":
        x = _find(c.st.lines, args.ref, "line")
        c.emit("line.backed", task=x["task"], line=x["ev"]["id"])
        c.sync()
        print(f"You back line {x['ev']['id'][:8]} ({x['ev']['title']}).")
        return 0
    t = c.task(args.ref)
    if args.action == "list":
        mine = any(x["ev"]["author"] == c.me for x in c.st.lines_for(t.id))
        rows = [x for x in c.st.open_lines(t.id) if args.all or mine or x["ev"]["author"] == c.me]
        hidden = len(c.st.open_lines(t.id)) - len(rows)
        out(args, {"task": t.key, "lines": [x["ev"] for x in rows], "hidden": hidden},
            (f"Lines proposed for the next round on {t.key}:\n" + "\n".join(line_row(x) for x in rows) if rows
             else f"No line is proposed on {t.key}.")
            + (f"\n({hidden} by other agents aren't shown until you have proposed yours: decide it first. --all "
               "shows them.)" if hidden else ""))
        return 0
    text = _text(args)
    if not (args.front and args.method and args.title and text and args.learn):
        raise Fail("a line needs --front, --method, --title, its text (--file FILE or --text) and --learn "
                   "(`python -m legion help research`)", 2)
    frm = [_find(c.st.findings, x, "finding")["ev"]["id"] for x in args.frm or []]
    ev = c.emit("line.proposed", task=t.id, front=args.front, method=args.method,
                move=args.move or move_for(c.st, t.id, c.ident.name), title=args.title.strip(), text=text,
                learn=args.learn.strip(), **({"differs": args.differs.strip()} if args.differs else {}),
                **({"escapes": args.escapes.strip()} if args.escapes else {}), **({"deep": True} if args.deep else {}),
                **({"needs": args.needs} if args.needs else {}), **({"from": frm} if frm else {}))
    made = launch_due(c, now)
    c.sync()
    others = [x for x in c.st.open_lines(t.id) if x["ev"]["author"] != c.me]
    print(f"Line {ev['id'][:8]} proposed on {t.key}, front {args.front}."
          + ("" if made else " " + waiting_text(c.st, c.me, time.time()).strip())
          + (f"\nNow the others' ({len(others)}): `python -m legion line list {t.key}`, and back the ones you'd run."
             if others else "") + "\nThen claim again: the round's lines are handed out by `claim`.")
    return 0


def cmd_finding(args):
    c = Ctx(sync=not args.no_sync)
    if args.action == "judge":
        f = _find(c.st.findings, args.ref, "finding")
        if args.verdict not in S.VERDICTS or not args.reason:
            raise Fail(f"`finding judge <id> <{'|'.join(S.VERDICTS)}> --reason \"what you checked yourself\"`", 2)
        c.emit("finding.judged", task=f["task"], finding=f["ev"]["id"], verdict=args.verdict, reason=args.reason.strip())
        c.sync()
        f = c.st.findings[f["ev"]["id"]]  # as it is with this verdict on it
        state = c.st.finding_state(f)
        print(f"Your verdict on {f['ev']['id'][:8]}: {args.verdict}. The finding is now {state}"
              + (": it stands on the map, confirmed by another owner's agent." if state == "confirmed" and c.st.independent(f)
                 else ": it stands on the map; an agent of another owner than its author's is still asked for its say."
                 if state == "confirmed" else ": it goes to another agent." if state == "unchecked"
                 else ": two agents disagree, and more are asked." if state == "contested" else "."))
        return 0
    t = c.task(args.ref)
    statement = (args.verdict or "").strip() or _text(args)  # the second positional is the statement here
    if not statement or not args.status:
        raise Fail(f"`finding add <task> \"the precise statement\" --status <{'|'.join(S.FINDING_STATUS)}> "
                   "[--evidence where] [--check how] [--front F] [--kind K]`", 2)
    line = c.task(args.line).id if args.line else None
    frm = [_find(c.st.findings, x, "finding")["ev"]["id"] for x in args.frm or []]
    ev = c.emit("finding.stated", task=t.id, statement=statement, status=args.status, kind=args.kind or "finding",
                **{k: v for k, v in (("evidence", args.evidence), ("check", args.check), ("front", args.front),
                                     ("line", line)) if v}, **({"from": frm} if frm else {}))
    c.sync()
    print(f"Finding {ev['id'][:8]} is on the map of {t.key}, unchecked until an agent other than you judges it "
          f"(`python -m legion finding judge {ev['id'][:8]} confirmed --reason \"...\"`).")
    return 0


def cmd_map(args):
    from .cli import out
    c = Ctx(sync=not args.no_sync)
    t = c.task(args.task)
    d = map_data(c.st, t, time.time())
    out(args, d, map_text(c.st, t, 10**6)
        + ("\n" + waiting_text(c.st, c.me, time.time()) if t.id in c.st.goals else ""))
    return 0


def cmd_round(args):
    c = Ctx(sync=not args.no_sync)
    t, now = c.task(args.task), time.time()
    if args.ideas:
        if t.id not in c.st.goals:
            raise Fail(f"{t.key} isn't a research goal", 3)
        if not may_launch(c.st, t, c.me, now):
            raise Fail(f"a round is launched by a founder or a trusted agent that works on {t.key}", 3)
        if args.dry_run:
            spec = idea_spec(c.st, t, now)
            print(f"A round of ideas on {t.key} would hand out {len(spec['lines'])} field(s): "
                  + ", ".join(x["field"] for x in spec["lines"]) + ".\n\nWhat its agents are shown:\n" + spec["common"])
            return 0
        rnd = launch_ideas(c, t, now)
        print(f"Round {rnd} on {t.key}: a round of ideas, each from a field of its own and judged by another agent, "
              f"then a synthesis that makes deep lines of the best. `python -m legion attack status {t.key}`.")
        return 0
    props = c.st.open_lines(t.id)
    if not props:
        raise Fail(f"no line is proposed on {t.key}: `python -m legion line propose {t.key} ...`", 3)
    if not may_launch(c.st, t, c.me, now):
        raise Fail(f"a round is launched by a founder or a trusted agent that works on {t.key}", 3)
    chosen = select(c.st, t.id, props, args.lines or round_size(c.st, now))
    if args.dry_run:
        print(f"The round on {t.key} would take {len(chosen)} of {len(props)} proposed line(s):\n"
              + "\n".join(line_row(x) for x in chosen))
        return 0
    rnd = launch(c, t, chosen, now)
    print(f"Round {rnd} on {t.key}: {len(chosen)} line(s), each with its check, and a synthesis. `claim` hands "
          f"them out; `python -m legion attack status {t.key}` follows it.")
    return 0


def cmd_barrier(args):
    from .cli import out
    c = Ctx(sync=not args.no_sync)
    t = c.task(args.task)
    if args.retire:
        c.emit("goal.barrier", task=t.id, barrier=args.retire, retired=True)
        c.sync()
        print(f"The barrier {args.retire} of {t.key} is retired.")
        return 0
    if args.name:
        statement = (args.statement or "").strip() or _text(args)
        frm = [_find(c.st.findings, x, "finding")["ev"]["id"] for x in args.frm or []]
        c.emit("goal.barrier", task=t.id, barrier=args.name, statement=statement, **({"from": frm} if frm else {}))
        c.sync()
        print(f"{t.key} has the barrier {args.name}: every line proposed on it from now on says why it escapes it.")
        return 0
    bars = c.st.barriers_for(t.id)
    out(args, {"task": t.key, "barriers": bars},
        barriers_text(c.st, t, 10**6) or f"{t.key} has no barriers named. `python -m legion barrier {t.key} NAME "
                                         "\"what doesn't work, and why\" --from <findings>` names one.")
    return 0


def add_parser(sub) -> None:
    s = sub.add_parser("goal", help="research goals: open problems worked on in rounds of proposed lines, never "
                                    "handed out whole (`help research`)")
    s.add_argument("task", nargs="?", help="open this task as a research goal (none: list them)")
    s.add_argument("--text", help="what counts as progress on it; or, on a spent goal, what is new")
    s.add_argument("--file")
    s.add_argument("--close", action="store_true", help="it's no longer a research goal")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_goal)
    s = sub.add_parser("line", help="propose a line of research for a goal's next round, back others', list them")
    s.add_argument("action", choices=["propose", "back", "list"])
    s.add_argument("ref", help="propose, list: the goal; back: the line's id")
    s.add_argument("--front", help="its direction, a short key: an existing front or a new one")
    s.add_argument("--method", choices=S.METHODS)
    s.add_argument("--move", choices=S.MOVES, help="default: the move claim handed you")
    s.add_argument("--title")
    s.add_argument("--text")
    s.add_argument("--file", help="the line's text: what to do, what counts as a result, where it gets hard")
    s.add_argument("--learn", help="what the project learns if the line fails")
    s.add_argument("--differs", help="what it does that earlier lines on its front didn't")
    s.add_argument("--escapes", help="which of the goal's barriers it could fall into, and why it doesn't")
    s.add_argument("--deep", action="store_true", help="one approach worked on over several sessions, kill test first")
    s.add_argument("--needs", nargs="*", help="programs its work takes on the machine")
    s.add_argument("--from", dest="frm", nargs="*", help="findings it builds on")
    s.add_argument("--all", action="store_true", help="list: the others' too, before you have proposed")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_line)
    s = sub.add_parser("finding", help="put a finding on a task's map, or judge another agent's")
    s.add_argument("action", choices=["add", "judge"])
    s.add_argument("ref", help="add: the task; judge: the finding's id")
    s.add_argument("verdict", nargs="?", help="add: the statement; judge: confirmed, refuted or \"not verified\"")
    s.add_argument("--status", choices=S.FINDING_STATUS)
    s.add_argument("--kind")
    s.add_argument("--evidence", help="where its proof or data is (a path in main, a save)")
    s.add_argument("--check", help="how another agent can confirm or refute it")
    s.add_argument("--front")
    s.add_argument("--line", help="the round's line it came from")
    s.add_argument("--from", dest="frm", nargs="*", help="findings it rests on")
    s.add_argument("--reason", help="judge: what you checked yourself and what you found")
    s.add_argument("--text")
    s.add_argument("--file")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_finding)
    s = sub.add_parser("map", help="a task's map: what stands, what fell, what failed, its fronts and rounds")
    s.add_argument("task")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_map)
    s = sub.add_parser("round", help="launch a goal's next round now, from the lines proposed; or a round of ideas")
    s.add_argument("task")
    s.add_argument("--ideas", action="store_true", help="a round of ideas: each agent, shown only the goal and its "
                                                        "barriers, proposes an approach from a field it is handed")
    s.add_argument("--lines", type=int, help="how many lines (default: by the agents active)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_round)
    s = sub.add_parser("barrier", help="a research goal's barriers: what is known not to work on it")
    s.add_argument("task", help="the goal")
    s.add_argument("name", nargs="?", help="name a barrier (a short key): with its statement")
    s.add_argument("statement", nargs="?", help="which family of arguments or objects doesn't work, and what stops it")
    s.add_argument("--from", dest="frm", nargs="*", help="the findings it rests on")
    s.add_argument("--retire", metavar="NAME", help="it turned out not to bar what it said")
    s.add_argument("--file")
    s.add_argument("--text")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_barrier)
