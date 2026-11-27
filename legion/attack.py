"""legion attack: a round of research on a hard task, run the way a lead runs subagents, across agents and machines.

A lead (a trusted agent that works on the task) launches a round from a spec: what the round is for, the text every
agent shares (the problem, what's known, the rules) and a few lines of attack. Each piece becomes a subtask:

- a LINE per direction: an agent explores it in a folder of its own and closes it with a report of precise claims,
  each with its status, its evidence and how to check it;
- the CHECK of each line, open the moment its line is in, and taken by an agent that didn't write the line
  (Task.apart): it tries to refute every claim, re-deriving each one, and gives each a verdict;
- the SYNTHESIS, open once every check is in: one consolidated report, and the task's new state of the art.

There is no barrier between a line and its check: lines finish at different times, and each check starts when its
own line lands. Each piece carries a contract (state.CONTRACT_DIR) that checks its report's form, so a piece can't
be accepted without its report; and the task itself is still done only by its own acceptance command. The lead
waits (`wait`), reads the synthesis and the digest (`attack digest`), checks the key claims itself, and launches the
next round from the next steps.
"""

import json
import re
import time
from pathlib import Path

from . import coordgit
from . import state as S
from .ctx import Ctx, Fail

MAX_LINES = 12
MAX_COMMON, MAX_TEXT = 20_000, 6_000  # characters: every piece's description carries the common text
STATUSES = ("proved", "checked by computation", "partial", "unproved")
KINDS = ("theorem", "computation", "construction", "barrier", "conjecture", "finding")
VERDICTS = ("confirmed", "refuted", "not verified")
BUDGET = {"line": 80, "check": 50}  # steps (tool calls) an agent aims to finish in, so the piece does report
REPORT, VERDICTS_FILE, SYNTHESIS = "report.json", "verdicts.json", "report.md"
ROUND = re.compile(r"^r(\d+)$")

RIGOR = """RULES OF EVIDENCE. Be rigorous and honest. A proof must be complete, or be labelled partial with the gap named. \
A computation must be reproducible: keep the script in your folder and say how to run it. Never state as proved \
something you only believe. A clear negative result (this approach can't work, and why) is a good outcome. Nothing \
counts as new if it is a known result in disguise: say what is known when you know it."""

LINE = """ATTACK {round} ON {parent}, LINE {line} ({front}): {title}
You are one of several agents working in parallel on {parent}, each on a line of its own (the other lines of this \
round are subtasks of {parent} too). Work alone on this one. Another agent, not you, will check every claim you \
report, in {key}-check.

WHAT THE ROUND IS FOR: {goal}

{common}

YOUR LINE: {task}

WHERE. Everything you write goes in {dir}/ (your zone): scripts, data, logs, notes.txt. Commit files of at most a \
few MB; leave big intermediate files (proof logs, dumps) out of the commit.

HOW. `start {key}` gives you main and the work saved on this line: if there is some, an earlier agent was cut off. \
Reuse what is sound after re-checking it, and don't redo finished work. Write your plan in {dir}/notes.txt within \
your first 15 steps, update it after every result and `save {key}` it, so nothing is lost if you stop. Aim to finish \
within about {budget} steps: then stop exploring, write up what you have, and submit. A partial but precise result \
reported is worth more than a larger one lost.

{rigor}

THE REPORT: {dir}/{report}, one JSON object (the contract checks its form):
  "summary": what you did and found, in at most 250 words;
  "claims": a list, each with "id" (unique), "statement" (the precise statement or computational result), "kind" \
(one of {kinds}, or a word of your own), "status" (one of {statuses}), "evidence" (the full proof, or exactly what \
was computed and with which script, by path), "how_to_check" (what an independent checker should do to confirm \
or refute it);
  "dead_ends": what didn't work and why, briefly;
  "next_step": the single most promising continuation.
List as claims only precise statements, each with its status. Then commit and `submit {key}`."""

CHECK = """ATTACK {round} ON {parent}, CHECK OF LINE {line} ({front}): {title}
YOUR JOB IS TO CHECK ANOTHER AGENT. The agent on line {line} reported the claims in {line_dir}/{report}, and its \
files are in {line_dir}/: read them, but don't trust them. For EACH claim, try to refute it: re-derive every step \
of a proof yourself and look for the gap; recompute every computational claim with code YOU write from the \
definitions (don't reuse their scripts except to compare); test constructions by an independent count; check that \
what is labelled proved really is, and that nothing claimed as new is a known result in disguise. Give "confirmed" \
only to what you verified yourself, "refuted" with the concrete error or counterexample, and "not verified" when \
you couldn't settle it. When in doubt, "not verified". A claim that would settle the problem, change the order of a \
bound or set a record needs the strictest check: assume it's wrong until you have reproduced it end to end.{extra}
Legion doesn't let whoever wrote the line take this check.

WHAT THE ROUND IS FOR: {goal}

{common}

THE LINE YOU CHECK: {task}

WHERE. Your scripts, logs and notes go in {dir}/ (your zone). `save {key}` as you go. Aim to finish within about \
{budget} steps: check the most important claims first, and mark the rest "not verified" rather than run out.

THE VERDICTS: {dir}/{verdicts}, one JSON object (the contract checks it against the line's report):
  "verdicts": one per claim of the report, each with "claim_id", "verdict" (one of {verdict_words}) and "reason" \
(what you checked yourself and what you found; the error, if refuted);
  "overall": in at most 120 words, what of this line stands after checking.
Then commit and `submit {key}`."""

