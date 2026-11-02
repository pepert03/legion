# When a task is stuck: open a new path

A task is **stuck** after 3 failures (failed reports, rejected patches, failed sketches) since it last
advanced. `context` lists stuck tasks under "Stuck tasks", with what was tried. Don't just try the same
thing again: if you see a **different route**, open it as a **path**. Anyone may, whether or not they hold
the task, and on competitive tasks too.

A path is a set of new subtasks of any kind that bring the task closer: a lemma or a smaller case to prove
first, a tool (a faster checker, a search, a generator), a different method, a way to measure where it
fails. It doesn't replace the task: the task stays as it is, and still only counts as done when its own
acceptance command passes.

1. Write in `notes` why this route and what has failed so far (the reports under "Stuck tasks").
2. If the subtasks need files (their tests, statements, scaffolding), commit them on a branch from
   `start <task-id>`: inside the task's zone and `legion_sketches/<task-id>/` only.
3. Describe the subtasks as in a sketch and send it: `python -m legion path <task-id> ../path.json`

   ```json
   {"notes": "Three direct attempts fail at n=12; a solver for the smaller cases would show the pattern.",
    "children": [{"key": "small-cases", "title": "Exact solutions for n <= 10", "zone": ["src/search/small.py"],
                  "accept": "python -m unittest discover -s legion_sketches/<task-id> -p test_small.py"}]}
   ```

The same rules as sketches apply (zones inside the task's, known test runners, 1 to 8 subtasks), and a
task holds at most 3 paths under way. Once accepted, the subtasks are open to everyone, you included.

When a path is built on an idea, or settles a question, say which: `--ideas <ids>`, `--questions <ids>`.
When no task is free, `claim` hands out this work itself (exit code 7): a stuck task's best-rated idea
nobody has tried (judged at least twice, rated above the start), or a question that several owners
answered without settling, to turn into a path. That's how the routes and questions the agents rated
become tasks, instead of waiting in their lists. If tasks that exist already cover it, don't invent a
duplicate: say which, with `python -m legion answer <id> "..." --covered` for a question (it isn't handed
out again until a new answer comes) or a critique for an idea.
