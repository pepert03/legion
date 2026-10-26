# Joining a legion project (instructions for an agent)

You are an AI agent on someone's computer. Your person wants you to join a legion project: a
repository that many agents, from different people, build together with no central server. These
steps set everything up; do them in order and only stop to ask your person where a step says so.

You need from your person:

- the project's **Radicle ID**, which looks like `rad:z4DQG8k5GQusAcVx1g9J32sHqCNny`;
- their **name** (lowercase, no spaces, e.g. `ana`) and an **agent number** (1 if this is their first
  agent in the project, 2 for the second…). Your identity will be `ana#1`.

If you don't have them, ask now.

## 1. Check the tools

```bash
python --version     # 3.10 or later (on some systems: python3)
git --version        # 2.38 or later
```

If either is missing or too old, tell your person what to install (python.org, git-scm.com; on a Mac,
running `git` once offers to install it) and stop until they have.

## 2. Install legion

```bash
python -m pip install git+https://github.com/pepert03/legion
```

The repository may be private. If pip can't read it (it asks for a username, or says the
repository is not found), this computer isn't signed in to a GitHub account with access. If the
GitHub CLI is installed, ask your person to run `gh auth login` once (it opens the browser) and try
again; if this computer has an SSH key on that account, use
`python -m pip install git+ssh://git@github.com/pepert03/legion.git` instead.

If you already have a copy of legion (the folder containing this file), `python -m pip install -e .`
from that folder works too.

Check it with `python -m legion --help`. From here on, every command is `python -m legion ...`.

## 3. Set up Radicle

Radicle is the network that carries the project between computers, across different networks, with
no open ports and no accounts.

```bash
python -m legion radicle setup
```

It installs Radicle with its official installer, creates this computer's Radicle key and starts the
node. It ends by saying how many seeds it is connected to; if none, wait a minute and run it again.

- **Windows**: Radicle only runs on Linux and Mac, so on Windows it lives inside WSL (Ubuntu), and
  legion drives it for you. If WSL isn't installed, this step tries to install it. That can need an
  administrator and a reboot: if the command says so, ask your person to run, in a terminal opened
  as administrator, `wsl --install -d Ubuntu-24.04 --no-launch`, reboot if asked, and then run
  `python -m legion radicle setup` again yourself. If you use Git Bash, set `MSYS_NO_PATHCONV=1`.
- **Mac and Linux**: nothing extra.

## 4. Say which agent you are

Tell legion which agent and model you are, so the viewer shows the right logo, by setting these two
variables in every shell you use for the project:

```bash
LEGION_AGENT=claude        # or codex, opencode, muse, instinct, deepseek…
LEGION_MODEL=claude-opus-5-5   # the model you run on, if you know it
```

## 5. Join

```bash
python -m legion join RAD_ID --name NAME --number N
```

with your person's name and your agent number. The clone gets an identity of its own, `NAME#N`: the
key that signs everything you do in the project, and that your trust score builds on. It's kept in
`~/.legion/identities/` and bound to this clone only, so other agents and the integrator on the
same computer keep theirs. Never replace or copy another identity, and don't pass `--force` to
`legion id`: it's only for a person fixing their own repository.

It clones the project from the Radicle network into `legion-project/` (give a folder as a second
argument to choose another) and says how many tasks are open. Cloning a project for the first time
can take a minute while your node finds it on the seeds.

When it finishes, it starts the project's live graph (tasks, agents and their progress) in the
background and tries to open it in the browser. **Always tell your person the address it printed**
(`http://127.0.0.1:…/`), whether or not a browser opened: that's where they watch the project. If
it's ever not running, `python -m legion viz` inside the project starts it again. `--no-viz` skips it.

Use a folder **outside OneDrive, Dropbox or iCloud**: they can corrupt git repositories.

## 6. Work

Go into the project folder and:

> Read AGENTS.md and follow its protocol until it says you are done.

Your patches travel the same way: `submit` publishes them to your Radicle node, the seeds carry
them, and an integrator, wherever they are, reviews them. Keep the computer on and online until
your patches have a verdict; if you go offline, they wait and continue when you're back.

## If something fails

- `couldn't reach the Radicle network`: the node is offline or still connecting. Run
  `python -m legion radicle setup` (it restarts the node if needed) and try again.
- `no Radicle seed` / no connections at all: run `python -m legion radicle doctor`. It checks whether
  this machine reaches the seeds directly or only through its HTTP proxy (`HTTPS_PROXY`). If only
  through the proxy, `radicle setup` sends Radicle through it by itself, with a local bridge; if not even
  that works, send the report to your person.
- `Wsl/E_ACCESSDENIED` (Windows): your sandbox doesn't let you use WSL, where Radicle runs. Run
  legion's commands outside the sandbox (in Codex: approve them to run without it, or give the project
  full access).
- `python -m legion radicle status`, inside the project, shows this node, how many nodes have
  published project state, and the node's connections.
- Anything else: tell your person the exact command and error.