SYNTH = """ATTACK {round} ON {parent}: THE CONSOLIDATED REPORT.
Every line of this round and its check are in main. `python -m legion attack digest {parent} --round {round}` \
prints them: each line's summary, each claim with its checker's verdict and reason, the checker's overall view, the \
dead ends and the next step.

WHAT THE ROUND WAS FOR: {goal}

{common}

Write {dir}/{synthesis}, at most 900 words of plain markdown: (1) whether what the round was for was reached, and \
whether anything changed what is known (say so plainly, yes or no); (2) the results that are NEW and CONFIRMED by \
their checker, each as a precise statement, with where its proof or data is; (3) the claims refuted, or that \
couldn't be verified; (4) where each front stands now; (5) the one or two most promising next steps, with the \
reason. Don't inflate: a claim counts as confirmed only if its verdict is "confirmed". Where two lines bear on each \
other, say so.{extra}

Then commit and `submit {key}`, and:
- rewrite the state of the art of {parent} with what is now confirmed or ruled out (`python -m legion brief {parent}` \
shows it, `--edit FILE` publishes yours). If legion doesn't let you (you never worked on {parent} and aren't trusted \
yet), leave it in {dir}/brief.txt in the same patch, and say so to {lead};
- post the headline in the hub: `python -m legion chat --task {parent} "..."`;
- tell {lead}, who launched the round: `python -m legion send {lead} "..."`."""


DEEP = """
THIS IS A DEEP LINE: one approach, worked on over several sessions, by you and whoever takes it after you. It doesn't \
hold its round back, and nobody expects a result from each session. FIRST its KILL TEST (in the line above): the \
cheapest definite computation or argument that would show the approach CAN'T work. Do it before anything else and \
write its outcome at the top of {dir}/notes.txt. Killed: report that as the line's result (a claim, and why, in \
dead_ends) and submit; that is a good outcome. Survived: go on with the approach. Every session: `save {key} --notes \
"..."` with what is done and what is next, so the next session (yours or another agent's) goes on from there; \
then let it go if your session ends (`release {key}` after saving): a deep line let go of isn't stale. Submit when \
you have something definite, at the latest after about {budget} steps in all."""

IDEA = """ATTACK {round} ON {parent}, IDEA {line}: a new approach, from {field}
This round asks for an IDEA, not a result. The rounds before it stopped moving {parent}: their lines were \
variations of one another. You are one of several agents each proposing, alone, an approach the map doesn't have.

YOU SEE ONLY THE GOAL AND ITS BARRIERS, ON PURPOSE. Don't read the map, the state of the art, the hub, or the lines \
and reports of {parent} before {dir}/{idea} is written and committed: agents shown what was tried propose \
variations of it. The literature outside the project you may read, and should.

THE GOAL: {goal}

{common}
The barriers as they stand now (more may have been named since this round was launched): `python -m legion barrier \
{parent}`. Read that, and only that, before writing.

YOUR FIELD: {field}: {field_text}. Find there a tool, a theorem or a construction that could decide the goal, and \
make it an approach to it. If nothing in that field can, say why in {dir}/{idea} and give your best idea from \
another field.

WHERE. {dir}/ is your zone: notes, references, a small computation if one helps you judge your idea. Add what you \
read to the hub (`python -m legion lit add ...`). Aim to finish within about {budget} steps.

THE IDEA: {dir}/{idea}, one JSON object (the contract checks its form), each value text:
  "title": the approach in one line;
  "field": where it comes from, and the theorem, tool or construction it takes from there, with a reference;
  "mechanism": how it would get past the core difficulty of the goal (the gap between the bounds, not a constant): \
the step that does the work, concretely;
  "escapes": for each barrier above, by name, why it doesn't stop this approach;
  "kill_test": the cheapest definite computation or argument, one session's work, whose outcome would show the \
approach CAN'T work;
  "first_step": what to do first if it survives;
  "would_settle": what it proves if it works, exactly.
Then commit and `submit {key}`. Another agent judges it against the map, in {key}-check."""

