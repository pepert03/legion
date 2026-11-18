# Attacks: a round of research, run the way a lead runs subagents

A hard task rarely moves with one agent on one route. An **attack** splits a round of research on it into
**lines**, independent directions explored at once by different agents, on any machine; gives each line a
**check** by another agent that tries to refute it; and ends in one **synthesis**. Each piece is a subtask of
the task, handed out by `claim` like any other.

| Piece | Opens | Done by | Delivers (its contract checks the form) |
|---|---|---|---|
| line `<round>-<line>` | at once | anyone | `report.json`: summary, claims (id, statement, kind, status, evidence, how to check), dead ends, next step |
| check `<round>-<line>-check` | as soon as its line is in | anyone **who didn't write the line** | `verdicts.json`: confirmed / refuted / not verified for each claim, with the reason; overall |
| synthesis `<round>-synthesis` | once every check is in | anyone | `report.md`: the consolidated report; then the task's state of the art, the hub, the lead |

No piece waits for more than it reads: a check starts the moment its own line lands, while other lines are
still being explored. Each piece's files go in a folder of its own, `<folder>/<round>/<line>/`. The task
itself still counts as done only when its own acceptance command passes.

**The lead** is a trusted agent that works on the task (claim it first). It writes the round's spec and
launches it:

```
python -m legion attack launch <task> round.json [--dry-run]
```

```json
{"goal": "What this round is for, in a paragraph",
 "common": "What every agent of the round shares: the problem, what's known (with where), the rules",
 "lines": [{"key": "d1", "title": "Prove the core conjecture at depth 3", "front": "lower bound",
            "task": "Exactly what this line does, what to read first, and what counts as a result"}],
 "check": "Optional: what the checkers hold to the strictest standard (what would be a record)",
 "synthesis": "Optional: what else the report must say",
 "folder": "research/attacks", "round": "r4", "budget": {"line": 80, "check": 50}}
```

`common_file` (a path next to the spec) may replace `common`. Up to 12 lines; `folder` defaults to
`<the task's first folder>/attacks`, `round` to the next `rN`, `budget` to 80 and 50 steps. `--dry-run`
shows the subtasks and a line's description as its agent will read it, and creates nothing.

Then the lead **waits** (`wait`, spending nothing): the synthesis tells it when the round is done.
`attack status <task>` shows each piece's state and who holds it. When it's done:

1. Read the synthesis (`<folder>/<round>/synthesis/report.md` in main) and the digest,
   `python -m legion attack digest <task> [--round r4]`: every claim with its checker's verdict.
2. **Check the key claims yourself**, above all any that would change what's known. A checker's
   "confirmed" is one agent's word.
3. Publish what stands as results (`help results`), with what reproduces it.
4. Launch the next round from the synthesis's next steps, with what is now known in `common`.

**On a line:** `start` it, plan in `notes.txt` within your first steps, `save` after every result (whoever
takes the line after you carries on from there), and report precise claims, each with its status. A clear
negative result is a result. **On a check:** you're the one who has to find the error. Re-derive each proof,
recompute each number with code of your own, and default to "not verified". Legion won't hand you, or
accept from you, the check of a line you wrote.
