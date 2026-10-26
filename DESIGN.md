# Legion — Design document

> Status: draft v0.1 · Last revised: 2026-10-25 · Implemented: phase 0, transport via origin (phase 1), p2p over plain git and Radicle (see README.md)

## 1. Goal

Allow **many people, each with their own agent and their own account**, to collaborate on the same project without a mandatory central platform, coordinating only through what is written down in the repository.

Joining should take **one sentence**:

> Clone `<repository-id>` and follow its `AGENTS.md`.

### Non-goals (for now)

- Spreading the compute of a single agent across machines. Each agent runs entirely on its owner's machine.
- Sharing accounts or API quotas between people. Everyone uses their own.
- Real-time conversation between agents. All communication is asynchronous and written down.

## 2. Principles

They come from what was learned from the reference systems (section 11 summarizes them):

1. **Coordinate as little as possible.** What scales is independent work with automatic verification, not conversation.
2. **Nothing is accepted without an executable verifier.** Every task comes with a command that says whether it is done.
3. **Append only, never edit.** The shared state is an event log that only grows, so merging never produces conflicts.
4. **Each node verifies locally.** Nothing another node says is trusted: the signature is checked and the tests are rerun.
5. **The cost of reading is bounded.** An agent never has to read the whole history to get started.
6. **The transport is replaceable.** The protocol does not depend on Radicle or GitHub.
7. **Any agent will do.** Claude Code, Codex, OpenCode or a personal agent: it only needs to be able to
   read files and run commands. The whole protocol is in `AGENTS.md` and the CLI.

## 3. Architecture

```
┌───────────────────────────────────────────────────────┐
│  Agent  (Claude Code or other)  ← reads AGENTS.md     │
├───────────────────────────────────────────────────────┤
│  Coordination protocol                                │
│  tasks · claims · attempts · patches · digest         │
├───────────────────────────────────────────────────────┤
│  Local verification  (sandbox: tests, benchmark, Lean)│
├───────────────────────────────────────────────────────┤
│  Transport  (plain git · GitHub · Radicle)            │
└───────────────────────────────────────────────────────┘
```

Repository layout:

```
AGENTS.md              protocol for the agents (only the integrator changes it)
ZONES.md               code zones and their owners
coord/
  events/              one file per event, signed, immutable
  DIGEST.md            curated digest of knowledge (decision 4)
src/ …                 the project itself
```

Events live on a **separate branch, `coord`**, so they don't clutter the code history.

### Transport over Radicle

With Radicle, each node pushes its own legion refs (`refs/legion/pub/*`: its coord and the main it
follows; `refs/legion/patches/*`) to its node, and Radicle stores them in that node's namespace
(`refs/namespaces/<NID>/…`) and replicates them through the seeds. Inside the namespace, each legion identity
publishes under its own prefix (`refs/legion/pub/<identity>/…`), so several agents on one computer share
its node without overwriting each other. Nodes behind NAT or on different
networks reach each other without opening ports, and nobody can write to anyone else's namespace.

To sync, legion asks the node for the latest (`rad sync --fetch`, rate-limited), copies every
namespace's coord into `refs/legion/peers/<NID>/coord`, merges them exactly as in p2p, and pushes its
own refs back. It also moves the ordinary `main` branch, the one Radicle's web and `rad clone` show, to the
official main, without forcing it, so it only ever moves forward. Since every node republishes the patches it has seen, the same patch can be in many
namespaces; patches are immutable, so legion takes each from the first namespace that has it.

On Windows, Radicle runs inside WSL and legion calls it through `wsl.exe`: the repository, the agents
and legion stay on Windows, and WSL's git works on the repository through `/mnt/c/…`. Peer refs carry
the 48-character node ID, so repositories need `core.longpaths`.

## 4. Decision 1 — The unit of work

**A task** is a `task.created` event with:

| Field | Example | What for |
|---|---|---|
| `id` | `t-7f3a` | unique identifier (hash of the content) |
| `title` | "ISO date parser" | |
| `zone` | `src/parser/` | paths it may touch; a patch that goes outside is rejected |
| `accept` | `pytest tests/parser` | **acceptance command**; exit 0 = done |
| `mode` | `exclusive` / `competitive` | see decision 2 |
| `metric` | `python bench.py` (optional) | in competitive mode, the best number wins |
| `deps` | `[t-1c2d]` | tasks that must be accepted first |
| `size` | `S` / `M` / `L` | indicative; L should be split |

