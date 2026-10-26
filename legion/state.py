"""Computes the project state by walking the events in Lamport order.

Every node holding the same events reaches the same state, without talking to anyone.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import events as E

MIN_TTL, MAX_TTL = 60, 24 * 3600
MIN_LEASE = 10  # the integrator's lease can be short: it renews it on every pass
LEASE = "__integrator__"
PATCH_PREFIXES = ("legion/", "refs/legion/patches/")  # never main or anyone else's branch

# Sketches: an agent splits a task into subtasks. Only the top task was audited by a human; an
# agent writes the subtasks, so they are limited: they can't leave the parent's zone, their tests
# live in SKETCH_DIR/<parent-id>/, and the parent only counts as done if it passes its own test.
SKETCH_DIR = "legion_sketches"
MAX_CHILDREN, MAX_DEPTH = 8, 3
# Paths: when a task stops advancing, anyone may open new routes towards it (subtasks of any kind). A task is
# stuck after STALL_AFTER failures (failed reports, rejected patches, failed sketches) since its last progress,
# and holds at most MAX_OPEN_PATHS paths that aren't finished, so being stuck doesn't turn into a flood.
STALL_AFTER, MAX_OPEN_PATHS = 3, 3
# When nothing is open, idle agents don't wait: they join a task someone already holds (at most MAX_SHARED
# agents on one task; the first patch accepted wins), or prepare a blocked one (they split it into subtasks
# that don't need its dependencies, or write the patch, which is reviewed once the dependencies are in).
MAX_SHARED = 3
# Agents working on the same task talk through notes on it ("I'm trying X, don't repeat it"): only someone
# working on the task may post one, and at most MAX_TASK_NOTES each, so they can't flood it.
MAX_TASK_NOTES = 30
# Once the exclusive tasks are done, claim hands out competitive ones: each agent keeps trying one until it has
# made COMPETITIVE_TRIES attempts on it since its best last improved, and is done when that holds for all.
COMPETITIVE_TRIES = 3
# Verification by other nodes. A node checks a patch the way the integrator would and signs the result
# (patch.verified): on which tree it merged it (main plus the patches ahead of it in the queue, which every node
# works out the same way) and which tree came out. Merges are deterministic, so when the integrator gets there
# the trees match and a quorum of passes from trusted people lets it accept without running the check itself;
# it still checks a share of them (spot_check), and a verification that turns out false costs trust.
POLICY = {"quorum": 2, "verifier_trust": 65, "spot_check": 0.25}
TREE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
SIZES = {"L": 0, "M": 1, "S": 2}
DEFAULT_RUNNERS = ["python -m unittest", "python -m pytest", "pytest", "npm test", "cargo test", "go test"]
SHELL_META = set(";&|`$<>\n\r")
CHILD_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


@dataclass
class Task:
    id: str
    key: str
    title: str
    zone: list
    accept: str
    mode: str = "exclusive"
    metric: str | None = None
    goal: str = "min"
    deps: list = field(default_factory=list)
    size: str = "M"
    body: str = ""
    lamport: int = 0
    parent: str | None = None
    sketch: str | None = None
    depth: int = 0
    cancelled: bool = False


@dataclass
class Sketch:
    id: str
    task: str
    author: str
    name: str
    children: list
    commit: str
    branch: str
    lamport: int
    notes: str = ""
    verdict: str | None = None  # None (pending), accepted or rejected
    reason: str = ""
    code: str | None = None
    merge_commit: str | None = None
    child_ids: list = field(default_factory=list)
    outcome: str | None = None  # once accepted: resolved (the parent passed its test) or failed
    kind: str = "sketch"  # or "path": a new route towards a stuck task, which doesn't replace it


@dataclass
class Claim:
    task: str
    author: str
    name: str
    lamport: int
    ts: float
    ttl: int
    last_beat: float
    released: bool = False
    join: bool = False  # joins whoever holds the task, instead of competing for it

    def active(self, now: float) -> bool:
        return not self.released and now <= self.last_beat + self.ttl


@dataclass
class Patch:
    id: str
    task: str
    author: str
    name: str
    commit: str
    branch: str
    lamport: int
    verdict: str | None = None
    reason: str = ""
    code: str | None = None  # why it was rejected: tests, zone, conflict... (see integrator.py)
    metric: float | None = None
    merge_commit: str | None = None
    ts: float = 0.0
    base_tree: str | None = None  # the trees the verdict was reached on: they tell a true verification from a
    tree: str | None = None       # false one


def task_id(ev: dict) -> str:
    return "t-" + ev["id"][:8]


def in_zone(path: str, zone: list) -> bool:
    return any(path == z.rstrip("/") or path.startswith(z.rstrip("/") + "/") for z in zone)


def sketch_dir(tid: str) -> str:
    return f"{SKETCH_DIR}/{tid}"


def valid_branch(branch) -> bool:
    return isinstance(branch, str) and branch.startswith(PATCH_PREFIXES) and ".." not in branch


def check_accept(cmd, runners: list) -> str | None:
    """Subtask commands are written by an agent: only known test runners are allowed."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "the acceptance command is missing"
    if any(ch in SHELL_META for ch in cmd):
        return f"the acceptance command can't contain ; & | ` $ < > or line breaks: {cmd}"
    if not any(cmd == r or cmd.startswith(r + " ") for r in runners):
        return f"the acceptance command must start with one of: {', '.join(runners)}"
    return None



