# When the statement is false: refute it

A task states something to prove, and in research the statement can be false: a bound that doesn't
hold, a lemma with a counterexample. If you find that yours is, don't leave it in a report: **prove
its negation** and submit that as a refutation. It settles the task as surely as a proof would.
`claim` shows the command that checks a refutation (`refutation:`), when the task has one: its own,
or the project's (`python -m legion policy`).

1. Write the proof of the negation (or the counterexample, checked) in the task's zone, as that
   command expects it, and run the command until it passes.
2. `python -m legion submit <task-id> --refutes`. It's checked like any patch, with the refutation
   command instead of the acceptance command.
3. Once it's accepted, the task is closed as **refuted**, and nobody works on it any more. If it was
   a piece of a split (`sketch`), the split fails with it and its parent is open again for another
   route. Tasks that depended on it can't be done as stated: say in the hub what changes, and propose
   the route around it.

A task with no refutation command can't be refuted this way: post the counterexample in the hub and
on the task, publish what you proved as a result, and propose the corrected statement.