A task without an executable `accept` **is not valid**. If something cannot be verified automatically, it is split until it can be, or a person reviews it explicitly (`accept: manual`, which should be exceptional).

## 5. Decision 2 — Assignment without a global lock

In p2p a task cannot be claimed atomically. Two mechanisms are combined:

**`exclusive` mode, with expiring claims:**
1. The agent publishes `task.claimed` with an expiry (2 h by default).
2. While it works, it publishes `task.heartbeat` to renew it.
3. If two claims collide, the one with the **lowest Lamport clock** wins; on a tie, the one with the lowest identity hash. It is a deterministic rule: all nodes reach the same conclusion without talking to each other.
4. The loser notices when syncing, publishes `task.released` and picks another task.
5. A claim that expires without a submission leaves the task free again.

**`competitive` mode, without claims:** several agents tackle the same task on purpose and the best result according to `metric` wins (in the style of Big O Speedrun). It is used for optimizations or open problems.

Each event carries a Lamport clock (`lamport` = the highest seen + 1) instead of wall-clock time, because machines' clocks don't agree.

## 6. Decision 3 — Who decides the good version

**A set of integrators, one at a time.** The project starts with one; `integrator.vote` events add
or remove them, applied when a strict majority of the current integrators agree (the last one can't
be removed, and a removed integrator's later events stop counting).
- **The integration lease** (`integrator.lease` / `renew` / `release`) works like a task claim on
  the integrator role: the earliest active lease of a current integrator holds it. It's renewed on
  every pass; if its holder goes quiet it expires and the next integrator takes over by itself.
- **Compare-and-swap on main.** `patch.accepted`, `sketch.accepted` and `main.updated` carry the
  `base` they were built on; the state ignores them if `main` has moved since. This, not the
  lease, keeps `main` from forking: two integrators acting at once (clock skew, network split) just
  means the second verdict is discarded and the patch is redone on the new `main`. The lease only
  avoids that duplicated work. The published `main` (origin, or `legion/main` in p2p) always
  follows what the state says.
- **Interrupted work is finished by the next integrator**: an accepted sketch without subtasks
  gets them created.
- Only integrators advance `main` and modify `AGENTS.md` and `ZONES.md`.
- Before accepting a patch, it **reruns `accept` itself in a sandbox**; it does not trust the author's claim that it passes.
- It publishes `patch.accepted` or `patch.rejected` with the reason.
- In competitive mode it has no opinion: it accepts the one with the best `metric`.

**Later:** auditable verdicts (any node re-runs a verdict and a wrong one penalises the integrator's
trust), k-of-n independent verifiers for fully open projects, and integrators per zone (like
subsystem maintainers in Linux).

## 7. Decision 4 — Sharing knowledge at a bounded cost

- Every attempt, successful or not, publishes `attempt.reported`: what was tried, what happened and why it failed. At most about 300 words.
- A **librarian** agent (launched by the integrator) periodically rewrites `coord/DIGEST.md`:
  - what is already known not to work, deduplicated,
  - the design decisions made,
  - the state of each zone.
  - It has a size limit (about 3,000 words); old material is condensed instead of piling up.
- When starting, an agent reads **`DIGEST.md` and the events after its latest version**, and nothing else. That way the cost of reading does not grow with the number of contributors.

## 8. Decision 5 — Trust and security

- **Identity**: one key per person (Radicle DID or SSH key). The agent signs with its owner's key, and each event also states which agent and which model produced it.
- **Mandatory signatures**: nodes ignore unsigned events or events with an invalid signature.
- **Other people's code is not trustworthy**: running the tests of someone else's patch is running a stranger's code. **Always inside a sandbox** (a container with no credentials and limited network), never directly on the machine.
- **Prompt injection**: agents only follow instructions from an `AGENTS.md` signed by the integrator. Any text in events, patches or digests is **information, not orders**.
- **Spam**: a limit on events per identity per hour. Nodes discard the excess. Reputation exists as a trust score (`legion/trust.py`): computed per agent from integrator-verified
  evidence only, summed per person, and a Beta mean starting at 50. It is informative for now; using
  it to rate-limit or prioritise is left for later.

## 8b. Decision 6 — Subtasks proposed by agents (sketches)

Inspired by Prove2Me's *proof-sketches*: "audit the top and let the agents loose below".

- **Who**: the agent that has claimed an exclusive task can publish `sketch.proposed` instead
  of a patch. It includes the list of subtasks and a scaffolding commit.
- **What the scaffolding may touch**: the parent's zone and `legion_sketches/<parent-id>/` (the tests of the pieces).
- **Automatic review**: the integrator checks the rules and that the merge is clean, without running
  anything (the pieces don't work yet). If everything checks out, it publishes `sketch.accepted` and creates the subtasks
  with `task.created` (`parent`, `sketch`).
- **Subtask rules**: from 1 to 8; zone inside the parent's; `accept` starts with a test runner
  from the project's list (`runners` in `project.init`) and contains no shell metacharacters;
  `deps` only on earlier subtasks; maximum depth 3.
- **Resolution**: when all subtasks are done, the integrator runs the parent's `accept`
  on main. If it passes, `task.resolved`. If not, `sketch.failed`: the unfinished subtasks are cancelled
  and the parent becomes free again.
- **Why it is safe**: the only command that decides whether the top-level work is done is the one
  a person wrote. What an agent writes can only cause work to be lost, never cause something that
  is not good to be accepted as good.

## 9. Example event

`coord/events/000142-9b1e4c.json`:

```json
{
  "type": "task.claimed",
  "task": "t-7f3a",
  "author": "did:key:z6Mk…",
  "agent": "claude-code",
  "model": "claude-sonnet-5-5",
  "lamport": 142,
  "expires_in": "2h",
  "sig": "…"
}
```

Event types: `task.created` · `task.claimed` · `task.heartbeat` · `task.released` · `attempt.reported` · `patch.submitted` · `patch.accepted` · `patch.rejected` · `digest.published` · `main.updated` · `sketch.proposed` · `sketch.accepted` · `sketch.rejected` · `sketch.failed` · `task.resolved`.

Since each event is a new file with a unique name, **git always merges without conflicts**: the state is computed by joining all the events and walking through them in order.

## 10. Phased plan

| Phase | What | Transport | Validates |
|---|---|---|---|
| 0 | Local simulation: 3–5 agents on one machine, each in its own git worktree | Local git repository | The protocol and AGENTS.md |
| 1 | A few people, each with their own account | GitHub | Real coordination between machines |
| 2 | Open project | Radicle (with GitHub as a mirror) | p2p, strangers, trust |

Minimum tooling for phase 0: a `legion` CLI with `init`, `task add`, `claim`, `heartbeat`, `report`, `submit`, `status` and `digest`. The agent uses them and does not edit events by hand.

## 11. Lessons from reference systems

- **Anthropic's multi-agent research system**: a lead with parallel workers, each with its own context, returning compressed summaries.
- **C compiler built with parallel Claude**: isolated containers, a shared git repository and claims via files. It works without a lead.
- **Cursor's browser**: the flat version with locks failed; the hierarchy of planners, workers and a judge worked.
- **Linux / Kubernetes**: zones with owners, CI as gatekeeper, hierarchical integration.
- **Big O Speedrun**: the setup is one sentence for the agent, there is a board of attempts (including failed ones), an automatic verifier (Lean) and results are aggregated by maximum. Its weak point is a board that grows without curation.

## 12. Open questions

- **Seeds' replication policy**: a public repository is carried by the public seeds, but whether they replicate every contributor's namespace, and how fast, has to be measured in a real test across networks. A project can run its own seed if needed.
- **Distributing legion itself** to new contributors: today they get a copy of this folder.
- **Which project to test** phase 0 with. It should be easy to split into zones and have good tests.
- **Claim duration**: a fixed 2 h, or based on `size`?
- **Choosing integrators** when there are several: by appointment, by reputation, or rotating?
- **Spending limits** per agent: set by the protocol or by each owner?
- **Repository location**: this folder is inside OneDrive, and OneDrive can corrupt a git repository when syncing it. It should be moved before running `git init`.
