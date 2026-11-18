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
    budget = dict(BUDGET, **spec.get("budget", {}))
    shared = dict(round=rnd, parent=parent.key, goal=spec["goal"].strip(), lead=lead,
                  common=(spec.get("common") or "").strip() or "(No common text: read the task and its state of the "
                                                               f"art, `python -m legion brief {parent.key}`.)")
    accept = lambda key: f"python -m unittest discover -s {S.contract_dir(key)} -p test_*.py"  # noqa: E731
    pieces, checks = [], []
    for ln in spec["lines"]:
        key = f"{parent.key}/{rnd}-{ln['key']}"
        d = f"{base}/{ln['key']}"
        front = (ln.get("front") or "").strip() or "line"
        body = LINE.format(**shared, key=key, line=ln["key"], front=front, title=ln["title"].strip(),
                           task=ln["task"].strip(), dir=d, budget=budget["line"], rigor=RIGOR, report=REPORT,
                           kinds=", ".join(KINDS), statuses=", ".join(STATUSES))
        pieces.append(dict(key=key, title=f"{rnd} line {ln['key']}: {ln['title'].strip()}"[:200], zone=[d + "/"],
                           accept=accept(key), body=body, size="L", mode="exclusive", deps=[],
                           contract={"test_report.py": report_contract(key, f"{d}/{REPORT}")},
                           attack={"round": rnd, "role": "line", "line": ln["key"]}))
        ckey, cd = f"{key}-check", f"{d}-check"
        extra = spec.get("check", "").strip()
        cbody = CHECK.format(**shared, key=ckey, line=ln["key"], front=front, title=ln["title"].strip(),
                             task=ln["task"].strip(), dir=cd, line_dir=d, budget=budget["check"], report=REPORT,
                             verdicts=VERDICTS_FILE, verdict_words=", ".join(VERDICTS),
                             extra=f"\n{extra}" if extra else "")
        pieces.append(dict(key=ckey, title=f"{rnd} check of {ln['key']}: {ln['title'].strip()}"[:200],
                           zone=[cd + "/"], accept=accept(ckey), body=cbody, size="M", mode="exclusive", deps=[key],
                           apart=[key],
                           contract={"test_verdicts.py": verdicts_contract(ckey, f"{d}/{REPORT}",
                                                                           f"{cd}/{VERDICTS_FILE}")},
                           attack={"round": rnd, "role": "check", "line": ln["key"]}))
        checks.append(ckey)
    skey, sd = f"{parent.key}/{rnd}-synthesis", f"{base}/synthesis"
    extra = spec.get("synthesis", "").strip()
    pieces.append(dict(key=skey, title=f"{rnd} synthesis: the consolidated report", zone=[sd + "/"],
                       accept=accept(skey), deps=checks, size="M", mode="exclusive",
                       body=SYNTH.format(**shared, key=skey, dir=sd, synthesis=SYNTHESIS,
                                         extra=f"\n{extra}" if extra else ""),
                       contract={"test_synthesis.py": synthesis_contract(skey, f"{sd}/{SYNTHESIS}")},
                       attack={"round": rnd, "role": "synthesis", "line": ""}))
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
    c.sync()
    made = [p["key"] for p in pieces if c.st.find_task(p["key"])]
    lines = sum(1 for p in pieces if p["attack"]["role"] == "line")
    print(f"Round {rnd} on {parent.key}: {lines} lines, {lines} checks and a synthesis, {len(made)} subtasks open "
          f"now. `claim` hands the lines out to any agent, on any machine; each check opens when its line is in, "
          "for an agent that didn't write it; the synthesis opens once every check is in.\n"
          f"Follow it with `python -m legion attack status {parent.key}`, and `wait`: the synthesis tells you when "
          f"it's done. Then read it and `attack digest {parent.key}`, check the key claims yourself, and launch the "
          "next round from its next steps.")
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