JUDGE = """ATTACK {round} ON {parent}, JUDGING IDEA {line}
Another agent proposed, without seeing the map, a new approach to {parent}: {line_dir}/{idea}. Judge it against \
everything the project knows. NOW read the map (`python -m legion map {parent}`), its barriers (`python -m legion \
barrier {parent}`) and what the idea cites.

WHAT THE ROUND IS FOR: {goal}

  NOVELTY, 0 to 3: 0 the map has it already, or it is a known result in disguise (say which); 1 a variation of a \
line run here; 2 new here but known to fail elsewhere; 3 nothing like it was tried.
  PLAUSIBILITY, 0 to 3: whether its mechanism can do what it says, step by step. Look for the step that fails.
  ESCAPES: whether each "escapes" holds, barrier by barrier.
  THE KILL TEST: run it if it takes you at most about 30 steps; say "killed" (with what killed it), "survived", or \
"not run" (and why).
Be fair and hard: most new ideas fail, and an idea killed now costs one round, not ten.

WHERE. {dir}/ is your zone. Aim to finish within about {budget} steps.

THE JUDGEMENT: {dir}/{judgement}, one JSON object: "novelty" and "plausibility" (integers 0 to 3), "escapes_hold" \
(true or false), "kill_test" ("killed", "survived" or "not run"), "reason" (what you checked and found, at most 200 \
words). Then commit and `submit {key}`. Legion doesn't let whoever wrote the idea take this."""

IDEA_FILE, JUDGEMENT_FILE = "idea.json", "judgement.json"
IDEA_KEYS = ("title", "field", "mechanism", "escapes", "kill_test", "first_step", "would_settle")
KILL = ("killed", "survived", "not run")
BUDGET_MORE = {"idea": 40, "judge": 40, "deep": 400}


def load_spec(path: str) -> dict:
    """The round's spec, from a JSON file; common_file is read relative to it."""
    p = Path(path)
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Fail(f"can't read the attack's spec {path}: {e}", 2)
    if not isinstance(spec, dict):
        raise Fail("an attack's spec is a JSON object (`help attacks`)", 2)
    if spec.get("common_file"):
        try:
            spec["common"] = (p.parent / spec["common_file"]).read_text(encoding="utf-8")
        except OSError as e:
            raise Fail(f"can't read common_file: {e}", 2)
    return spec


def check_spec(spec: dict) -> str | None:
    """Why the spec can't make a round, if it can't."""
    text = lambda k, n: isinstance(spec.get(k, ""), str) and len(spec.get(k, "")) <= n  # noqa: E731
    if not isinstance(spec.get("goal"), str) or not spec["goal"].strip() or not text("goal", MAX_TEXT):
        return f"say what the round is for: goal, at most {MAX_TEXT} characters"
    if not text("common", MAX_COMMON):
        return f"common is text of at most {MAX_COMMON} characters"
    for k in ("check", "synthesis"):
        if not text(k, MAX_TEXT):
            return f"{k} is text of at most {MAX_TEXT} characters"
    lines = spec.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        return f"a round has 1 to {MAX_LINES} lines"
    seen = set()
    for ln in lines:
        if not isinstance(ln, dict):
            return "each line is an object with key, title and task"
        key = ln.get("key")
        if not isinstance(key, str) or not S.CHILD_KEY.match(key) or key.endswith("-check") or key == "synthesis":
            return (f"invalid line key {key!r}: lowercase letters, digits and hyphens, not ending in -check, "
                    "and not 'synthesis'")
        if key in seen:
            return f"two lines are called {key}"
        seen.add(key)
        for k in ("title", "task"):
            if not isinstance(ln.get(k), str) or not ln[k].strip() or len(ln[k]) > MAX_TEXT:
                return f"line {key} needs {k}, text of at most {MAX_TEXT} characters"
        if not isinstance(ln.get("front", ""), str) or len(ln.get("front", "")) > 60:
            return f"line {key}: front is a few words"
        needs = ln.get("needs") or []
        if not isinstance(needs, list) or len(needs) > 5 or not all(isinstance(n, str) and S.TOOL.fullmatch(n)
                                                                    for n in needs):
            return f"line {key}: needs lists at most 5 programs its work takes (lake, kissat...)"
        if ln.get("kind", "") not in ("", "idea") or not isinstance(ln.get("deep", False), bool):
            return f"line {key}: kind is idea or nothing, deep true or false"
        if ln.get("kind") == "idea" and not (isinstance(ln.get("field"), str) and ln["field"].strip()
                                             and len(ln["field"]) <= 40 and isinstance(ln.get("field_text", ""), str)):
            return f"line {key}: an idea names the field it is to come from (at most 40 characters)"
    bars = spec.get("barriers", [])
    if not isinstance(bars, list) or len(bars) > S.MAX_BARRIERS or not all(isinstance(b, str) and S.CHILD_KEY.match(b)
                                                                            for b in bars):
        return "barriers lists the names of the goal's barriers"
    if spec.get("round") is not None and (not isinstance(spec["round"], str) or not S.CHILD_KEY.match(spec["round"])):
        return "round is a short name: lowercase letters, digits and hyphens"
    if spec.get("folder") is not None and (not isinstance(spec["folder"], str) or not spec["folder"].strip("/")
                                           or ".." in spec["folder"] or spec["folder"].startswith("/")):
        return "folder is a path inside the task's zone"
    budget = spec.get("budget", {})
    if not isinstance(budget, dict) or not all(k in BUDGET and isinstance(v, int) and 10 <= v <= 1000
                                               for k, v in budget.items()):
        return "budget gives line and check, in steps (10 to 1000)"
    return None


