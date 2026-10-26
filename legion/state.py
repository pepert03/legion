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
        self.digest: dict | None = None
        self.description: str = ""  # what the project is about, set by an integrator
        self.main_tip: str | None = None  # the main the integrator signed last
        self.events: list[dict] = []
        self.ignored: list[tuple[dict, str]] = []
        self.max_lamport = 0
        self.votes: dict[tuple[str, str], set] = {}  # (add|remove, key) -> integrators who voted for it
        self.leases: list[Claim] = []                # claims on the integrator role
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

    def patches_for(self, tid: str, verdict: str | None = "any") -> list[Patch]:
        ps = [p for p in self.patches.values() if p.task == tid]
        if verdict != "any":
            ps = [p for p in ps if p.verdict == verdict]
        return sorted(ps, key=lambda p: (p.lamport, p.id))

    def sketches_for(self, tid: str) -> list[Sketch]:
        return sorted((k for k in self.sketches.values() if k.task == tid), key=lambda k: (k.lamport, k.id))

    def active_sketch(self, tid: str) -> Sketch | None:
        """The accepted sketch that isn't resolved yet: its subtasks are under way."""
        return next((k for k in self.sketches_for(tid) if k.verdict == "accepted" and not k.outcome), None)

    def children(self, tid: str) -> list[Task]:
        return [t for t in self.tasks.values() if t.parent == tid]

    def ready_to_resolve(self) -> list[Sketch]:
        return [k for k in self.sketches.values()
                if k.verdict == "accepted" and not k.outcome and k.child_ids
                and all(self.done(c) for c in k.child_ids)]

    def best(self, tid: str) -> float | None:
        task = self.tasks[tid]
        values = [p.metric for p in self.patches_for(tid, "accepted") if p.metric is not None]
        if not values:
            return None
        return min(values) if task.goal == "min" else max(values)

    def done(self, tid: str) -> bool:
        return tid in self.resolved or bool(self.patches_for(tid, "accepted"))

    def deps_ok(self, task: Task) -> bool:
        return all(self.done(d) for d in task.deps)

    def status(self, tid: str, now: float) -> str:
        task = self.tasks[tid]
        if task.mode == "exclusive" and self.done(tid):
            return "accepted"
        if task.cancelled:
            return "cancelled"
        if self.patches_for(tid, None) or any(k.verdict is None for k in self.sketches_for(tid)):
            return "review"
        if self.active_sketch(tid):
            return "sketched"
        if not self.deps_ok(task):
            return "blocked"
        if task.mode == "exclusive" and self.holder(tid, now):
            return "claimed"
        return "open"

    def claimable(self, now: float) -> list[Task]:
        return [t for t in self.tasks.values() if t.mode == "exclusive" and self.status(t.id, now) == "open"]

    def held_by(self, author: str, now: float) -> list[Task]:
        return [t for t in self.tasks.values()
                if t.mode == "exclusive" and self.status(t.id, now) in ("claimed", "review")
                and (h := self.holder(t.id, now)) and h.author == author]

    def exclusive_done(self) -> bool:
        ex = [t for t in self.tasks.values() if t.mode == "exclusive" and not t.cancelled]
        return bool(ex) and all(self.done(t.id) for t in ex)

    def check_children(self, parent: Task, children) -> str | None:
        """The rules that make it safe to let an agent split a task without auditing the subtasks."""
        if parent.mode != "exclusive":
            return "only exclusive tasks can be split"
        if parent.depth + 1 > MAX_DEPTH:
            return f"can't split more than {MAX_DEPTH} levels deep"
        if not isinstance(children, list) or not 1 <= len(children) <= MAX_CHILDREN:
            return f"a sketch has between 1 and {MAX_CHILDREN} subtasks"
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
                return f"the dependencies of {key} must be earlier subtasks of the same sketch"
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

        if t in ("sketch.accepted", "sketch.rejected", "sketch.failed"):
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
            self.claims[task.id].append(Claim(task.id, ev["author"], ev["name"], ev["lamport"], ev["ts"], ttl, ev["ts"]))
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
        elif t == "patch.submitted":
            if not ev.get("commit") or not ev.get("branch"):
                return "missing commit or branch"
            if not valid_branch(ev["branch"]):
                return "a patch branch must be under legion/ or refs/legion/patches/"
            self.patches[ev["id"]] = Patch(ev["id"], task.id, ev["author"], ev["name"], ev["commit"],
                                           ev["branch"], ev["lamport"])
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
        if ev.get("parent") or ev.get("sketch"):
            parent = self.tasks.get(ev.get("parent"))
            sketch = self.sketches.get(ev.get("sketch"))
            if not parent or not sketch or sketch.task != parent.id or sketch.verdict != "accepted":
                return "a subtask needs a parent and an accepted sketch of that parent"
            if not key.startswith(parent.key + "/") or not all(in_zone(z.rstrip("/"), parent.zone) for z in zone):
                return "a subtask must carry its parent's key and stay inside its zone"
            depth = parent.depth + 1
        tid = task_id(ev)
        self.tasks[tid] = Task(tid, key, ev["title"], zone, ev["accept"], ev.get("mode", "exclusive"),
                               ev.get("metric"), ev.get("goal", "min"), deps, ev.get("size", "M"),
                               ev.get("body", ""), ev["lamport"], ev.get("parent"), ev.get("sketch"), depth)
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
        if t == "integrator.lease":
            ttl = ev.get("ttl")
            if not isinstance(ttl, int) or not MIN_LEASE <= ttl <= MAX_TTL:
                return "lease ttl out of range"
            self.leases.append(Claim(LEASE, author, ev["name"], ev["lamport"], ev["ts"], ttl, ev["ts"]))
            return None
        mine = [c for c in self.leases if c.author == author and not c.released]
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

    def leader(self, now: float) -> Claim | None:
        """The integrator holding the lease right now: the earliest active lease of a current integrator."""
        active = [c for c in self.leases if c.author in self.integrators and c.active(now)]
        return min(active, key=lambda c: (c.lamport, c.author)) if active else None

    def _judge_sketch(self, ev: dict) -> str | None:
        k = self.sketches.get(ev.get("sketch"))
        if k is None:
            return "unknown sketch"
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
        k.verdict = "accepted" if ev["type"] == "sketch.accepted" else "rejected"
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