def lease_key(shard: str | None) -> str:
    return LEASE if shard is None else f"{LEASE}:{shard}"

class State:
    def __init__(self):
        self.project: dict | None = None
        self.integrators: set = set()
        self.tasks: dict[str, Task] = {}
        self.claims: dict[str, list[Claim]] = defaultdict(list)
        self.patches: dict[str, Patch] = {}
        self.sketches: dict[str, Sketch] = {}
        self.resolved: set = set()  # tasks done because their subtasks were completed
        self.attempts: list[dict] = []
        self.notes: dict[str, list[dict]] = defaultdict(list)  # task -> its notes, in order
        self.digest: dict | None = None
        self.description: str = ""  # what the project is about, set by an integrator
        self.main_tip: str | None = None  # the main the integrator signed last
        self.events: list[dict] = []
        self.ignored: list[tuple[dict, str]] = []
        self.max_lamport = 0
        self.votes: dict[tuple[str, str], set] = {}  # (add|remove, key) -> integrators who voted for it
        self.leases: list[Claim] = []                # claims on the integrator role, whole or per subtree
        self.verifications: dict[str, list[dict]] = defaultdict(list)  # patch -> patch.verified events
        self.policy: dict = dict(POLICY)
        self.integrator_names: dict[str, str] = {}

    # --- queries -----------------------------------------------------------

    @property
    def runners(self) -> list:
        return (self.project or {}).get("runners") or DEFAULT_RUNNERS

    def find_task(self, ref: str) -> Task | None:
        if ref in self.tasks:
            return self.tasks[ref]
        return next((t for t in self.tasks.values() if t.key == ref), None)

    def holder(self, tid: str, now: float) -> Claim | None:
        active = [c for c in self.claims[tid] if c.active(now)]
        return min(active, key=lambda c: (c.lamport, c.author)) if active else None

    def claimants(self, tid: str, now: float) -> list[Claim]:
        """Who works on the task: its holder, and up to MAX_SHARED - 1 agents who joined it."""
        h = self.holder(tid, now)
        if not h:
            return []
        out, seen = [h], {h.author}
        for c in sorted((c for c in self.claims[tid] if c.join and c.active(now)), key=lambda c: (c.lamport, c.author)):
            if len(out) == MAX_SHARED:
                break
            if c.author not in seen:
                out.append(c)
                seen.add(c.author)
        return out

    def works_on(self, tid: str, author: str, now: float) -> bool:
        return any(c.author == author for c in self.claimants(tid, now))

    def patches_for(self, tid: str, verdict: str | None = "any") -> list[Patch]:
        ps = [p for p in self.patches.values() if p.task == tid]
        if verdict != "any":
            ps = [p for p in ps if p.verdict == verdict]
        return sorted(ps, key=lambda p: (p.lamport, p.id))

    def sketches_for(self, tid: str, kind: str = "sketch") -> list[Sketch]:
        return sorted((k for k in self.sketches.values() if k.task == tid and k.kind == kind),
                      key=lambda k: (k.lamport, k.id))

    def paths_for(self, tid: str) -> list[Sketch]:
        return self.sketches_for(tid, "path")

    def open_paths(self, tid: str) -> list[Sketch]:
        """Paths being reviewed, or accepted with subtasks still to do."""
        return [k for k in self.paths_for(tid) if k.verdict is None or k.verdict == "accepted" and not
                all(self.done(c) or self.tasks[c].cancelled for c in k.child_ids)]

    def last_progress(self, tid: str) -> int:
        task = self.tasks[tid]
        return max([task.lamport] + [p.lamport for p in self.patches_for(tid, "accepted")]
                   + [k.lamport for k in self.sketches.values() if k.task == tid and k.verdict == "accepted"])

    def attempts_by(self, tid: str, author: str) -> int:
        """How many times this agent tried the task since it last advanced: reports, patches, released claims."""
        since = self.last_progress(tid)
        return (sum(1 for a in self.attempts if a.get("task") == tid and a["author"] == author and a["lamport"] > since)
                + sum(1 for p in self.patches_for(tid) if p.author == author and p.lamport > since)
                + sum(1 for c in self.claims[tid] if c.author == author and c.released and c.lamport > since))

    def competitive_for(self, author: str) -> list[Task]:
        """Competitive tasks this agent hasn't exhausted, the ones with no result yet first."""
        ts = [t for t in self.tasks.values() if t.mode == "competitive" and not t.cancelled
              and self.deps_ok(t) and self.attempts_by(t.id, author) < COMPETITIVE_TRIES
              and not any(p.author == author and p.verdict is None for p in self.patches_for(t.id))]
        return sorted(ts, key=lambda t: (self.best(t.id) is not None, self.attempts_by(t.id, author), t.lamport))

    def failures_since_progress(self, tid: str) -> int:
        """How stuck a task is: failures since it last advanced (created, improved or accepted, a sketch or a
        path accepted). Only from events, so every node counts the same."""
        task = self.tasks[tid]
        progress = max([task.lamport] + [p.lamport for p in self.patches_for(tid, "accepted")]
                       + [k.lamport for k in self.sketches.values() if k.task == tid and k.verdict == "accepted"])
        fails = [a for a in self.attempts if a.get("task") == tid and a.get("outcome") == "failure"]
        return (sum(1 for a in fails if a["lamport"] > progress)
                + sum(1 for p in self.patches_for(tid, "rejected") if p.lamport > progress)
                + sum(1 for k in self.sketches_for(tid) if k.outcome == "failed" and k.lamport >= progress))

    def stalled(self, tid: str) -> bool:
        task = self.tasks[tid]
        if task.cancelled or task.mode == "exclusive" and self.done(tid):
            return False
        return self.failures_since_progress(tid) >= STALL_AFTER

    def active_sketch(self, tid: str) -> Sketch | None:
        """The accepted sketch that isn't resolved yet: its subtasks are under way."""
        return next((k for k in self.sketches_for(tid) if k.verdict == "accepted" and not k.outcome), None)

    def children(self, tid: str) -> list[Task]:
        return [t for t in self.tasks.values() if t.parent == tid]

    def ready_to_resolve(self) -> list[Sketch]:
        return [k for k in self.sketches.values()
                if k.kind == "sketch" and k.verdict == "accepted" and not k.outcome and k.child_ids
                and all(self.done(c) for c in k.child_ids) and self.deps_ok(self.tasks[k.task])]

    def best(self, tid: str) -> float | None:
        task = self.tasks[tid]
        values = [p.metric for p in self.patches_for(tid, "accepted") if p.metric is not None]
        if not values:
            return None
        return min(values) if task.goal == "min" else max(values)

    def done(self, tid: str) -> bool:
        return tid in self.resolved or bool(self.patches_for(tid, "accepted"))

    def unblocks(self, tid: str) -> int:
        """How many unfinished tasks wait, directly or not, for this one: those that depend on it, and the parent
        whose sketch it belongs to, and so on up and along. The integrator reviews these first."""
        waiting = defaultdict(set)
        for t in self.tasks.values():
            for d in t.deps:
                waiting[d].add(t.id)
            k = self.sketches.get(t.sketch) if t.sketch else None
            if t.parent and k and k.kind == "sketch":  # the parent is done once its sketch's subtasks are
                waiting[t.id].add(t.parent)
        seen, todo = set(), [tid]
        while todo:
            for nxt in waiting[todo.pop()]:
                if nxt not in seen and not self.done(nxt) and not self.tasks[nxt].cancelled:
                    seen.add(nxt)
                    todo.append(nxt)
        return len(seen)

    def deps_ok(self, task: Task) -> bool:
        return all(self.done(d) for d in task.deps)

    def status(self, tid: str, now: float) -> str:
        task = self.tasks[tid]
        if task.mode == "exclusive" and self.done(tid):
            return "accepted"
        if task.cancelled:
            return "cancelled"
        if any(k.verdict is None for k in self.sketches_for(tid)):
            return "review"
        if self.active_sketch(tid):
            return "sketched"
        if not self.deps_ok(task):  # a patch written ahead waits here until its dependencies are in
            return "blocked"
        if self.patches_for(tid, None):
            return "review"
        if task.mode == "exclusive" and self.holder(tid, now):
            return "claimed"
        return "open"

    def claimable(self, now: float) -> list[Task]:
        return [t for t in self.tasks.values() if t.mode == "exclusive" and self.status(t.id, now) == "open"]

    def held_by(self, author: str, now: float) -> list[Task]:
        return [t for t in self.tasks.values()
                if t.mode == "exclusive" and (s := self.status(t.id, now)) in ("claimed", "review", "blocked")
                and self.works_on(t.id, author, now)
                and not any(p.author == author for p in self.patches_for(t.id, None))]

    def joinable(self, author: str, now: float) -> list[Task]:
        """Tasks held by others with room for one more, the hardest first: stuck, then the most failures, then
        the biggest, then the one held longest."""
        # A blocked task someone is preparing can be joined too: they may be splitting it, or writing it ahead.
        ts = [t for t in self.tasks.values() if t.mode == "exclusive"
              and self.status(t.id, now) in ("claimed", "blocked") and self.holder(t.id, now)
              and len(cl := self.claimants(t.id, now)) < MAX_SHARED and all(c.author != author for c in cl)]
        return sorted(ts, key=lambda t: (not self.stalled(t.id), -self.failures_since_progress(t.id),
                                         SIZES.get(t.size, 1), self.holder(t.id, now).ts, t.id))

    def preparable(self, now: float) -> list[Task]:
        """Blocked tasks nobody is preparing, the closest to unblocking first."""
        ts = [t for t in self.tasks.values() if t.mode == "exclusive" and self.status(t.id, now) == "blocked"
              and not self.holder(t.id, now) and not self.patches_for(t.id, None)]
        return sorted(ts, key=lambda t: (sum(not self.done(d) for d in t.deps), t.lamport, t.id))

    def exclusive_done(self) -> bool:
        ex = [t for t in self.tasks.values() if t.mode == "exclusive" and not t.cancelled]
        return bool(ex) and all(self.done(t.id) for t in ex)

    def check_children(self, parent: Task, children, path: bool = False) -> str | None:
        """The rules that make it safe to let an agent split a task without auditing the subtasks."""
        what = "path" if path else "sketch"
        if parent.mode != "exclusive" and not path:
            return "only exclusive tasks can be split"
        if parent.depth + 1 > MAX_DEPTH:
            return f"can't split more than {MAX_DEPTH} levels deep"
        if not isinstance(children, list) or not 1 <= len(children) <= MAX_CHILDREN:
            return f"a {what} has between 1 and {MAX_CHILDREN} subtasks"
        seen = []
        for ch in children:
            if not isinstance(ch, dict):
                return "each subtask must be an object"
            key = ch.get("key")
            if not isinstance(key, str) or not CHILD_KEY.match(key):
                return f"invalid subtask key: {key!r} (lowercase letters, digits and hyphens)"
            if key in seen:
                return f"duplicate key: {key}"
            if self.find_task(f"{parent.key}/{key}"):
                return f"task {parent.key}/{key} already exists"
            if not ch.get("title"):
                return f"subtask {key} has no title"
            zone = ch.get("zone")
            if not isinstance(zone, list) or not zone or not all(isinstance(z, str) and z for z in zone):
                return f"subtask {key} needs a zone"
            outside = [z for z in zone if ".." in z or not in_zone(z.rstrip("/"), parent.zone)]
            if outside:
                return f"the zone of {key} goes outside the parent's ({', '.join(parent.zone)}): {', '.join(outside)}"
            err = check_accept(ch.get("accept"), self.runners)
            if err:
                return f"subtask {key}: {err}"
            deps = ch.get("deps") or []
            if not isinstance(deps, list) or any(d not in seen for d in deps):
                return f"the dependencies of {key} must be earlier subtasks of the same {what}"
            seen.append(key)
        return None

    # --- applying events -------------------------------------------------

    def _my_claim(self, ev: dict) -> Claim | None:
        mine = [c for c in self.claims[ev.get("task")] if c.author == ev["author"] and not c.released]
        return mine[-1] if mine else None

    def apply(self, ev: dict) -> str | None:
        t = ev["type"]
        if t == "project.init":
            if self.project:
                return "duplicate project.init"
            self.project = ev
            self.integrators = set(ev.get("integrators") or [ev["author"]])
            self.integrator_names[ev["author"]] = ev["name"]
            if isinstance(ev.get("description"), str):
                self.description = ev["description"][:E.MAX_DESCRIPTION]
            return None
        if self.project is None:
            return "the project is not started"
        if t in E.INTEGRATOR_ONLY and ev["author"] not in self.integrators:
            return "only an integrator can publish this event"

        # Compare-and-swap on main: whatever moves main must say which main it was built on. If main has
        # moved since (another integrator got there first), the event doesn't count and the work is redone.
        # This, not the lease, is what keeps main from forking; the lease only avoids duplicated work.
        if t in ("patch.accepted", "sketch.accepted", "main.updated") and ev.get("base") is not None \
                and self.main_tip and ev["base"] != self.main_tip:
            return "stale base: main moved on since this was computed"

        if t.startswith("integrator."):
            return self._integrators(ev)

        if t == "task.created":
            return self._create_task(ev)

        if t == "agent.joined":  # only so the others (and the viewer) know this agent is here
            return None

        if t == "project.policy":
            policy = dict(self.policy)
            for key, kind, lo, hi in (("quorum", int, 0, 9), ("verifier_trust", int, 0, 100),
                                      ("spot_check", (int, float), 0, 1)):
                if key in ev:
                    v = ev[key]
                    if not isinstance(v, kind) or isinstance(v, bool) or not lo <= v <= hi:
                        return f"{key} must be between {lo} and {hi}"
                    policy[key] = v
            self.policy = policy
            return None

        if t == "project.described":
            if not isinstance(ev.get("description"), str):
                return "missing description"
            if len(ev["description"]) > E.MAX_DESCRIPTION:
                return f"the description is longer than {E.MAX_DESCRIPTION} characters"
            self.description = ev["description"]
            return None

        if t == "main.updated":
            if not isinstance(ev.get("commit"), str) or not ev["commit"]:
                return "missing commit"
            self.main_tip = ev["commit"]
            return None

        if t in ("sketch.accepted", "sketch.rejected", "sketch.failed", "path.accepted", "path.rejected"):
            return self._judge_sketch(ev)

        task = self.tasks.get(ev.get("task"))
        if task is None and t != "digest.published" and t not in ("patch.accepted", "patch.rejected"):
            return "unknown task"

        if t == "task.claimed":
            if task.mode != "exclusive":
                return "competitive tasks are not claimed"
            ttl = ev.get("ttl")
            if not isinstance(ttl, int) or not MIN_TTL <= ttl <= MAX_TTL:
                return "ttl out of range"
            self.claims[task.id].append(Claim(task.id, ev["author"], ev["name"], ev["lamport"], ev["ts"], ttl, ev["ts"],
                                              join=ev.get("join") is True))
        elif t == "task.heartbeat":
            c = self._my_claim(ev)
            if c is None:
                return "no claim to renew"
            if ev["ts"] > c.last_beat + c.ttl:
                return "the claim had already expired"
            c.last_beat = max(c.last_beat, ev["ts"])
        elif t == "task.released":
            mine = [c for c in self.claims[task.id] if c.author == ev["author"] and not c.released]
            if not mine:
                return "no claim to release"
            for c in mine:
                c.released = True
        elif t == "attempt.reported":
            if ev.get("outcome") not in ("success", "failure"):
                return "outcome must be success or failure"
            if not isinstance(ev.get("notes", ""), str) or len(ev.get("notes", "")) > E.MAX_NOTES:
                return f"notes must be text of at most {E.MAX_NOTES} characters"
            self.attempts.append(ev)
        elif t == "patch.verified":
            p = self.patches.get(ev.get("patch"))
            if p is None or p.task != task.id:
                return "unknown patch"
            if p.verdict:
                return "the patch already has a verdict"
            if ev["author"] == p.author:
                return "a patch can't be verified by its own author"
            if ev.get("outcome") not in ("pass", "fail"):
                return "outcome must be pass or fail"
            if not all(isinstance(ev.get(k), str) and TREE.match(ev[k]) for k in ("base_tree", "tree")):
                return "base_tree and tree must be git tree ids"
            if ev.get("metric") is not None and (not isinstance(ev["metric"], (int, float)) or isinstance(ev["metric"], bool)):
                return "metric must be a number"
            if any(v["author"] == ev["author"] and v["base_tree"] == ev["base_tree"] for v in self.verifications[p.id]):
                return "already verified on that tree"
            self.verifications[p.id].append(ev)
        elif t == "task.note":
            text = ev.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > E.MAX_NOTES:
                return f"a note is text of at most {E.MAX_NOTES} characters"
            if not self.works_on(task.id, ev["author"], ev["ts"]):
                return "only someone working on the task may post notes on it"
            if sum(n["author"] == ev["author"] for n in self.notes[task.id]) >= MAX_TASK_NOTES:
                return f"at most {MAX_TASK_NOTES} notes per agent on a task"
            self.notes[task.id].append(ev)
        elif t == "patch.submitted":
            if not ev.get("commit") or not ev.get("branch"):
                return "missing commit or branch"
            if not valid_branch(ev["branch"]):
                return "a patch branch must be under legion/ or refs/legion/patches/"
            self.patches[ev["id"]] = Patch(ev["id"], task.id, ev["author"], ev["name"], ev["commit"],
                                           ev["branch"], ev["lamport"], ts=ev["ts"])
        elif t in ("patch.accepted", "patch.rejected"):
            p = self.patches.get(ev.get("patch"))
            if p is None:
                return "unknown patch"
            if p.verdict:
                return "the patch already had a verdict"
            p.verdict = "accepted" if t == "patch.accepted" else "rejected"
            p.reason = ev.get("reason", "")
            p.code = ev.get("code")
            p.metric = ev.get("metric")
            p.merge_commit = ev.get("merge_commit")
            p.base_tree, p.tree = ev.get("base_tree"), ev.get("tree")
            if p.merge_commit:
                self.main_tip = p.merge_commit
        elif t == "sketch.proposed":
            if self.done(task.id) or task.cancelled or self.active_sketch(task.id) \
                    or any(k.verdict is None for k in self.sketches_for(task.id)):
                return "the task is already done or already has a sketch under way"
            if not ev.get("commit") or not valid_branch(ev.get("branch")):
                return "missing commit, or the branch is outside the patch namespace"
            if not isinstance(ev.get("notes", ""), str) or len(ev.get("notes", "")) > E.MAX_NOTES:
                return f"notes must be text of at most {E.MAX_NOTES} characters"
            err = self.check_children(task, ev.get("children"))
            if err:
                return err
            self.sketches[ev["id"]] = Sketch(ev["id"], task.id, ev["author"], ev["name"], ev["children"],
                                             ev["commit"], ev["branch"], ev["lamport"], ev.get("notes", ""))
        elif t == "path.proposed":
            # Anyone may propose one, holding the task or not, exclusive or competitive: but only once the task
            # is stuck, and only a few at a time.
            if task.cancelled or task.mode == "exclusive" and self.done(task.id):
                return "the task is already done"
            if not self.stalled(task.id):
                return (f"the task isn't stuck: paths open after {STALL_AFTER} failures since its last progress "
                        f"(it has {self.failures_since_progress(task.id)})")
            if len(self.open_paths(task.id)) >= MAX_OPEN_PATHS:
                return f"the task already has {MAX_OPEN_PATHS} paths under way"
            notes = ev.get("notes", "")
            if not isinstance(notes, str) or not notes.strip() or len(notes) > E.MAX_NOTES:
                return f"a path needs notes (why this route, what failed before), at most {E.MAX_NOTES} characters"
            if ev.get("commit") and not valid_branch(ev.get("branch")):
                return "the branch is outside the patch namespace"
            err = self.check_children(task, ev.get("children"), path=True)
            if err:
                return err
            self.sketches[ev["id"]] = Sketch(ev["id"], task.id, ev["author"], ev["name"], ev["children"],
                                             ev.get("commit") or "", ev.get("branch") or "", ev["lamport"], notes,
                                             kind="path")
        elif t == "task.resolved":
            k = self.sketches.get(ev.get("sketch"))
            if not k or k.task != task.id or k not in self.ready_to_resolve():
                return "the task can't be resolved: not every subtask of its sketch is done"
            k.outcome = "resolved"
            self.resolved.add(task.id)
        elif t == "digest.published":
            if not isinstance(ev.get("text"), str):
                return "missing text"
            self.digest = ev
        return None

    def _create_task(self, ev: dict) -> str | None:
        zone = ev.get("zone")
        if not ev.get("title") or not ev.get("accept") or not isinstance(zone, list) or not zone:
            return "a task needs title, accept and zone"
        if ev.get("mode", "exclusive") not in ("exclusive", "competitive"):
            return "mode must be exclusive or competitive"
        if ev.get("mode") == "competitive" and not ev.get("metric"):
            return "a competitive task needs a metric"
        deps = ev.get("deps") or []
        if any(d not in self.tasks for d in deps):
            return "unknown dependency"
        key = ev.get("key") or task_id(ev)
        if any(x.key == key for x in self.tasks.values()):
            return f"a task with key {key} already exists"
        depth, sketch = 0, None
        if ev.get("parent") or ev.get("sketch") or ev.get("path"):
            parent = self.tasks.get(ev.get("parent"))
            sketch = self.sketches.get(ev.get("sketch") or ev.get("path"))
            if not parent or not sketch or sketch.task != parent.id or sketch.verdict != "accepted":
                return "a subtask needs a parent and an accepted sketch or path of that parent"
            if not key.startswith(parent.key + "/") or not all(in_zone(z.rstrip("/"), parent.zone) for z in zone):
                return "a subtask must carry its parent's key and stay inside its zone"
            depth = parent.depth + 1
        tid = task_id(ev)
        self.tasks[tid] = Task(tid, key, ev["title"], zone, ev["accept"], ev.get("mode", "exclusive"),
                               ev.get("metric"), ev.get("goal", "min"), deps, ev.get("size", "M"),
                               ev.get("body", ""), ev["lamport"], ev.get("parent"), sketch.id if sketch else None,
                               depth)
        if sketch:
            sketch.child_ids.append(tid)
        return None

    def _integrators(self, ev: dict) -> str | None:
        """Who may integrate (decided by majority vote) and who integrates right now (a lease)."""
        t, author = ev["type"], ev["author"]
        if t == "integrator.vote":
            action, key = ev.get("action"), ev.get("key")
            if action not in ("add", "remove") or not isinstance(key, str) or not key.startswith("ed25519:"):
                return "a vote needs action add|remove and an ed25519 key"
            if (action == "add") == (key in self.integrators):
                return "nothing to change"
            if action == "remove" and len(self.integrators) == 1:
                return "can't remove the last integrator"
            voters = self.votes.setdefault((action, key), set())
            voters.add(author)
            if len(voters & self.integrators) * 2 > len(self.integrators):  # strict majority of current ones
                (self.integrators.add if action == "add" else self.integrators.discard)(key)
                if action == "add" and ev.get("member"):
                    self.integrator_names[key] = ev["member"]
                self.votes.pop((action, key), None)
            return None
        shard = ev.get("shard")
        if shard is not None and (shard not in self.tasks or self.tasks[shard].parent):
            return "a lease's shard is a top-level task"
        if t == "integrator.lease":
            ttl = ev.get("ttl")
            if not isinstance(ttl, int) or not MIN_LEASE <= ttl <= MAX_TTL:
                return "lease ttl out of range"
            self.leases.append(Claim(lease_key(shard), author, ev["name"], ev["lamport"], ev["ts"], ttl, ev["ts"]))
            return None
        mine = [c for c in self.leases if c.author == author and not c.released]
        # A renewal names its lease (a shard, or none: the project's); a release too, or with neither a shard nor
        # project_lease it ends them all.
        if shard is not None or t == "integrator.renew" or ev.get("project_lease") is True:
            mine = [c for c in mine if c.task == lease_key(shard)]
        if not mine:
            return "no lease to renew or release"
        if t == "integrator.renew":
            c = mine[-1]
            if ev["ts"] > c.last_beat + c.ttl:
                return "the lease had already expired"
            c.last_beat = max(c.last_beat, ev["ts"])
        else:
            for c in mine:
                c.released = True
        return None

    def leader(self, now: float, shard: str | None = None) -> Claim | None:
        """The integrator holding the lease (the whole project's, or a shard's) right now: the earliest active
        one of a current integrator."""
        active = [c for c in self.leases if c.task == lease_key(shard) and c.author in self.integrators
                  and c.active(now)]
        return min(active, key=lambda c: (c.lamport, c.author)) if active else None

    def root_of(self, tid: str) -> str:
        while self.tasks[tid].parent:
            tid = self.tasks[tid].parent
        return tid

    def integrates(self, author: str, tid: str, now: float) -> bool:
        """Whether this integrator handles the task now: it holds its subtree's lease, or the project's lease
        and nobody else holds the subtree's."""
        holder = self.leader(now, self.root_of(tid)) or self.leader(now)
        return holder.author == author if holder else author in self.integrators  # no lease: anyone may

    def pending_roots(self) -> dict[str, int]:
        """Top-level tasks with work waiting for an integrator, and how much."""
        work = defaultdict(int)
        for p in self.patches.values():
            if p.verdict is None and self.deps_ok(self.tasks[p.task]):
                work[self.root_of(p.task)] += 1
        for k in self.sketches.values():
            if k.verdict is None:
                work[self.root_of(k.task)] += 1
        for k in self.ready_to_resolve():
            work[self.root_of(k.task)] += 1
        return dict(work)

    def _judge_sketch(self, ev: dict) -> str | None:
        k = self.sketches.get(ev.get("sketch") or ev.get("path"))
        if k is None:
            return "unknown sketch or path"
        if (k.kind == "path") != ev["type"].startswith("path."):
            return "a sketch's verdict is sketch.*, a path's is path.*"
        if ev["type"] == "sketch.failed":
            if k.verdict != "accepted" or k.outcome:
                return "only an accepted, unresolved sketch can fail"
            k.outcome, k.reason = "failed", ev.get("reason", "")
            for c in k.child_ids:  # whatever wasn't finished no longer makes sense
                if not self.done(c):
                    self.tasks[c].cancelled = True
            return None
        if k.verdict:
            return "the sketch already had a verdict"
        k.verdict = "accepted" if ev["type"].endswith(".accepted") else "rejected"
        k.reason = ev.get("reason", "")
        k.code = ev.get("code")
        k.merge_commit = ev.get("merge_commit")
        if k.merge_commit:
            self.main_tip = k.merge_commit
        return None


def build(events: list[dict], trusted=None) -> State:
    """The state after these events, in Lamport order. `trusted`: events whose signature is known to be good
    (a set of their id(), or True for all of them), which aren't checked again."""
    st = State()
    for ev in sorted(events, key=lambda e: (e.get("lamport", 0) if isinstance(e.get("lamport"), int) else 0,
                                            str(e.get("id", "")))):
        known = trusted is True or (trusted is not None and id(ev) in trusted)
        err = (None if known else E.check(ev)) or st.apply(ev)
        if err:
            st.ignored.append((ev, err))
            continue
        st.events.append(ev)
        st.max_lamport = max(st.max_lamport, ev["lamport"])
    return st