def rounds(st: S.State, parent: S.Task) -> dict:
    """The rounds launched on a task: name -> its pieces in order (lines, checks, synthesis)."""
    out = {}
    for t in sorted((t for t in st.tasks.values() if t.parent == parent.id and t.attack), key=lambda t: t.lamport):
        out.setdefault(t.attack.get("round", "?"), []).append(t)
    return out


def next_round(st: S.State, parent: S.Task) -> str:
    taken = [int(m.group(1)) for r in rounds(st, parent) if (m := ROUND.match(r))]
    return f"r{max(taken, default=0) + 1}"


def default_folder(parent: S.Task) -> str | None:
    """Where rounds go when the spec doesn't say: under the task's first folder."""
    folders = [z for z in parent.zone if z.endswith("/")]
    return folders[0].rstrip("/") + "/attacks" if folders else None


def _contract_root(key: str) -> int:
    """How far up from a contract's file the repository's root is: legion_contracts/<key>/test_x.py."""
    return len(key.split("/")) + 1


def report_contract(key: str, path: str) -> str:
    return f'''"""The contract of an attack's line: its report, in the form the check and the synthesis read."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{_contract_root(key)}]
REPORT = {path!r}
STATUSES = {STATUSES!r}


class Report(unittest.TestCase):
    def test_the_report_is_there_and_complete(self):
        f = ROOT / REPORT
        self.assertTrue(f.is_file(), f"write the line's report in {{REPORT}}")
        r = json.loads(f.read_text(encoding="utf-8"))
        self.assertIsInstance(r, dict, "the report is one JSON object")
        for k in ("summary", "dead_ends", "next_step"):
            self.assertIsInstance(r.get(k), str, f"the report needs {{k}}, as text")
        self.assertTrue(r["summary"].strip(), "say what you did and found in summary")
        self.assertLessEqual(len(r["summary"].split()), 400, "summary: at most 250 words")
        self.assertIsInstance(r.get("claims"), list, "claims is a list (it may be empty)")
        ids = set()
        for c in r["claims"]:
            self.assertIsInstance(c, dict, "each claim is an object")
            for k in ("id", "statement", "kind", "status", "evidence", "how_to_check"):
                self.assertIsInstance(c.get(k), str, f"claim {{c.get('id')!r}} needs {{k}}, as text")
                self.assertTrue(c[k].strip(), f"claim {{c.get('id')!r}}: {{k}} is empty")
            self.assertIn(c["status"], STATUSES, f"claim {{c['id']!r}}: status is one of {{STATUSES}}")
            self.assertNotIn(c["id"], ids, f"two claims are called {{c['id']!r}}")
            ids.add(c["id"])


if __name__ == "__main__":
    unittest.main()
'''


def verdicts_contract(key: str, report: str, path: str) -> str:
    return f'''"""The contract of an attack's check: a verdict on every claim of the line's report."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{_contract_root(key)}]
REPORT, VERDICTS = {report!r}, {path!r}
WORDS = {VERDICTS!r}


class Verdicts(unittest.TestCase):
    def test_every_claim_has_one_verdict(self):
        claims = json.loads((ROOT / REPORT).read_text(encoding="utf-8"))["claims"]
        f = ROOT / VERDICTS
        self.assertTrue(f.is_file(), f"write the verdicts in {{VERDICTS}}")
        v = json.loads(f.read_text(encoding="utf-8"))
        self.assertIsInstance(v, dict, "the verdicts are one JSON object")
        self.assertIsInstance(v.get("overall"), str, "say in overall what of the line stands")
        self.assertTrue(v["overall"].strip(), "overall is empty")
        self.assertIsInstance(v.get("verdicts"), list, "verdicts is a list")
        given = []
        for x in v["verdicts"]:
            self.assertIsInstance(x, dict, "each verdict is an object")
            for k in ("claim_id", "verdict", "reason"):
                self.assertIsInstance(x.get(k), str, f"a verdict needs {{k}}, as text")
            self.assertIn(x["verdict"], WORDS, f"claim {{x['claim_id']!r}}: the verdict is one of {{WORDS}}")
            self.assertTrue(x["reason"].strip(), f"claim {{x['claim_id']!r}}: say why")
            given.append(x["claim_id"])
        ids = [c["id"] for c in claims]
        self.assertEqual(sorted(given), sorted(ids), "one verdict for each claim of the report, and no other")


if __name__ == "__main__":
    unittest.main()
'''


def synthesis_contract(key: str, path: str) -> str:
    return f'''"""The contract of an attack's synthesis: the consolidated report."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{_contract_root(key)}]
REPORT = {path!r}


class Synthesis(unittest.TestCase):
    def test_the_report_is_written(self):
        f = ROOT / REPORT
        self.assertTrue(f.is_file(), f"write the consolidated report in {{REPORT}}")
        words = len(f.read_text(encoding="utf-8").split())
        self.assertGreaterEqual(words, 60, "the report is too short to say where the round stands")
        self.assertLessEqual(words, 1400, "at most 900 words: keep what matters")


if __name__ == "__main__":
    unittest.main()
'''


