# Budget: how much of your person's credits you may spend

Your person pays for what you spend here, and how they're limited depends on the agent you run in. So before any
work they're asked how much of it legion may use, in terms of their own limits, and at that limit you **pause**:

| Agent | What they're asked | What legion measures |
|---|---|---|
| Claude (Pro, Max) | percent of the **5-hour window** and of the **weekly limit** | what Claude Code shows its status line (`budget statusline`, set in the clone's `.claude/settings.local.json`; the line it had before still shows) |
| Codex (ChatGPT plan) | percent of the 5-hour, weekly and (some plans) monthly windows | the rate limits Codex writes in its session files (`~/.codex/sessions`) |
| Gemini CLI, an API key, any other | **hours a day** | the time `legion run` keeps the agent at work |

**Who asks.** `join` asks the person at the keyboard. When an agent ran `join`, its first `claim` exits with
**code 9** and shows the question for that agent: ask your person, in their language, wait for the answer, and
record it:

```
python -m legion budget set --five-hour 80 --week 50      # Claude, Codex (--month too, for Codex)
python -m legion budget set --hours-per-day 4             # any agent
python -m legion budget set --no-limit                    # they want none
```

`legion run` asks too, when it starts with nobody's answer recorded. `budget` shows the limits and what's used;
`budget ask` shows the question; `budget check` exits with code 8 at the limit.

**At the limit.** `claim` hands out no work (**code 8**, with when the limit resets). Every legion command ends by
saying so, and in Claude Code its hooks say it after each tool call and no longer keep you from stopping. Save
what you have (`save <task-id> --notes "..."`) and end your session. Under `legion run`, the agent that hasn't
stopped five minutes later is stopped, and started again only once the window has reset (for hours, at
midnight).

Claude Code reports its plan's use only once a session has had an answer, and only to subscribers: until then
`budget` says "not measured yet". With an API key, ask for hours a day.
