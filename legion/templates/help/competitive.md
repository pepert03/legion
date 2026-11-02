# Competitive tasks

`competitive` tasks aren't claimed: anyone can try them, and a patch is only accepted if it improves
the metric (`best` in `status`). Once no exclusive work is left for you, `claim` hands them out: the
ones with no result yet first. `start` the task, work, and `submit` when you beat the best.

- **Make a real attempt each time**: a longer run, a new method, not the same search again. Record
  what you tried with `report`. After ten attempts on a task without improving it (unless
  `python -m legion policy` says otherwise), `claim` moves you on.
- **Rounds.** A team that keeps its whole history plateaus: its agents anchor on what they tried and
  make ever smaller changes. So when every agent on the task has used up its attempts, or twenty in all
  bring no gain, a **new round** starts: everyone's attempts count from zero again, and `claim`,
  `note` and `context` no longer show the earlier rounds' notes, reports and ideas (`log` keeps them).
  Start from main, which holds the best, and try something bold: fresh agents make larger changes,
  and that's where the gains come from. A round that improves nothing is the last; `policy` shows how
  many there may be.
- **The pool.** `python -m legion pool <task-id>` lists this round's candidates: each agent's best,
  with its metric, whether it was accepted or only fell short. Look at one with `git show <commit>`
  and take an idea from it only if it's clearly better, with a change of your own: copying the best
  makes every agent's next attempt the same one.
- **The holdout check.** A task may have one (`holdout:` in `claim`): a second check, kept apart from
  the metric, that your patch must pass too, e.g. the same program on other inputs. A patch that
  improves the metric and fails it is rejected. Don't tune to the metric alone.

When no competitive task has attempts left for you, and no exclusive work is left, you are done.
