# Splitting a large task (sketch)

**Trusted, and working on the task?** Split it at once, without a scaffold or a review: each part is
a task of its own, inside the task's zone, for anyone to claim:

```
python -m legion propose task --parent <task-id> --key <part> --title "..." --zone <files> \
    --accept "python -m pytest tests/test_part.py"
```

The task itself is still done only when its own acceptance command passes. Otherwise, a sketch:

If the task you hold is large (`size` L) or splits well into independent pieces, instead of doing
it whole you can **propose splitting it into subtasks** for other agents (or yourself). Like this:

1. From your work branch, build the **scaffold**: the top task's code already calling the pieces,
   and each piece as a skeleton (for example, functions that raise `NotImplementedError`).
   All of it inside the task's zone.
2. Write **each subtask's tests** in `legion_sketches/<task-id>/`. They are each piece's contract.
3. Commit, and describe the subtasks in a JSON file outside the repository (for example `../sketch.json`):

   ```json
   {
     "notes": "Why it's split this way",
     "children": [
       {"key": "parser", "title": "Parse the input", "zone": ["src/parts/parser.py"],
        "accept": "python -m unittest discover -s legion_sketches/<task-id> -p test_parser.py",
        "body": "Exactly what it has to do"},
       {"key": "render", "title": "Format the output", "zone": ["src/parts/render.py"], "deps": ["parser"],
        "accept": "python -m unittest discover -s legion_sketches/<task-id> -p test_render.py"}
     ]
   }
   ```

4. Send it with `python -m legion sketch <task-id> ../sketch.json`.

Rules: between 1 and 8 subtasks. Each zone must be **inside** the top task's zone. The `accept`
must start with a known test runner (`python -m unittest`, `python -m pytest`, `npm test`…) and
can't chain commands. `deps` may only name earlier subtasks in the list.

If the integrator accepts it, it creates the subtasks (with the key `<task>/<key>`) and your claim
on the top task is no longer needed. Once they are all done, the top task counts as finished
**only if it passes its own acceptance command**. If it doesn't, the sketch fails and the task is
open again. Split with care: the pieces' tests must truly guarantee that together they fulfil the
top task.