def idea_contract(key: str, path: str, barriers: list[str] = ()) -> str:
    return f'''"""The contract of an idea round's idea: every field of it, as text; its escapes answer the goal's
barriers as they stood at launch, each by name."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{_contract_root(key)}]
IDEA = {path!r}
KEYS = {IDEA_KEYS!r}
BARRIERS = {list(barriers)!r}


class Idea(unittest.TestCase):
    def test_the_idea_is_there_and_complete(self):
        f = ROOT / IDEA
        self.assertTrue(f.is_file(), f"write the idea in {{IDEA}}")
        x = json.loads(f.read_text(encoding="utf-8"))
        self.assertIsInstance(x, dict, "the idea is one JSON object")
        for k in KEYS:
            self.assertIsInstance(x.get(k), str, f"the idea needs {{k}}, as text")
            self.assertGreaterEqual(len(x[k].strip()), 20, f"say more in {{k}}")
        said = " ".join(x["escapes"].lower().replace("-", " ").split())
        missing = [b for b in BARRIERS if b.replace("-", " ") not in said]
        self.assertFalse(missing, f"escapes answers every barrier by name: missing {{missing}}")


if __name__ == "__main__":
    unittest.main()
'''


def judgement_contract(key: str, path: str) -> str:
    return f'''"""The contract of the judging of an idea: scores, whether its escapes hold, its kill test, why."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{_contract_root(key)}]
JUDGEMENT = {path!r}
KILL = {KILL!r}


class Judgement(unittest.TestCase):
    def test_the_judgement_is_there_and_complete(self):
        f = ROOT / JUDGEMENT
        self.assertTrue(f.is_file(), f"write the judgement in {{JUDGEMENT}}")
        x = json.loads(f.read_text(encoding="utf-8"))
        self.assertIsInstance(x, dict, "the judgement is one JSON object")
        for k in ("novelty", "plausibility"):
            self.assertIsInstance(x.get(k), int, f"{{k}} is an integer, 0 to 3")
            self.assertIn(x[k], (0, 1, 2, 3), f"{{k}} is 0, 1, 2 or 3")
        self.assertIsInstance(x.get("escapes_hold"), bool, "escapes_hold is true or false")
        self.assertIn(x.get("kill_test"), KILL, f"kill_test is one of {{KILL}}")
        self.assertIsInstance(x.get("reason"), str, "say why, in reason")
        self.assertTrue(x["reason"].strip(), "reason is empty")


if __name__ == "__main__":
    unittest.main()
'''


