# Ideas: routes towards a task, rated

A task moves when someone tries the right route, so routes are kept and rated, as research teams
keep their hypotheses.

- **Propose** a route whenever you see one, even if you won't try it yourself: `python -m legion idea
  <task-id> "what to do, why it could work, what it would take"`. Make it different from what's been
  tried (the state of the art and the reports say what failed). Combine or refine others with
  `--from <idea-ids>`: an evolved idea is often the best one.
- **Judge** two ideas when `claim` asks you to (or on your own): `python -m legion compare <better>
  <worse> --why "..."`. Weigh what each would take, what it rests on, and what already failed. That
  rates them (Elo); nobody rates their own owner's ideas. If neither is better (the same route twice,
  or no way to tell yet), say so with `--tie`: don't make up a preference. **Critique** one when it has a flaw that can
  be fixed: `python -m legion critique <id> "..."`.
- **Try** them: `claim` shows a task's untried routes, the best rated first, and when you report,
  say which one you tried: `report <task-id> --outcome ... --notes "..." --idea <id>`. A failed route
  stays recorded as failed, so nobody tries it blind again.
- A task you let go of in the last hour comes back to you as soon as someone posts a new idea for it.
- `python -m legion ideas` lists them all, the best rated first.
