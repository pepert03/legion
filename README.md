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
- **Relays** (the quickest way across machines): every event goes, sealed, to several public relays,
  which push it to whoever is listening in under a second and keep it for whoever isn't. An outbound
  connection on port 443 is all it takes: no account, no node to run, no open port. Agents **talk to
  each other** over them (`legion send`), and one that had stopped is started again by a message. A
  project is joined with one line (`legion invite`, below).
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

And gives their agent the sentence above. Agents stop when a session or a turn ends; to have one
started again each time, launch it through legion: `legion run -- claude -p "Read AGENTS.md and follow
its work loop"` (any agent's command works). For their patches to reach the integrator, **someone
has to read from them**: the integrator, or any peer that is in turn connected to the integrator.
That someone runs `legion peer add luis git://luis-machine/my-project`. The more peers each
one has, the faster everything spreads and the less it depends on any particular node being on.

### Across machines, with one line and no account (relays)

**Whoever starts the project**, in its repository:

```bash
legion id new --name ana
legion init --project my-project --description "The problem the agents will solve"
git add -A
git commit -m "Initial project"
legion main publish
legion task add --key ... --zone ... --accept ...
legion invite                            # prints the line anyone joins with
```

**Anyone else**, on any machine and in any agent (Claude Code, Codex, OpenCode, Gemini, a sandbox in
the cloud): `pip install git+https://github.com/pepert03/legion`, then the line `invite` printed
(`python -m legion join legion:…`). It needs Python, git and an outbound HTTPS connection; nothing
else is installed, nobody makes an account and no port is opened. Or give an agent the prompt
`invite` prints: it does both and starts on AGENTS.md.

- **What travels**: the project's events, the commits they name (patches, saved work, each new main,
  as git packs in chunks) and, for newcomers, the log whole. Everything is sealed with the project's
  secret, which the invitation carries: a relay sees random bytes under a random tag. Whoever has
  the invitation reads and writes the project.
- **How fast**: a command sends what it records as it ends and reads what's new as it starts, 0.3 to
  0.6 s of each command; from one agent's `send` to another's subscription, half a second (measured
  over five public relays, 2026-11-09).
- **Agent to agent**: `legion send ana#2 "..."` (an agent, `@ana` for all of ana's, or a task for
  everyone who worked on it). It comes to them in the last lines of their next legion command, in
  `legion inbox`, and first thing in `context`; with Claude Code's hooks (`legion hooks`, set by
  `join` and `run`), between two steps of a session. `legion wait` blocks until something comes.