def plan(st: S.State, parent: S.Task, spec: dict, lead: str) -> tuple[str, list[dict]]:
    """The round's name and its subtasks, as task.created payloads in the order they're created."""
    err = check_spec(spec)
    if err:
        raise Fail(err, 2)
    rnd = spec.get("round") or next_round(st, parent)
    if rnd in rounds(st, parent):
        raise Fail(f"{parent.key} already has a round {rnd}: name the new one (`round` in the spec)", 2)
    folder = (spec.get("folder") or default_folder(parent) or "").strip("/")
    if not folder:
        raise Fail(f"{parent.key}'s zone has no folder for the round's files: say where (`folder` in the spec), "
                   f"inside {', '.join(parent.zone)}", 2)
    base = f"{folder}/{rnd}"
    budget = {**BUDGET_MORE, **BUDGET, **spec.get("budget", {})}
    shared = dict(round=rnd, parent=parent.key, goal=spec["goal"].strip(), lead=lead,
                  common=(spec.get("common") or "").strip() or "(No common text: read the task and its state of the "
                                                               f"art, `python -m legion brief {parent.key}`.)")
    accept = lambda key: f"python -m unittest discover -s {S.contract_dir(key)} -p test_*.py"  # noqa: E731
    pieces, checks = [], []
    deep_checks = []
    for ln in spec["lines"]:
        key = f"{parent.key}/{rnd}-{ln['key']}"
        d = f"{base}/{ln['key']}"
        front = (ln.get("front") or "").strip() or "line"
        ckey, cd = f"{key}-check", f"{d}-check"
        mark = {"round": rnd, "line": ln["key"]}
        if ln.get("kind") == "idea":
            # An idea, from the field it was handed, by an agent shown only the goal and its barriers; judged by
            # another against the map. Its own text is the round's common one: the barriers, not the map.
            mark.update(kind="idea", field=ln["field"].strip()[:40])
            body = IDEA.format(**shared, key=key, line=ln["key"], field=ln["field"].strip(),
                               field_text=(ln.get("field_text") or ln["task"]).strip(), dir=d, idea=IDEA_FILE,
                               budget=budget["idea"])
            contract = {"test_idea.py": idea_contract(key, f"{d}/{IDEA_FILE}", spec.get("barriers") or [])}
            cbody = JUDGE.format(**shared, key=ckey, line=ln["key"], dir=cd, line_dir=d, idea=IDEA_FILE,
                                 judgement=JUDGEMENT_FILE, budget=budget["judge"])
            ccontract = {"test_judgement.py": judgement_contract(ckey, f"{cd}/{JUDGEMENT_FILE}")}
            title, ctitle = f"{rnd} idea {ln['key']} ({ln['field'].strip()})", f"{rnd} judging idea {ln['key']}"
        else:
            if ln.get("deep"):
                mark["deep"] = True
            body = LINE.format(**shared, key=key, line=ln["key"], front=front, title=ln["title"].strip(),
                               task=ln["task"].strip(), dir=d, budget=budget["deep" if ln.get("deep") else "line"],
                               rigor=RIGOR, report=REPORT, kinds=", ".join(KINDS), statuses=", ".join(STATUSES))
            if ln.get("deep"):
                body += "\n" + DEEP.format(dir=d, key=key, budget=budget["deep"])
            contract = {"test_report.py": report_contract(key, f"{d}/{REPORT}")}
            extra = spec.get("check", "").strip()
            cbody = CHECK.format(**shared, key=ckey, line=ln["key"], front=front, title=ln["title"].strip(),
                                 task=ln["task"].strip(), dir=cd, line_dir=d, budget=budget["check"], report=REPORT,
                                 verdicts=VERDICTS_FILE, verdict_words=", ".join(VERDICTS),
                                 extra=f"\n{extra}" if extra else "")
            ccontract = {"test_verdicts.py": verdicts_contract(ckey, f"{d}/{REPORT}", f"{cd}/{VERDICTS_FILE}")}
            title = f"{rnd} {'deep line' if ln.get('deep') else 'line'} {ln['key']}: {ln['title'].strip()}"
            ctitle = f"{rnd} check of {ln['key']}: {ln['title'].strip()}"
        pieces.append(dict(key=key, title=title[:200], zone=[d + "/"], accept=accept(key), body=body, size="L",
                           mode="exclusive", deps=[], contract=contract, attack=dict(mark, role="line")))
        # What the line's work takes on the machine, and the proposal it was made from (research.py), if any.
        pieces[-1].update({k: ln[k] for k in ("needs", "proposal") if ln.get(k)})
        pieces.append(dict(key=ckey, title=ctitle[:200], zone=[cd + "/"], accept=accept(ckey), body=cbody, size="M",
                           mode="exclusive", deps=[key], apart=[key], contract=ccontract,
                           attack=dict(mark, role="check")))
        (deep_checks if ln.get("deep") else checks).append(ckey)
    skey, sd = f"{parent.key}/{rnd}-synthesis", f"{base}/synthesis"
    extra = spec.get("synthesis", "").strip()
    ideas = any(ln.get("kind") == "idea" for ln in spec["lines"])
    # A deep line's check doesn't hold the synthesis back, unless the round has nothing else.
    pieces.append(dict(key=skey, title=f"{rnd} synthesis: the consolidated report", zone=[sd + "/"],
                       accept=accept(skey), deps=checks or deep_checks, size="M", mode="exclusive",
                       body=SYNTH.format(**shared, key=skey, dir=sd, synthesis=SYNTHESIS,
                                         extra=f"\n{extra}" if extra else ""),
                       contract={"test_synthesis.py": synthesis_contract(skey, f"{sd}/{SYNTHESIS}")},
                       attack={"round": rnd, "role": "synthesis", "line": "",
                               **({"assess": "progress"} if spec.get("assess") and not ideas else {}),
                               **({"kind": "idea"} if ideas else {})}))
    for p in pieces:
        p["parent"] = parent.id
        why = (st._subtask_error(parent, p["key"], p["zone"]) or S.check_accept(p["accept"], st.runners)
               or S.contract_error(p) or (f"a task with key {p['key']} already exists" if st.find_task(p["key"]) else None))
        if why:
            raise Fail(f"{p['key']}: {why}", 2)
    return rnd, pieces


# --- reading a round back --------------------------------------------------------------------------------------------

def _read(repo, ref: str, path: str):
    p = coordgit.run(repo, "show", f"{ref}:{path}")
    return p.stdout if p.returncode == 0 else None


def digest(c: Ctx, parent: S.Task, rnd: str, ref: str) -> list[dict]:
    """What the synthesis reads: each line's report, every claim with its checker's verdict, from main."""
    now, out = time.time(), []
    pieces = rounds(c.st, parent).get(rnd, [])
    for line in (t for t in pieces if t.attack.get("role") == "line"):
        check = next((t for t in pieces if t.attack.get("role") == "check"
                      and t.attack.get("line") == line.attack.get("line")), None)
        d = line.zone[0].rstrip("/")
        entry = {"line": line.attack.get("line"), "title": line.title, "status": c.st.status(line.id, now),
                 "check_status": c.st.status(check.id, now) if check else None, "folder": d}
        if line.attack.get("kind") == "idea":  # an idea and its judgement, not claims and verdicts
            for k, path in (("idea", f"{d}/{IDEA_FILE}"),
                            ("judgement", f"{check.zone[0].rstrip('/')}/{JUDGEMENT_FILE}" if check else None)):
                try:
                    got = json.loads(_read(c.repo, ref, path) or "null") if path else None
                except ValueError:
                    got = None
                if isinstance(got, dict):
                    entry[k] = got
            out.append(entry)
            continue
        try:
            report = json.loads(_read(c.repo, ref, f"{d}/{REPORT}") or "null")
        except ValueError:
            report = None
        verdicts = {}
        if check:
            try:
                v = json.loads(_read(c.repo, ref, f"{check.zone[0].rstrip('/')}/{VERDICTS_FILE}") or "null")
            except ValueError:
                v = None
            if isinstance(v, dict):
                verdicts = {x.get("claim_id"): x for x in v.get("verdicts") or [] if isinstance(x, dict)}
                entry["check_overall"] = v.get("overall", "")
        if isinstance(report, dict):
            entry.update(summary=report.get("summary", ""), dead_ends=report.get("dead_ends", ""),
                         next_step=report.get("next_step", ""))
            entry["claims"] = [dict(id=x.get("id"), statement=x.get("statement"), kind=x.get("kind"),
                                    status=x.get("status"),
                                    verdict=verdicts.get(x.get("id"), {}).get("verdict", "not verified"),
                                    reason=verdicts.get(x.get("id"), {}).get("reason", ""))
                               for x in report.get("claims") or [] if isinstance(x, dict)]
        out.append(entry)
    return out


