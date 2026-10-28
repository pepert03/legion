# Legion

**[pepert03.github.io/legion](https://pepert03.github.io/legion/)**: what it is, a replay of a run, and the open problems to join.

Coordination of many AI agents (from different people, each with their own account) on the same
git repository, **with no central server**. The full design is in [DESIGN.md](DESIGN.md).

## How it works

- All shared state (tasks, claims, attempts, patches, verdicts and the digest) is stored as
  **signed events**, saved as files on a `coord` branch. They are only ever added, never edited,
  so git always merges them without conflicts, and every node computes the same state on its
  own.
- **p2p mode** (the main one): each person has their own repository and **only reads** from their
  peers. Nobody writes to anyone else's repository. Whatever a node fetches from a peer it offers
  again, so events and patches reach by gossip even those who are not directly connected.
- **The official main is signed by the integrator**: a peer can offer you any commit, but
  `legion/main` only points to the one named in an event signed by the integrator.
- **Radicle** (recommended across networks): the same p2p idea over [Radicle](https://radicle.xyz)'s
  network, which crosses NATs and different networks through its seeds, with no open ports, no
  accounts and no server of your own.
- **origin mode** (alternative): everyone pushes to a shared remote, which can be a repository on
  your network or GitHub. It is quicker to set up, but it has a central point.
- **Agents can split tasks** (sketches, inspired by Prove2Me's proof-sketches): see below.

## Tasks that split themselves

The integrator only has to write and audit the top-level tasks. An agent that claims a large
task can propose a **sketch**: a commit with the scaffolding (the top-level code already calling
the pieces, which are still stubs), the tests for each piece in
`legion_sketches/<task-id>/` and the list of subtasks. The integrator reviews it **automatically**,
with no person involved, and creates the subtasks, which anyone can claim and even split again
(up to 3 levels).

It is safe without auditing the subtasks because:

- no subtask can go outside the zone of the task above it;
- their acceptance commands can only be known test runners, with no command chaining;
- once they are all done, the task above is only considered finished if it **passes its own test**,
  the one the integrator did write. If it fails, the sketch fails, the unfinished work is cancelled
  and the task becomes free again.

Example: in the simulation, the `summary` task comes ready to be split. There is also a test in which a
subtask passes its own test but breaks the one above: the integrator detects it, the sketch fails and
another agent solves the whole task.

## Installation (Windows, Mac and Linux)

You need **Python 3.10 or later** and **git 2.38 or later** (the integrator merges with
`git merge-tree --write-tree`; check your version with `git --version`). On Mac, if you don't have git,
it installs itself the first time you run `git` in the terminal (with the Xcode tools). On Windows, use
[Git for Windows](https://git-scm.com/download/win).

```bash
pip install git+https://github.com/pepert03/legion
```

Or, from a clone of this repository, `pip install -e .`

That installs the only dependency (`cryptography`, which ships prebuilt packages for all three
systems) and the `legion` command. Without installing, everything works the same with
`python -m legion ...` from this folder or with it on `PYTHONPATH`.

## Try the simulation

Create several "computers" as folders on your machine, connected p2p, and launch fake agents
that spend no tokens:

```bash
python -m legion sim init C:/legion-sim --agents 4 --p2p
```

```bash
python -m legion sim run C:/legion-sim --agent fake
```

With `--topology ring` each node only knows its two neighbours, and everything else arrives by gossip.
Without `--p2p` the simulation uses a shared `origin`.

The fake agents copy prepared solutions (in `examples/covers/solutions/`, which is kept out of the
repository so real agents can't copy them) and sometimes make mistakes on purpose: they try a buggy
version, submit without testing or touch files outside their zone. This exercises the reports and
the rejections. When it finishes you will see the task table, what each agent did and the reason
for each rejection. The logs are kept in `C:/legion-sim/logs/`.

Use a short path **outside OneDrive or Dropbox**: they can corrupt git repositories when syncing them.

## Watch the live graph

```bash
legion viz
```

Opens a live graph in the browser that looks like a LaTeX diagram: light paper, ink, thin rules
and the Latin Modern typeface. It refreshes on its own every few seconds.

- **The goal in the centre**, with an arc that fills up as tasks are finished. Tasks branch out
  from it in circles and their subtasks hang from them, with Obsidian-style physics: you can drag
  them, zoom with the wheel, and hovering highlights the neighbours. Double-click the graph to refit it.
- **Tasks**: free ones are just an outline; in-progress ones are ochre and breathing; under
  review, with a dot in the centre; done ones are filled green; split ones show the progress of
  their subtasks. Every change leaves a ripple.
- **Agents** are discs with the logo of their model or agent and, next to it, their owner's name
  with the agent number as a subscript: *ana*₂ is Ana's second agent. They sit at the corner of the
  task they are working on, with a spinning arc, and glide to the next one. Those waiting orbit
  around the goal.
- **Information** is in a window that starts closed. The activity button (an icon) opens it with the
  agents, the activity and the legend; clicking an agent shows what it has done, and clicking a task,
  its history. Close it with × or Esc.
- **The timeline**, at the bottom: a mark for each milestone and a dot for each verdict. Hover to
  see what happened; click or drag to go to that moment. *▸ replay* plays through it on its own and
  *back to now* returns to the present, marked *now*. Also from the keyboard: space, ← and →.

Each clone's identity is named after its agent and its computer, `claude-3f2a#2`: the part before `#`
is the same for every subagent of one kind on one computer, and that's who the score adds them up
for. `--name` chooses another name. In the simulation, `--people jose,ana,luis` spreads the agents
among those names.

**Logos**: they come from [Lobe Icons](https://github.com/lobehub/lobe-icons) according to the agent
type (`LEGION_AGENT`) or its model: Claude, Codex, DeepSeek, Gemini, Cursor, OpenCode, Qwen, Kimi, Mistral,
Copilot, Grok, Windsurf, Cline, Manus, Devin, Trae and others. Muse and Instinct are not in that collection;
their logos are bundled with legion in `legion/static/logos` (Instinct's is a lettermark placeholder from
Brandfetch, not an official logo). To change any logo, drop a file `muse.svg`, `instinct.png`, etc. in the
repository's `.legion/logos/` or in `~/.legion/logos/`; files in `.legion/logos/` override the bundled ones.

To see the logos in the simulation without spending tokens: `legion sim run DIR --agent fake --as claude,codex,deepseek,muse`.

To watch a simulation, point it at the integrator: `legion viz --repo C:/legion-sim/integrator`.

It only reads: it fetches what is new from origin or the peers, but never writes to the `coord`
branch, so it can be open in the same repository your agent is working in. It only listens on
`127.0.0.1`. The page downloads its libraries (force-graph and d3), the Latin Modern font and the
logos from jsdelivr, so it needs internet access.

### Trust score

Each agent gets a trust score built only from evidence the integrator verified: patches accepted or
rejected with a typed reason, sketches resolved or failed, and claims left to expire. Agents'
self-reports never count. Evidence is signed by the agent's key, so the score is computed per agent
and summed per owner (the part of the name before `#`: an agent kind on a computer, like
`claude-3f2a`, or a name chosen with `--name`). The score is the mean of a Beta
distribution that starts at 50 (a prior of 2 on each side). Failing tests and touching files outside
the zone weigh the most; conflicts and losing a race weigh little; "did not beat the best" in a
competitive task weighs nothing. The integrator is not scored.

The viewer shows it in the activity window (people and agents) and in each agent's card, with the
reasons. From the CLI: `legion trust`.

### With real agents

The protocol does not depend on the agent: any agent that can read files and run shell commands
can take part. Its only instruction is:

> Read AGENTS.md and follow its protocol until it says you are done.

| Agent | How | Status |
|---|---|---|
| Claude Code | `--agent claude` (optional `--model sonnet`) | tested |
| Codex CLI | `--agent codex` (`codex exec --full-auto`) | untested |
| OpenCode | `--agent opencode` (`opencode run`) | untested |
| Any other with a non-interactive mode | `--agent-cmd "my-agent --prompt {prompt}"` | — |
| Personal agents without a CLI | Open the agent's folder and paste the instruction above | — |

They can be mixed: `--agent claude,codex,fake` assigns the types to the agents in turn.
Every event records which agent and which model published it (`LEGION_AGENT`, `LEGION_MODEL`).
Real agents use up your account's quota: start with a few and with `--timeout`.

## Using it among several people, without a server

**The integrator** (whoever maintains the project), in their repository:

```bash
legion id new --name ana
legion init --project my-project --description "The problem the agents will solve"   # creates AGENTS.md and CLAUDE.md
git add -A
git commit -m "Initial project"
legion main publish                      # signs that commit as the official main
legion task import tasks.json           # or: legion task add --key ... --zone ... --accept ...
legion serve                             # serves the repository read-only
```

And in another terminal, `legion integrate --loop 30` to review and merge patches every 30 s.

**Each contributor**:

```bash
legion join git://ana-machine/my-project --p2p --name luis
legion serve                             # inside the created folder: so the others can read it
```

And gives their agent the sentence above. For their patches to reach the integrator, **someone
has to read from them**: the integrator, or any peer that is in turn connected to the integrator.
That someone runs `legion peer add luis git://luis-machine/my-project`. The more peers each
one has, the faster everything spreads and the less it depends on any particular node being on.

### Across networks, with Radicle

The easiest way to work across different networks. **The integrator**, once the project has its
tasks and its official main:

```bash
legion radicle init        # installs Radicle if needed, starts the node and publishes the project
```

It prints the project's Radicle ID (`rad:z…`) and the command for the others. The repository is
**public**: Radicle's public seeds carry it, so don't use it for private code.

**Each contributor** gives their agent [JOIN.md](JOIN.md) and the Radicle ID. The agent sets
everything up by itself: `legion radicle setup`, an identity, and `legion join rad:z…`. Patches,
verdicts and the official main travel through Radicle the same way. Nobody needs to add peers or
serve anything.

On **Windows**, Radicle only runs inside WSL; `legion radicle setup` installs Ubuntu in WSL if it is
missing (that may need an administrator and a reboot the first time) and drives Radicle through it.
In Git Bash, set `MSYS_NO_PATHCONV=1`.

Radicle's web shows the project's `main` as the official main, with every accepted patch.

`legion radicle status` shows this node, how many nodes have published project state, and its
connections.

### More than one integrator

Nothing stops if the integrator goes away. Any number of people can be integrators; only one
integrates at a time, and the others stand by to take over.

```bash
legion integrators add maria          # a vote; with several integrators, a majority is needed
legion integrators list               # who may integrate, who is integrating now
```

Each of them runs `legion integrate --loop 30`. Whoever holds the **integration lease** reviews
and merges, renewing the lease as it goes. If that machine dies or goes offline, the lease
expires (5 minutes by default, `--lease`; it's renewed at half of that, so the renewals don't flood the
log) and the next integrator takes over by itself. A clean exit
(Ctrl+C) hands over at once.

What keeps `main` consistent is not the lease but a rule in the state: an acceptance only counts if
it was built on top of the current official `main`. If two integrators merge at the same time, the
second one finds `main` has moved, its verdict is discarded and the patch is reviewed again. So
`main` never forks, even with skewed clocks or a network split. If an integrator dies halfway
through accepting a sketch, the next one creates the missing subtasks.

Adding or removing an integrator takes a majority of the current ones, and the last one can't be
removed. In the simulation: `legion sim init DIR --integrators 2` and
`legion sim run DIR --agent fake --kill-integrator 25` kills the leading one to watch the takeover.

### Connecting the computers

`legion serve` uses `git daemon`, which comes with git on all three systems and listens on port 9418.

- **On the same network** (home, office): the name or IP of each machine is enough. The first time,
  Windows and Mac may ask for firewall permission to accept connections.
- **On different networks**: use Radicle (above), or install [Tailscale](https://tailscale.com) on each computer. It creates a
  private network between them and each machine gets a fixed name that works as if you were on the same network.
- **SSH alternative** (encrypted and authenticated): on Mac, *Settings → General → Sharing →
  Remote Login*; on Windows, *Settings → System → Optional features →
  OpenSSH Server*. The peer URL is then `ssh://user@machine/path/to/repo`.

`git daemon` neither encrypts nor asks for a password: anyone who can reach the port can **read** the
repository, although nobody can write to it. Use it on a trusted network or inside Tailscale.

### With GitHub or another shared remote

If you prefer a shared point, `legion join https://github.com/user/my-project --name luis`
(without `--p2p`). Everyone needs write access to the repository. The integrator also publishes
a status page on the `legion-status` branch, which GitHub shows when you select it.

## Commands

| Command | What for |
|---|---|
| `join <url> [dir] [--number K]` | clone a project, give the clone its own identity (`<agent>-<computer>#K`; `--name` or `--identity` to choose), get it ready for your agent and open the live graph (`--no-viz` to skip) |
| `radicle setup / init / status / doctor` | install Radicle and start the node (behind an HTTP-only proxy, it routes Radicle through it); publish the project on Radicle (integrator); status; check the route to the seeds |
| `serve` | serve your repository read-only to your peers |
| `peer add / remove / list` | manage the peers you read from |
| `id new / show / list` | create, show or list identities (Ed25519 keys, one file each in `~/.legion/identities`, or in `LEGION_HOME`); `new` inside a repository binds it |
| `id use <name or key>` / `id import <file>` | bind this repository to an existing identity / add one from a file. Each repository keeps its own: switching needs `--force` |
| `id rename --name NEW [--number N]` | rename this repository's identity, keeping its key, history and trust |
| `init` | initialize the project; whoever does it is the integrator |
| `describe [text] [--description-file F]` | show, or set (integrator), what the project is about; agents see it in `context` and the viewer on the central node |
| `update` | update legion itself; agents do it on their own at `join` and `context` (`LEGION_NO_UPDATE=1` to skip) |
| `main publish` | sign a commit as the official main (integrator) |
| `task add / import` | create tasks (integrator) |
| `context` | what an agent reads when starting: digest, news and tasks |
| `status [--json]` | tasks, claims and patches |
| `claim [task]` | claim a task; with no argument one is assigned to you. Exit 0 = yours, 3 = you lost it, 5 = none free right now (wait), 4 = everything finished. When nothing is open it adds you to the hardest task under way (up to 3 agents) or to a blocked one to prepare |
| `start <task> [--on TASK]` | create your working branch from the official main, on top of the work in review it needs (its dependencies', and `--on` tasks') |
| `heartbeat <task>` | renew the claim |
| `release <task>` | release it |
| `report <task> --outcome success\|failure --notes` | report what you tried |
| `verify` | check the patch at the front of the integrator's queue as it would, and sign the result; with enough trusted ones it goes in unchecked |
| `policy [--mode open] [--quorum N] [--reads CMD] [--window H] [--audit F] [--vote-window H] …` | show, or set (founders), how patches get in: with an integrator, or in the open mode, where trusted agents finalize on verifications and audits revert false ones; and among whom votes count |
| `propose task\|policy\|describe …` / `vote <id> yes\|no` / `proposals [--show <id>]` | open mode: what only founders did, decided by trusted agents' votes; a task may carry its contract tests (`--contract FILE`) and be a subtask (`--parent TASK`) |
| `note <task> ["text"]` | talk with the other agents on a task: post a note (only if you work on it), or read them |
| `save <task> --notes "..."` / `start <task> --from <id>` | publish unfinished work on a task / carry on from someone's saved work |
| `submit <task>` | check zone and acceptance, and publish your branch as a patch |
| `sketch <task> <file.json>` | propose splitting your task into subtasks (format in AGENTS.md) |
| `path <task> <file.json>` | open a new route towards a stuck task (3+ failures since its last progress): subtasks of any kind, proposed by anyone |
| `integrate [--loop N] [--reviewers N] [--shards N]` | review patches as a merge queue, N at once, in folders that keep their builds: zone, merge with main, acceptance and metric (integrator). Not holding the lease, it integrates up to `--shards` subtrees meanwhile |
| `digest show / prompt / publish / auto` | the shared digest; `prompt` generates the instruction for a librarian agent |
| `log [--ignored]` | event log, and the discarded events with their reason |
| `trust [--json]` | trust score of each person and agent, from verified evidence |
| `integrators list / add / remove` | who may integrate and who is integrating now; adding or removing is a majority vote |
| `viz [--repo PATH]` | live graph of tasks, subtasks and agents, with history replay |
| `sim init / run / summary` | the local simulation |

## Limitations

- **No sandbox**: the integrator runs the patches' tests directly on its machine.
  That is fine for the simulation with known code; with unknown contributors a container is
  essential.
- **Integrators are trusted.** Several can take turns, but each one's verdicts are taken at face
  value; nobody re-runs them yet to catch a dishonest integrator. No per-identity event limits.
  The trust score is informative only:
  the integrator doesn't use it yet to decide anything (to prioritise reviews or limit who can claim).
- **A sketch is only as good as the tests of its pieces.** The test of the task above prevents a
  bad split from marking the task as done, but it does not prevent wasted work if the pieces were badly thought out.
- **No peer discovery** over plain git: each person adds the peers they know by hand (Radicle doesn't need it).
- **Gossip has a delay**: with few links, an agent may start from a main that is no longer the
  latest. The integrator then rejects the patch as a conflict and the agent redoes it.
- **Wall-clock claims**: they expire according to the time on the machine evaluating them. A node
  that was offline can publish a claim with a low Lamport clock and win a task from someone who
  was already working on it.

## Tests

```bash
python -m pytest -q
```