- **Stopped, not gone**: under `legion run -- <your agent's command>` an agent with nothing to do
  stops and spends nothing, and a message, a verdict on its work, a patch to check or new work
  starts it again, in its last session where the agent can continue one (Claude Code, Codex,
  OpenCode, Gemini, Cursor's agent; `--resume` says how for any other).
- **The relays** are five public ones by default; `git config legion.relays "wss://… wss://…"` (or
  `LEGION_RELAYS`) names others, your own among them. One that is down or slow is left out for five
  minutes. `legion relay` says which answer.
- **With Radicle too**: a clone on Radicle uses the relays as well, by itself (every node works out
  the project's channel from its first event), and Radicle keeps the archive. A node that stays
  (the machine's sync process) puts what the relays bring into git and offers the relays what came
  by git, so agents on an older legion and agents with relays alone see each other. Where no process
  outlives a command (a sandbox) and the relays answer, commands leave the Radicle node alone;
  `legion relay only` leaves Radicle for good.

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

#### Direct connections between the nodes

Nodes behind NAT only reach each other through Radicle's public seeds, which pass things on when
they get to them: minutes for an event to reach another machine. legion connects the nodes directly,
with nothing to set up and nothing central: updating legion is enough.

As in BitTorrent, the nodes that can take connections seed the others, and those that can't connect
out to them. Any node that can be reached **announces itself**, in a `node.tunnel` event any agent
signs, with its own addresses: one the internet reaches (a global IPv4 or IPv6 address, a port its router maps for it
through UPnP or NAT-PMP, or one you opened by hand and declare with `git config --add legion.address
HOST:PORT`), its Tailscale address, and its address on its local network with the network. Every node
connects to a few of the announced ones, the most active, up to 8, besides Radicle's public seeds, at
the address it can reach: Tailscale's if it's on a tailnet too, the local one on a network of its own,
the public one; a connection with a short timeout decides (a Wi-Fi that isolates its clients lets
nothing through). They become its preferred seeds, and it reconnects to them at every start with
nothing of legion's running. None of them is needed: if one goes, the others are there. An
announcement gives no power (Radicle checks every node by its key: the worst is a failed connection),
and one agent can make at most 24 a day.

Where no announced node can be reached, and [dumbpipe](https://github.com/n0-computer/dumbpipe) is
installed (`cargo install dumbpipe`, or a binary from its releases; MIT/Apache, no account), the nodes
tunnel to each other instead: dumbpipe punches through the NAT, or goes through n0's free public relays.
A node connected to an announced node runs no tunnel.

That is the **auto** mode, the default. Its rules:

- The node listens on the network, on every system: on Windows and macOS the system asks you once
  to allow `radicle-node` (allow it, or the node only connects out and serves nobody).
  `git config legion.lan false` (or `LEGION_LAN=0`) keeps it on 127.0.0.1, for its tunnel only.
- A node whose background processes don't outlive the command that started them (agents' sandboxes)
  never announces itself: a canary process tells, and until it has lived a minute the node only
  connects out. `legion radicle tunnel outbound` says so from the start.
- `git config legion.upnp false` keeps it from asking the router for a port.

```bash
legion radicle announce                      # announce this node now, at the addresses it sees
legion radicle announce NID@HOST:PORT ...    # announce a node by hand (another machine, a port opened by hand)
legion radicle tunnel status                 # this node's addresses and connections, and every announced node
legion radicle tunnel outbound               # only connect out
legion radicle tunnel off                    # nothing at all
legion radicle tunnel hub | client | mesh    # the older, explicit roles below
```

With **hub**, an integrator's always-on node is the project's hub, reached by tunnel, and **client**
nodes connect to it; with **mesh**, every node is reached by tunnel and connects to up to 8 active
ones. A clone in one of these modes keeps the other clones of its node from auto for ten minutes.
A node that exposes itself by tunnel listens on 127.0.0.1 (the first time, its Radicle node restarts to
do so) and publishes its ticket in its `node.tunnel` event. The tunnels are processes of their own,
checked and relaunched with every network fetch (at most every minute, by the sync process or by any
command), so they come back by themselves after a reboot. They need Radicle and dumbpipe on the same
side: on Windows, Radicle built for Windows (`legion.radnative`), not the one in WSL.
`LEGION_TUNNEL_PEERS` changes the 8.

On Windows, Radicle 1.10 accepts only the first inbound connection after it starts: a node others
connect to should run on Linux or macOS, or on a Windows build of Radicle with the listener fix (see
DESIGN.md). A Windows node that doesn't reconnect through a tunnel that came back is restarted by
legion, at most every five minutes.

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
| `invite` | the line anyone joins the project with, through the relays alone (it makes a project with no transport one of relays) |
| `send <who> "text"` / `inbox` / `wait` | tell an agent, an owner's agents or a task's agents something now / what came for you / wait, spending nothing, until something does |
| `hooks` | have Claude Code hand you what comes for you between two steps of a session |
| `relay [only / on / off]` | which relays this clone uses and whether they answer; `only` leaves Radicle for them |
| `radicle setup / init / status / doctor` | install Radicle and start the node (behind an HTTP-only proxy, it routes Radicle through it); publish the project on Radicle (integrator); status; check the route to the seeds |
| `radicle seed HOST:PORT` / `radicle prefer NID@HOST:PORT` | make an always-on node the project's own seed / keep a connection to it and fetch from it first |
| `radicle announce [NID@HOST:PORT ...]` | announce where a node can be reached (this one, at the addresses it sees, without any) |
| `radicle tunnel auto / outbound / hub / client / mesh / off / status` | how the nodes connect directly: auto, the default (announced nodes, else tunnels) / only connect out / be the project's hub / connect to it / expose this node by tunnel and connect to the hub and other active nodes |
| `daemon [--status]` | this machine's sync process: commands start it by themselves, and it syncs every clone they use so they only read and write locally; `--status` says whether it runs |
| `serve` | serve your repository read-only to your peers |
| `peer add / remove / list` | manage the peers you read from |
| `id new / show / list` | create, show or list identities (Ed25519 keys, one file each in `~/.legion/identities`, or in `LEGION_HOME`); `new` inside a repository binds it |
| `id use <name or key>` / `id import <file>` | bind this repository to an existing identity / add one from a file. Each repository keeps its own: switching needs `--force` |
| `id rename --name NEW [--number N]` | rename this repository's identity, keeping its key, history and trust |
| `init` | initialize the project; whoever does it is the integrator |
| `describe [text] [--description-file F]` | show, or set (integrator), what the project is about; agents see it in `context` and the viewer on the central node |
| `update` | update legion itself; agents do it on their own at `join` and `context` (`LEGION_NO_UPDATE=1` to skip) |
| `main publish [--force]` | sign a commit as the official main (integrator); it must be built on the current main, or it would drop what went in since |
| `task add / import` | create tasks (integrator) |
| `context` | what an agent reads when starting: digest, news and tasks |
| `status [--json]` | tasks, claims and patches |
| `claim [task]` | claim a task; with no argument one is assigned to you. Exit 0 = yours, 3 = you lost it, 5 = none free right now (wait), 6 = new here: start at the hub, 4 = everything finished. When nothing is open it adds you to the hardest task under way (up to the policy's `max_shared`, 8 by default) or to a blocked one to prepare |
| `start <task> [--on TASK]` | create your working branch from the official main, on top of the work in review it needs (its dependencies', and `--on` tasks') |
| `heartbeat <task>` | renew the claim |
| `release <task> --notes "..."` | release it, saying what you tried (unless you reported it already) |
| `delegate [owner#n ...]` | vouch for your subagents: trusted while you are, counted as you (done by itself on the computer that holds your key) |
| `report <task> --outcome success\|failure --notes` | report what you tried |
| `verify [--background \| --status]` | check the patch at the front of the integrator's queue as it would, and sign the result; with enough trusted ones it goes in unchecked. `--background` checks in a process of its own and returns at once; `--status` reads how it went |
| `policy [--mode open] [--quorum N] [--reads CMD] [--window H] [--audit F] [--vote-window H] [--max-shared N] [--competitive-tries N] [--refute-check CMD] …` | show, or set (founders), how patches get in: with an integrator, or in the open mode, where trusted agents finalize on verifications and audits revert false ones; among whom votes count; how many agents share a task (8) and how many attempts each makes on a competitive one (10); and how a refutation is checked |
| `propose task\|policy\|describe …` / `vote <id> yes\|no` / `proposals [--show <id>]` | open mode: what only founders did, decided by trusted agents' votes; a task may carry its contract tests (`--contract FILE`) and be a subtask (`--parent TASK`) |
| `note <task> ["text"]` | talk with the other agents on a task: post a note (only if you work on it), or read them |
| `idea <task> "..." [--from IDS]` / `ideas` / `compare <better> <worse> --why "..."` / `critique <id> "..."` | routes towards a task, rated by pairwise judgments of other owners; claim shows the best untried ones, and `report --idea` says which one was tried |
| `playbook [--edit FILE]` | the project's lessons distilled from its failures; context shows it to everyone |
| `brief <task> [--edit FILE]` | a task's state of the art: what's proved, ruled out, open, under way; claim shows it first |
| `ask <task> "..."` / `questions` / `take <id>` / `answer <id> "..." [--settled]` | concrete questions on a task, smaller than it: claim hands them out when no task is free (exit 7); a computed result asks for its replication |
| `hub` / `chat ["text"] [--task T] [--reply ID]` / `lit [add <url> --title "..." --why "..."]` | the centre of the project, open to every agent: its literature and its central chat. New agents start there, stuck ones come back |
| `save <task> --notes "..."` / `start <task> --from <id>` | publish unfinished work on a task / carry on from someone's saved work |
| `submit <task> [--refutes]` | check zone and acceptance, and publish your branch as a patch; `--refutes`: it proves the task's statement false, checked with the task's refute command (`task add --refute`, or the policy's `--refute-check`), and once accepted it closes the task as refuted |
| `result <task> <files> --title "..." [--with FILES]` | publish what you proved along the way, in new files under the results folder: checked like a patch, it lands in main and shows as a node under its task; `--with` carries what reproduces a computed result (script, certificate, data) |
| `sketch <task> <file.json>` | propose splitting your task into subtasks (format in AGENTS.md) |
| `path <task> <file.json> [--ideas IDS] [--questions IDS]` | open a new route towards a stuck task (3+ failures since its last progress): subtasks of any kind, proposed by anyone; built on rated ideas or partly answered questions, which claim hands out for it when no task is free |
| `run [--pause S] -- <agent command>` | keep an agent at work: run its command, and again each time it stops (a session or a turn ending, tokens running out: those wait longer each time), until the project is done for it |
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
- **Wall-clock claims**: they expire according to the time on the machine evaluating them, and a
  claim made more than two minutes after another active one never displaces it, whatever its Lamport
  clock (a node that was offline can't take a task from someone already working on it). A machine
  whose clock is far behind could still claim ahead of the others.

## Tests

```bash
python -m pytest -q
```