def digest_text(entries: list[dict]) -> str:
    out = []
    for e in entries:
        out.append(f"== line {e['line']}: {e['title']}  [line {e['status']}, check {e['check_status']}]")
        if "idea" in e or "judgement" in e:
            for k in IDEA_KEYS:
                if (e.get("idea") or {}).get(k):
                    out.append(f"   {k}: {e['idea'][k]}")
            j = e.get("judgement")
            if j:
                out.append(f"   JUDGED: novelty {j.get('novelty')}/3, plausibility {j.get('plausibility')}/3, escapes "
                           f"{'hold' if j.get('escapes_hold') else 'do not hold'}, kill test {j.get('kill_test')}: "
                           f"{j.get('reason', '')}")
            else:
                out.append("   (not judged yet)")
            continue
        if "summary" not in e:
            out.append("   (its report isn't in main yet)")
            continue
        out.append(f"   summary: {e['summary']}")
        for x in e.get("claims", []):
            out.append(f"   - {x['id']} [{x['status']}; {x['verdict']}] {x['statement']}")
            if x["reason"]:
                out.append(f"       checker: {x['reason']}")
        if e.get("check_overall"):
            out.append(f"   check, overall: {e['check_overall']}")
        out.append(f"   dead ends: {e['dead_ends']}")
        out.append(f"   next step: {e['next_step']}")
    return "\n".join(out)


# --- the command -----------------------------------------------------------------------------------------------------

def cmd_attack(args):
    from .cli import out
    if args.action == "launch":
        return launch(args)
    c = Ctx(sync=not args.no_sync)
    parent = c.task(args.task)
    rs = rounds(c.st, parent)
    if not rs:
        raise Fail(f"{parent.key} has no rounds of attack: `python -m legion attack launch {parent.key} SPEC` "
                   "(`help attacks`)", 3)
    rnd = args.round or list(rs)[-1]
    if rnd not in rs:
        raise Fail(f"{parent.key} has no round {rnd}: it has {', '.join(rs)}", 3)
    if args.action == "digest":
        coordgit.refresh_base(c.repo)
        entries = digest(c, parent, rnd, coordgit.base_ref(c.repo))
        out(args, {"task": parent.key, "round": rnd, "lines": entries},
            f"Round {rnd} on {parent.key}, from main:\n{digest_text(entries)}")
        return 0
    now, rows = time.time(), []
    for t in rs[rnd]:
        h = c.st.holder(t.id, now)
        rows.append({"key": t.key, "role": t.attack.get("role"), "line": t.attack.get("line"),
                     "status": c.st.status(t.id, now), "holder": h.name if h else None})
    done = sum(c.st.done(t.id) for t in rs[rnd])
    out(args, {"task": parent.key, "round": rnd, "rounds": list(rs), "pieces": rows},
        f"Round {rnd} on {parent.key} ({done} of {len(rows)} pieces done; rounds: {', '.join(rs)}):\n" +
        "\n".join(f"  {r['key']:<44} {r['role']:<9} {r['status']:<8} {r['holder'] or ''}" for r in rows))
    return 0


# Who a new round is announced to: every owner seen in the project within this many seconds. A message to an owner
# reaches each of its agents (`send @owner`): an idle one under `legion run` is started again with it, a busy one
# gets it mid-session (its hooks, or its next legion command).
CALL_SEEN = 86400

CALL = """Round {rnd} of attack on {parent} is open, launched by {lead}: {lines} line(s) ({keys}), each checked by an \
agent that didn't write it, then one synthesis (`python -m legion help attacks`). Join it, the way a lead fills a \
round with subagents. Between tasks: `python -m legion claim` hands you a piece. Busy on a task: don't drop it; if \
your harness can, start subagents of yours (OWNER#<n>, each its own identity and its own clone: \
`python -m legion join <invitation> --number <n>`), one per piece nobody holds, until every open piece has someone, \
and go on with yours. Who holds what: `python -m legion attack status {parent}`."""


def open_pieces(st: S.State, now: float) -> list[S.Task]:
    """The pieces of attack open right now that nobody holds: the holes a round's agents are called to fill."""
    return sorted((t for t in st.tasks.values() if t.attack and not t.cancelled and st.status(t.id, now) == "open"
                   and st.holder(t.id, now) is None), key=lambda t: t.key)


def call_text(st: S.State, me: str, now: float) -> str:
    """What `context` says of the open pieces this agent could take (not the checks of lines it wrote), or ""."""
    holes = [t for t in open_pieces(st, now) if not st.apart_error(t, me)]
    if not holes:
        return ""
    return (f"{len(holes)} piece(s) of attack open with nobody on them: {', '.join(t.key for t in holes[:8])}"
            + ("…" if len(holes) > 8 else "") + ". Between tasks, `claim` hands you one. Busy on a task: if your "
            "harness can, start one subagent of yours (OWNER#<n>, its own identity and clone: `join <invitation> "
            "--number <n>`) per piece, until each has someone, and go on with yours (`help attacks`).")


def announce(c: Ctx, parent: S.Task, rnd: str, pieces: list[dict], now: float) -> list[str]:
    """Tell every owner active in the project that the round is open: the owners it went to."""
    owners = sorted({S.owner(c.st.names[a]) for a, ts in c.st.last_seen.items()
                     if now - ts <= CALL_SEEN and a in c.st.names})
    if not owners:
        return []
    keys = [p["key"] for p in pieces if p["attack"]["role"] == "line"]
    text = CALL.format(rnd=rnd, parent=parent.key, lead=c.ident.name, lines=len(keys), keys=", ".join(keys))
    for i in range(0, len(owners), S.MAX_RECIPIENTS):  # a message names at most MAX_RECIPIENTS
        c.emit("message.sent", to=["@" + o for o in owners[i:i + S.MAX_RECIPIENTS]], task=parent.id,
               text=text[:2000])
    return owners


def launch(args):
    c = Ctx()
    parent = c.task(args.task)
    now = time.time()
    if parent.mode != "exclusive" or parent.kind != "task":
        raise Fail(f"{parent.key} isn't a task one can split: an attack goes on an exclusive task", 3)
    if c.st.done(parent.id) or parent.cancelled:
        raise Fail(f"{parent.key} is already done", 3)
    if not (c.st.trusted(c.me) and c.st.took_part(parent.id, c.me, now)):
        raise Fail(f"a round of attack is launched by a trusted agent that works on the task: claim {parent.key} "
                   "first (`python -m legion claim " + parent.id + "`), or ask someone trusted to launch it", 3)
    rnd, pieces = plan(c.st, parent, load_spec(args.spec), c.ident.name)
    if args.dry_run:
        print(f"Round {rnd} on {parent.key} would create {len(pieces)} subtasks:")
        for p in pieces:
            print(f"  {p['key']:<44} zone {p['zone'][0]:<40} deps {', '.join(p['deps']) or '-'}")
        print("\nThe first line's description, as its agent will read it:\n\n" + pieces[0]["body"])
        return 0
    for p in pieces:
        c.emit("task.created", **p, split=True)
    called = announce(c, parent, rnd, pieces, now)
    c.sync()
    made = [p["key"] for p in pieces if c.st.find_task(p["key"])]
    lines = sum(1 for p in pieces if p["attack"]["role"] == "line")
    print(f"Round {rnd} on {parent.key}: {lines} lines, {lines} checks and a synthesis, {len(made)} subtasks open "
          f"now. `claim` hands the lines out to any agent, on any machine; each check opens when its line is in, "
          "for an agent that didn't write it; the synthesis opens once every check is in.\n"
          f"Follow it with `python -m legion attack status {parent.key}`, and `wait`: the synthesis tells you when "
          f"it's done. Then read it and `attack digest {parent.key}`, check the key claims yourself, and launch the "
          "next round from its next steps.")
    if called:
        print(f"Announced to {', '.join('@' + o for o in called)}: their agents between tasks take pieces, and busy "
              "ones start subagents until each piece has someone.")
    if len(made) < len(pieces):
        raise Fail(f"only {len(made)} of {len(pieces)} subtasks were created: "
                   f"{c.st.ignored[-1][1] if c.st.ignored else 'unknown'}", 1)
    return 0


def add_parser(sub) -> None:
    s = sub.add_parser("attack", help="a round of research on a hard task: lines explored in parallel, each checked "
                                      "by another agent, then one synthesis (`help attacks`)")
    s.add_argument("action", choices=["launch", "status", "digest"])
    s.add_argument("task")
    s.add_argument("spec", nargs="?", help="launch: the round's spec, a JSON file (`help attacks`)")
    s.add_argument("--round", help="status, digest: which round (default: the latest)")
    s.add_argument("--dry-run", action="store_true", help="launch: show the subtasks and a description, create nothing")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=_dispatch)


def _dispatch(args):
    if args.action == "launch" and not args.spec:
        raise Fail("launch needs the round's spec: `python -m legion attack launch TASK SPEC.json` (`help attacks`)", 2)
    return cmd_attack(args)
