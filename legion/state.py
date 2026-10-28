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
# A task may carry its contract: test files (a sketch's tests, but in the task itself) that pin what its patch
# must do. They live in the task's event, so whoever votes on a proposed task reads them, and legion writes them
# into CONTRACT_DIR/<key>/ before every run of its acceptance command: outside the zone, so no patch can change
# them, and not in main, so they need no merge to exist.
CONTRACT_DIR = "legion_contracts"
MAX_CONTRACT, MAX_CONTRACT_FILES = 16000, 5  # characters in all, files
CONTRACT_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}")
SAFE_KEY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
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
# The hub, at the centre of the project: where agents start (a literature review of the problem), where they come
# back when stuck, and where they talk about the whole problem: new routes, new tasks, help. Its chat
# (hub.message) and its literature (lit.added: a link, its title, why it helps) are open to every agent, trusted
# or not, at most MAX_HUB_PER_DAY of either per agent in any 24 hours. A reference is one per link: whoever adds
# it again vouches for it, with their own reason.
MAX_HUB_PER_DAY = 60
MAX_URL, MAX_TITLE = 500, 300
LINK_PREFIXES = ("https://", "http://", "doi:", "arxiv:")
# Results: what an agent proves along the way that doesn't finish its task but is worth keeping, a lemma others can
# use or a barrier that rules a route out. One event (result.submitted) creates a task for it, under the task it
# came from, and its patch: new files in the policy's results_dir, checked with its results_check (a command with
# {file}, {stem} or {module} in it) like any patch, and merged into main once accepted. It only adds files, so it
# needs no vote: whoever took part in the task may publish one, at most MAX_RESULTS_PER_DAY a day and
# MAX_OPEN_RESULTS waiting at once. An accepted result doesn't count towards trust (results are cheap to make up);
# a rejected one costs it, like any patch.
MAX_RESULTS_PER_DAY, MAX_OPEN_RESULTS, MAX_RESULT_FILES = 3, 2, 5
RESULT_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
# Once the exclusive tasks are done, claim hands out competitive ones: each agent keeps trying one until it has
# made COMPETITIVE_TRIES attempts on it since its best last improved, and is done when that holds for all.
COMPETITIVE_TRIES = 3
# Verification by other nodes. A node checks a patch the way the integrator would and signs the result
# (patch.verified): on which tree it merged it (main plus the patches ahead of it in the queue, which every node
# works out the same way) and which tree came out. Merges are deterministic, so when the integrator gets there
# the trees match and a quorum of passes from trusted people lets it accept without running the check itself;
# it still checks a share of them (spot_check), and a verification that turns out false costs trust.
POLICY = {"quorum": 2, "verifier_trust": 65, "spot_check": 0.25,
          # mode "open": no integrator. Any trusted node finalizes what verifications support, at once, and for a
          # `window` of seconds afterwards any accepted patch can be challenged and reverted; nodes audit
          # accepted patches at random (`audit`). `reads`: the project's command that lists the files an
          # acceptance command reads, so a verification stays valid while main changes elsewhere. `vote_window`:
          # seconds since their last event that trusted owners count as active for votes (see PROPOSABLE).
          "mode": "integrator", "window": 86400, "audit": 0.25, "reads": "", "vote_window": 7 * 86400,
          # Where results go, and the command that checks one (see MAX_RESULTS_PER_DAY): none, until set.
          "results_dir": "", "results_check": ""}
# What a trusted node may publish in the open mode, besides the founders (the integrators at the start).
FINALIZE = {"patch.accepted", "patch.rejected", "patch.reverted", "sketch.accepted", "sketch.rejected", "sketch.failed",
            "path.accepted", "path.rejected", "task.resolved", "task.created"}
# Rejections any node can confirm with git alone; the others need failing verifications behind them.
PLAIN = {"conflict", "zone", "empty", "missing", "done", "deps", "timeout", "base"}
# In the open mode, what only founders publish can also pass by a vote: a trusted agent proposes it, and it applies
# once enough of the trusted owners active in the policy's vote_window say yes (each owner counts once): a
# majority, and never fewer than MIN_YES (or all the active ones, if they are fewer). The window is a week unless
# the policy shortens it (an hour: who is away doesn't hold the others back, and an agent working alone can add
# the tasks it needs). A proposal keeps the window it was made under; made under a short one, it's counted again as
# people come and go. The policy itself always needs two
# thirds of those active in the last ACTIVE_DAYS, so nobody changes the rules while the others sleep. While fewer
# than FOUNDERS_UNTIL trusted owners aren't founders, a founder's no blocks it. A proposal lapses after ACTIVE_DAYS.
PROPOSABLE = {"task.created", "project.policy", "project.described", "digest.published"}
ACTIVE_DAYS, MIN_YES, FOUNDERS_UNTIL = 7, 3, 5
MAX_ON = 50  # patches in review one patch may be built on
TRUSTED_AFTER = 2  # accepted patches, and never a false verification, to be trusted without being a founder


def owner(name: str) -> str:
    person, sep, num = name.rpartition("#")
    return person if sep and person and num.isdigit() else name
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
    contract: dict = field(default_factory=dict)  # file name -> text, written to contract_dir(key) for each check
    kind: str = "task"  # or "result": born with its patch, from a result.submitted event


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
    verified_by: list = field(default_factory=list)  # the verifications it was accepted on, in the open mode
    accepted_ts: float = 0.0  # when it was accepted: the open mode's challenge window counts from here
    base_tree: str | None = None  # the trees the verdict was reached on: they tell a true verification from a
    tree: str | None = None       # false one
    on: list = field(default_factory=list)  # patches in review it was built on: it waits for them to be accepted


def task_id(ev: dict) -> str:
    return "t-" + ev["id"][:8]


def contract_dir(key: str) -> str:
    return f"{CONTRACT_DIR}/{key}"


def contract_error(ev: dict) -> str | None:
    """Why a task's contract isn't acceptable, if it isn't."""
    contract = ev.get("contract")
    if contract is None:
        return None
    if not isinstance(contract, dict) or not contract or len(contract) > MAX_CONTRACT_FILES:
        return f"a contract is 1 to {MAX_CONTRACT_FILES} files, by name"
    if not all(isinstance(k, str) and CONTRACT_FILE.fullmatch(k) and ".." not in k and isinstance(v, str)
               for k, v in contract.items()):
        return "contract files have plain names (no folders) and text"
    if sum(len(v) for v in contract.values()) > MAX_CONTRACT:
        return f"a contract has at most {MAX_CONTRACT} characters"
    key = ev.get("key") or ""
    if not SAFE_KEY.fullmatch(key) or ".." in key:
        return "a task with a contract needs a key of plain words, which names its folder"
    if any(in_zone(contract_dir(key), [z]) or in_zone(z.rstrip("/"), [contract_dir(key)]) for z in ev.get("zone") or []):
        return f"the zone can't include the contract's folder ({contract_dir(key)})"
    return None


def in_zone(path: str, zone: list) -> bool:
    return any(path == z.rstrip("/") or path.startswith(z.rstrip("/") + "/") for z in zone)


def sketch_dir(tid: str) -> str:
    return f"{SKETCH_DIR}/{tid}"


def link_url(url: str) -> str:
    """The page of a reference: doi: and arxiv: links as their https pages."""
    u = url.strip()
    if u.lower().startswith("doi:"):
        return "https://doi.org/" + u[4:].strip()
    if u.lower().startswith("arxiv:"):
        return "https://arxiv.org/abs/" + u[6:].strip()
    return u


def link_key(url: str) -> str:
    """One key per reference, however its link is written: the scheme and host in lower case, without www., a
    fragment or a trailing slash, and an arXiv paper as its abstract, whatever the version or format."""
    u = link_url(url).split("#", 1)[0].rstrip("/")
    scheme, _, rest = u.partition("://")
    host, _, path = rest.partition("/")
    host = host.lower().removeprefix("www.")
    if host == "arxiv.org" and path[:4] in ("abs/", "pdf/"):
        path = "abs/" + re.sub("v[0-9]+$", "", path[4:].removesuffix(".pdf"))
    return f"{scheme.lower()}://{host}" + (f"/{path}" if path else "")


def result_accept(template: str, files: list[str]) -> str:
    """The command that checks a result: the policy's results_check, once for each of its files."""
    def one(f):
        stem = f.rsplit(".", 1)[0] if "." in f.rsplit("/", 1)[-1] else f
        return template.replace("{file}", f).replace("{stem}", stem).replace("{module}", stem.replace("/", "."))
    return " && ".join(one(f) for f in files)


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
        self.saved: dict[str, list[dict]] = defaultdict(list)  # task -> work in progress published on it, in order
        self.hub: list[dict] = []  # the central chat's messages, in order
        self.literature: dict[str, dict] = {}  # link_key -> {url, title, by: the lit.added events vouching for it}
        self.hub_by: dict[str, list[float]] = defaultdict(list)  # author -> when it posted in the hub
        self.digest: dict | None = None
        self.description: str = ""  # what the project is about, set by an integrator
        self.main_tip: str | None = None  # the main the integrator signed last
        self.events: list[dict] = []
        self.ignored: list[tuple[dict, str]] = []
        self.max_lamport = 0
        self.votes: dict[tuple[str, str], set] = {}  # (add|remove, key) -> integrators who voted for it
        self.leases: list[Claim] = []                # claims on the integrator role, whole or per subtree
        self.verifications: dict[str, list[dict]] = defaultdict(list)  # patch -> patch.verified events
        self.accepted_count: dict[str, int] = defaultdict(int)  # author -> its accepted patches
        self.falsified: set[str] = set()  # authors of a reverted patch, and whoever verified it as passing
        self.last_seen: dict[str, float] = {}  # author -> its latest event: who is active, for votes
        self.names: dict[str, str] = {}
        self.proposals: dict[str, dict] = {}  # id -> {"ev", "votes": {owner: yes?}, "status", "reason"}
        self._by_vote: str | None = None  # the proposal being applied right now
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

    def trusted(self, author: str) -> bool:
        """Whose verifications and verdicts count in the open mode: the founders, and agents with accepted work
        and never a false verification."""
        return author in self.integrators or (self.accepted_count[author] >= TRUSTED_AFTER
                                              and author not in self.falsified)

    def challengeable(self, p, now: float) -> bool:
        """An accepted patch can still be audited, and reverted, in the open mode's window."""
        return (self.policy["mode"] == "open" and p.verdict == "accepted"
                and now - p.accepted_ts <= self.policy["window"])

    def backed(self, p, ids, outcome: str, tree: str | None = None) -> str | None:
        """Check that these verifications are `outcome`s by enough trusted owners, never the author's."""
        vs = {v["id"]: v for v in self.verifications[p.id]}
        if not isinstance(ids, list) or not all(i in vs for i in ids):
            return "verified_by must list this patch's verifications"
        chosen = [vs[i] for i in ids]
        if any(v["outcome"] != outcome or not self.trusted(v["author"]) or owner(v["name"]) == owner(p.name)
               or (tree is not None and v["tree"] != tree) for v in chosen):
            return f"verified_by must be {outcome}es by trusted agents other than the patch's own"
        need = max(1, self.policy["quorum"]) if outcome == "pass" or tree is None else 2
        if len({owner(v["name"]) for v in chosen}) < need:
            return f"it needs {need} trusted owners behind it"
        return None

    def _finalizer_rules(self, ev: dict) -> str | None:
        """What a trusted node, not a founder, needs behind its verdict in the open mode."""
        t = ev["type"]
        p = self.patches.get(ev.get("patch"))
        if t == "patch.accepted" and p:
            if ev["author"] == p.author:
                return "nobody finalizes their own patch"
            return self.backed(p, ev.get("verified_by"), "pass")
        if t == "patch.rejected" and p and ev.get("code") not in PLAIN:
            return self.backed(p, ev.get("verified_by"), "fail")
        if t == "patch.reverted":
            if not p or not self.challengeable(p, ev["ts"]):
                return "only an accepted patch, inside its challenge window, can be reverted"
            if not ev.get("merge_commit"):
                return "a revert needs its commit"
            return self.backed(p, ev.get("verified_by"), "fail", tree=p.tree)
        return None

    def active_owners(self, now: float, kind: str = "project.policy", window: int | None = None) -> set[str]:
        """Trusted owners with an event recently: who a vote on this kind is counted among. The policy's vote
        window, or the one a proposal was made under (window); for the policy itself, always the week."""
        window = ACTIVE_DAYS * 86400 if kind == "project.policy" else window or self.policy["vote_window"]
        return {owner(self.names[a]) for a, ts in self.last_seen.items() if now - ts <= window and self.trusted(a)}

    def votes_needed(self, kind: str, now: float, window: int | None = None) -> int:
        active = len(self.active_owners(now, kind, window))
        share = active * 2 // 3 + (1 if active * 2 % 3 else 0) if kind == "project.policy" else active // 2 + 1
        return max(share, min(MIN_YES, active), 1)

    def recount(self, now: float) -> None:
        """Count again the open proposals made under a short vote window: those voting on them may have gone away
        since. (Under the week's, a proposal is counted on its votes alone, as it always was.)"""
        for pid, prop in list(self.proposals.items()):
            if (prop["status"] == "open" and prop["window"] < ACTIVE_DAYS * 86400
                    and now - prop["ev"]["ts"] <= ACTIVE_DAYS * 86400
                    and sum(prop["votes"].values()) >= self.votes_needed(prop["ev"]["kind"], now, prop["window"])):
                self._tally(pid, now)

    def _proposal(self, ev: dict) -> str | None:
        if self.policy["mode"] != "open":
            return "proposals and votes are for the open mode (with an integrator, the founders decide)"
        if not self.trusted(ev["author"]):
            return "only a trusted agent may propose or vote"
        me = owner(ev["name"])
        if ev["type"] == "proposal.made":
            if ev.get("kind") not in PROPOSABLE or not isinstance(ev.get("payload"), dict):
                return f"a proposal is a kind ({', '.join(sorted(PROPOSABLE))}) and its payload"
            if ev["kind"] == "task.created":
                payload = ev["payload"]
                if payload.get("sketch") or payload.get("path"):
                    return "a voted subtask has a parent, not a sketch or a path"
                # A subtask by vote, like a path's: inside its parent's zone and key, and it doesn't replace the
                # parent, which still counts as done only when its own acceptance command passes.
                err = payload.get("parent") and self._subtask_error(self.tasks.get(payload["parent"]),
                                                                    payload.get("key") or "", payload.get("zone") or [])
                err = err or contract_error(payload)
                if err:
                    return err
                # A dependency is a task, or one that another open proposal creates: this one waits for it.
                coming = {p["ev"]["payload"].get("key") for p in self.proposals.values()
                          if p["status"] == "open" and p["ev"]["kind"] == "task.created"}
                deps = payload.get("deps") or []
                if not isinstance(deps, list) or not all(isinstance(d, str) and (self.find_task(d) or d in coming)
                                                         for d in deps):
                    return "a dependency is a task, or the key of a task another open proposal creates"
            # Counted with the vote window in force now, whatever the policy says later: a shorter one must not
            # pass, on a recount, what was proposed under the week's rule.
            self.proposals[ev["id"]] = {"ev": ev, "votes": {me: True}, "status": "open", "reason": "",
                                        "window": self.policy["vote_window"]}
            return self._tally(ev["id"], ev["ts"])
        prop = self.proposals.get(ev.get("proposal"))
        if prop is None:
            return "unknown proposal"
        if prop["status"] != "open" or ev["ts"] - prop["ev"]["ts"] > ACTIVE_DAYS * 86400:
            return "the proposal is closed"
        if not isinstance(ev.get("yes"), bool):
            return "a vote is yes: true or false"
        prop["votes"][me] = ev["yes"]
        return self._tally(prop["ev"]["id"], ev["ts"])

    def _tally(self, pid: str, now: float) -> None:
        prop = self.proposals[pid]
        kind = prop["ev"]["kind"]
        founders = {owner(self.names.get(a, "")) for a in self.integrators}
        others = self.active_owners(now) - founders  # the founders' word weighs while the project is young
        if len(others) < FOUNDERS_UNTIL and any(prop["votes"].get(f) is False for f in founders):
            prop["status"], prop["reason"] = "vetoed", "a founder said no"
            return None
        yes = sum(1 for o, v in prop["votes"].items() if v)
        if yes < self.votes_needed(kind, now, prop["window"]):
            return None
        waiting = [d for d in prop["ev"]["payload"].get("deps") or [] if kind == "task.created" and not self.find_task(d)]
        if waiting:  # voted in, but a task it depends on comes from a proposal that hasn't passed yet
            prop["reason"] = f"waiting for {', '.join(waiting)} to pass"
            return None
        # It passes: apply it as if a founder had published it, the same way on every node. The task's id and
        # everything else come from the proposal itself.
        ev = dict(prop["ev"]["payload"], type=kind, author=prop["ev"]["author"], name=prop["ev"]["name"],
                  lamport=prop["ev"]["lamport"], ts=now, id=prop["ev"]["id"])
        self._by_vote = ev["id"]
        try:
            err = self.apply(ev)
        finally:
            self._by_vote = None
        prop["status"], prop["reason"] = ("failed", err) if err else ("passed", "")
        if not err and kind == "task.created":  # the proposals that were waiting for this task
            for other, p in list(self.proposals.items()):
                if p["status"] == "open" and p["reason"].startswith("waiting for"):
                    self._tally(other, now)
        return None

    def newcomer(self, author: str) -> bool:
        """An agent that hasn't done anything here yet, nor been to the hub: it starts there (claim sends it).
        The founders aren't sent."""
        return (author not in self.integrators and not self.hub_by.get(author)
                and not any(c.author == author for cs in self.claims.values() for c in cs)
                and not any(a["author"] == author for a in self.attempts)
                and not any(p.author == author for p in self.patches.values()))

    def references(self, tid: str | None = None) -> list[dict]:
        """The literature, the references most agents vouch for first (then the newest), or those for a task."""
        refs = [r for r in self.literature.values() if tid is None or any(e.get("task") == tid for e in r["by"])]
        return sorted(refs, key=lambda r: (-len(r["by"]), -r["lamport"]))

    def _result(self, ev: dict, target: Task) -> str | None:
        """A result: the task it is, under the one it came from, and its patch (see MAX_RESULTS_PER_DAY)."""
        pol = self.policy
        if not pol["results_dir"] or not pol["results_check"]:
            return "this project takes no results yet: a founder sets them up (`policy --results-dir --results-check`)"
        if target.cancelled or target.kind == "result":
            return "a result belongs to a task, one that isn't cancelled"
        if not self.took_part(target.id, ev["author"], ev["ts"]):
            return "only someone who works on the task, or has, may publish a result on it"
        mine = [p for p in self.patches.values() if p.author == ev["author"] and self.tasks[p.task].kind == "result"]
        if sum(ev["ts"] - 86400 < p.ts <= ev["ts"] for p in mine) >= MAX_RESULTS_PER_DAY:
            return f"at most {MAX_RESULTS_PER_DAY} results a day per agent"
        if sum(p.verdict is None for p in mine) >= MAX_OPEN_RESULTS:
            return f"at most {MAX_OPEN_RESULTS} results waiting for their checks per agent"
        slug, title, notes, files = ev.get("key"), ev.get("title"), ev.get("notes", ""), ev.get("files")
        if not isinstance(slug, str) or not CHILD_KEY.match(slug):
            return f"invalid result key: {slug!r} (lowercase letters, digits and hyphens)"
        if self.find_task(f"{target.key}/{slug}"):
            return f"a task with key {target.key}/{slug} already exists"
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
            return f"a result's title says what it proves, in at most {MAX_TITLE} characters"
        if not isinstance(notes, str) or len(notes) > E.MAX_NOTES:
            return f"notes are text of at most {E.MAX_NOTES} characters"
        root = pol["results_dir"].rstrip("/") + "/"
        if (not isinstance(files, list) or not 1 <= len(files) <= MAX_RESULT_FILES or len(set(files)) != len(files)
                or not all(isinstance(f, str) and f.startswith(root) and RESULT_FILE.fullmatch(f) for f in files)):
            return f"a result is 1 to {MAX_RESULT_FILES} new files under {root}"
        taken = {f for t in self.tasks.values() if t.kind == "result" and self.status(t.id, ev["ts"]) != "cancelled"
                 for f in t.zone}
        if taken & set(files):
            return f"another result has {', '.join(sorted(taken & set(files)))}"
        if not ev.get("commit") or not valid_branch(ev.get("branch")):
            return "missing commit, or the branch is outside the patch namespace"
        tid = task_id(ev)
        self.tasks[tid] = Task(tid, f"{target.key}/{slug}", title.strip(), list(files),
                               result_accept(pol["results_check"], files), body=notes, lamport=ev["lamport"],
                               parent=target.id, depth=target.depth + 1, kind="result")
        self.patches[ev["id"]] = Patch(ev["id"], tid, ev["author"], ev["name"], ev["commit"], ev["branch"],
                                       ev["lamport"], ts=ev["ts"])
        return None

    def _hub(self, ev: dict) -> str | None:
        if sum(ev["ts"] - 86400 < ts <= ev["ts"] for ts in self.hub_by[ev["author"]]) >= MAX_HUB_PER_DAY:
            return f"at most {MAX_HUB_PER_DAY} messages and references a day per agent in the hub"
        if ev.get("task") is not None and ev["task"] not in self.tasks:
            return "unknown task"
        if ev["type"] == "hub.message":
            text = ev.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > E.MAX_NOTES:
                return f"a message is text of at most {E.MAX_NOTES} characters"
            if ev.get("reply") is not None and not any(m["id"] == ev["reply"] for m in self.hub):
                return "a reply answers a message in the hub"
            self.hub.append(ev)
        else:
            url, title, why = ev.get("url"), ev.get("title"), ev.get("why")
            if (not isinstance(url, str) or not url.lower().startswith(LINK_PREFIXES) or len(url) > MAX_URL
                    or any(ch.isspace() for ch in url)):
                return "a reference is a link: https://..., http://..., doi:... or arxiv:..."
            if not isinstance(why, str) or not why.strip() or len(why) > E.MAX_NOTES:
                return f"a reference says why it helps, in at most {E.MAX_NOTES} characters"
            ref = self.literature.get(link_key(url))
            if ref is None:
                if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
                    return f"a new reference needs its title, at most {MAX_TITLE} characters"
                self.literature[link_key(url)] = {"key": link_key(url), "url": url, "title": title.strip(),
                                                  "id": ev["id"], "lamport": ev["lamport"], "ts": ev["ts"],
                                                  "by": [ev]}
            elif any(e["author"] == ev["author"] for e in ref["by"]):
                return "this agent already added that reference"
            else:
                ref["by"].append(ev)  # found useful again, by someone else: it vouches for it
        self.hub_by[ev["author"]].append(ev["ts"])
        return None

    def took_part(self, tid: str, author: str, now: float) -> bool:
        """Works on the task now, or did: claimed it, reported on it, sent or verified a patch, proposed a sketch
        or a path. They are the ones with something to say about it."""
        return (self.works_on(tid, author, now) or any(c.author == author for c in self.claims[tid])
                or any(a.get("task") == tid and a["author"] == author for a in self.attempts)
                or any(p.author == author for p in self.patches_for(tid))
                or any(v["author"] == author for p in self.patches_for(tid) for v in self.verifications.get(p.id, ()))
                or any(k.author == author for k in self.sketches.values() if k.task == tid)
                or any(w["author"] == author for w in self.saved[tid]))

    def saves(self, tid: str) -> list[dict]:
        """The work in progress others can build on: each author's latest save on the task, newest first."""
        latest = {}
        for w in self.saved[tid]:
            latest[w["author"]] = w
        return sorted(latest.values(), key=lambda w: -w["lamport"])

    def mentions(self, name: str) -> list[dict]:
        """Notes that name this agent (@ana#2, or @ana for all of ana's) and that it hasn't answered since:
        no later note of its own on that task."""
        owner = name.rpartition("#")[0] if "#" in name else name
        tags = {f"@{name}".lower(), f"@{owner}".lower()}
        out = []
        for notes in [*self.notes.values(), self.hub]:  # a task's conversation, and the central chat
            for i, n in enumerate(notes):
                words = {w.rstrip(".,:;!?)").lower() for w in n["text"].split() if w.startswith("@")}
                if n["name"] != name and words & tags and not any(m["name"] == name for m in notes[i + 1:]):
                    out.append(n)
        return sorted(out, key=lambda n: n["lamport"])

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
              and (self.deps_ok(t) or self.deps_in_review(t)) and self.attempts_by(t.id, author) < COMPETITIVE_TRIES
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

    def under(self, t: Task) -> str | None:
        """The task it belongs to, to show it there: its parent, or, for a task voted without naming one (before
        proposals could, or by an agent that didn't), the task its key starts with: upper-constant/tail-module
        under upper-constant. Only for showing: it changes nothing a parent means."""
        if t.parent:
            return t.parent
        head = self.find_task(t.key.rpartition("/")[0]) if "/" in t.key else None
        return head.id if head else None

    def depth_under(self, t: Task) -> int:
        """How deep it's shown: how many tasks it is under (see under)."""
        up = self.under(t)
        return 1 + self.depth_under(self.tasks[up]) if up else 0

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

    def deps_in_review(self, task: Task) -> list[str]:
        """The dependencies not done yet, if every one of them is in review: the task can be worked on now, on
        top of their work (empty if some dependency isn't in review)."""
        pending = [d for d in task.deps if not self.done(d)]
        return pending if pending and all(self.in_review(d) is not None for d in pending) else []

    def in_review(self, tid: str) -> list | None:
        """What a task waiting only for verdicts would bring, to build on before anyone verifies it: its latest
        patch in review, or, split by a sketch, those of its subtasks (each done or in review). [] if it's done,
        None if some of it isn't written yet. Nothing ever waits for a verification to go on working."""
        if self.done(tid):
            return []
        waiting = [p for p in self.patches_for(tid, None) if not self.doomed(p)]
        if waiting:
            return [waiting[-1]]
        k = self.active_sketch(tid)
        if k is None or not k.child_ids:
            return None
        parts = [self.in_review(c) for c in k.child_ids]
        return None if any(x is None for x in parts) else [p for x in parts for p in x]

    def review_patches(self, tids) -> list:
        """The patches in review to build on, to have what these tasks bring, oldest first."""
        found = {p.id: p for t in tids for p in self.in_review(t) or []}
        return sorted(found.values(), key=lambda p: (p.lamport, p.id))

    def ready(self, p: Patch) -> bool:
        """Its dependencies are done and the patches it was built on accepted: it can be checked now."""
        return self.deps_ok(self.tasks[p.task]) and all(self.patches[x].verdict == "accepted" for x in p.on)

    def doomed(self, p: Patch):
        """A patch it was built on was rejected: it can't go in as it is (that patch, or None)."""
        return next((self.patches[x] for x in p.on if self.patches[x].verdict == "rejected"), None)

    def status(self, tid: str, now: float) -> str:
        task = self.tasks[tid]
        if task.mode == "exclusive" and self.done(tid):
            return "accepted"
        if task.cancelled:
            return "cancelled"
        if task.kind == "result":  # born with its patch: in review until it's accepted, gone if it's rejected
            return "review" if self.patches_for(tid, None) else "cancelled"
        if any(k.verdict is None for k in self.sketches_for(tid)):
            return "review"
        if self.active_sketch(tid):
            return "sketched"
        if not self.deps_ok(task):
            # Its dependencies are in review: workable now, on top of their patches, so nobody waits for the
            # verdicts. A patch written for it waits here until they are accepted.
            if not self.deps_in_review(task) or self.patches_for(tid, None):
                return "blocked"
        elif self.patches_for(tid, None):
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
        ex = [t for t in self.tasks.values() if t.mode == "exclusive" and not t.cancelled and t.kind != "result"]
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
        if t in ("proposal.made", "proposal.voted"):
            return self._proposal(ev)
        if t in E.INTEGRATOR_ONLY and ev["author"] not in self.integrators and ev.get("id") != self._by_vote:
            open_ok = (self.policy["mode"] == "open" and t in FINALIZE and self.trusted(ev["author"])
                       and (t != "task.created" or ev.get("sketch") or ev.get("path")))
            if not open_ok:
                return "only an integrator can publish this event"
            err = self._finalizer_rules(ev)
            if err:
                return err

        # Compare-and-swap on main: whatever moves main must say which main it was built on. If main has
        # moved since (another integrator got there first), the event doesn't count and the work is redone.
        # This, not the lease, is what keeps main from forking; the lease only avoids duplicated work.
        if t in ("patch.accepted", "patch.reverted", "sketch.accepted", "main.updated") and ev.get("base") is not None \
                and self.main_tip and ev["base"] != self.main_tip:
            return "stale base: main moved on since this was computed"

        if t.startswith("integrator."):
            return self._integrators(ev)

        if t == "task.created":
            return self._create_task(ev)

        if t == "agent.joined":  # only so the others (and the viewer) know this agent is here
            return None

        if t in ("hub.message", "lit.added"):
            return self._hub(ev)

        if t == "project.policy":
            policy = dict(self.policy)
            if "mode" in ev and ev["mode"] not in ("integrator", "open"):
                return "mode must be integrator or open"
            if "reads" in ev and (not isinstance(ev["reads"], str) or len(ev["reads"]) > 200):
                return "reads must be a command"
            if "results_dir" in ev and (not isinstance(ev["results_dir"], str)
                                        or not RESULT_FILE.fullmatch(ev["results_dir"].rstrip("/"))):
                return "results_dir must be a folder of the repository, like Erdos902/Results"
            if "results_check" in ev and (not isinstance(ev["results_check"], str) or len(ev["results_check"]) > 300
                                          or not any(f"{{{k}}}" in ev["results_check"] for k in ("file", "stem", "module"))):
                return "results_check must be a command with {file}, {stem} or {module} in it"
            policy.update({k: ev[k] for k in ("mode", "reads", "results_dir", "results_check") if k in ev})
            for key, kind, lo, hi in (("quorum", int, 0, 9), ("verifier_trust", int, 0, 100),
                                      ("spot_check", (int, float), 0, 1), ("window", int, 600, 30 * 86400),
                                      ("audit", (int, float), 0, 1), ("vote_window", int, 600, ACTIVE_DAYS * 86400)):
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
        if task is None and t != "digest.published" and t not in ("patch.accepted", "patch.rejected", "patch.reverted"):
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
        elif t == "patch.checking":  # an agent verifying it, or the integrator reviewing it, starts its check
            p = self.patches.get(ev.get("patch"))
            if p is None or p.task != task.id:
                return "unknown patch"
            if p.verdict:
                return "the patch already has a verdict"
        elif t == "patch.verified":
            p = self.patches.get(ev.get("patch"))
            if p is None or p.task != task.id:
                return "unknown patch"
            if p.verdict and not self.challengeable(p, ev["ts"]):
                return "the patch already has a verdict"
            if ev["author"] == p.author:
                return "a patch can't be verified by its own author"
            reads = ev.get("reads", ["*"])
            if not (reads == ["*"] or isinstance(reads, dict) and len(reads) <= 5000 and all(
                    isinstance(k, str) and len(k) < 300 and (v is None or isinstance(v, str) and TREE.match(v))
                    for k, v in reads.items())):
                return "reads must be [\"*\"] or the blob of each file the check read"
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
            if not self.took_part(task.id, ev["author"], ev["ts"]):
                return "only someone who works on the task, or has, may post notes on it"
            if sum(n["author"] == ev["author"] for n in self.notes[task.id]) >= MAX_TASK_NOTES:
                return f"at most {MAX_TASK_NOTES} notes per agent on a task"
            if ev.get("reply") is not None and not any(n["id"] == ev["reply"] for n in self.notes[task.id]):
                return "a reply answers a note on the same task"
            self.notes[task.id].append(ev)
        elif t == "result.submitted":
            return self._result(ev, task)
        elif t == "work.saved":
            # Unfinished work, published so it isn't lost when its author stops: whoever takes the task next starts
            # from it (`start --from`). It isn't a patch: nobody judges it, and it changes nothing by itself.
            if not isinstance(ev.get("commit"), str) or not ev["commit"] or not valid_branch(ev.get("branch")):
                return "missing commit, or the branch is outside the patch namespace"
            if not isinstance(ev.get("notes", ""), str) or len(ev.get("notes", "")) > E.MAX_NOTES:
                return f"notes must be text of at most {E.MAX_NOTES} characters"
            if not self.took_part(task.id, ev["author"], ev["ts"]):
                return "only someone who works on the task, or has, may save work on it"
            self.saved[task.id].append({k: ev[k] for k in ("id", "author", "name", "commit", "branch", "ts", "lamport")}
                                       | {"task": task.id, "notes": ev.get("notes", "")})
        elif t == "patch.submitted":
            if not ev.get("commit") or not ev.get("branch"):
                return "missing commit or branch"
            if not valid_branch(ev["branch"]):
                return "a patch branch must be under legion/ or refs/legion/patches/"
            on = ev.get("on") or []
            if not isinstance(on, list) or len(on) > MAX_ON or not all(isinstance(x, str) and x in self.patches
                                                                       for x in on):
                return f"`on` lists at most {MAX_ON} patches sent before this one"
            self.patches[ev["id"]] = Patch(ev["id"], task.id, ev["author"], ev["name"], ev["commit"],
                                           ev["branch"], ev["lamport"], ts=ev["ts"], on=on)
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
            if p.verdict == "accepted":
                p.accepted_ts = ev["ts"]
                p.verified_by = list(ev.get("verified_by") or [])
                if self.tasks[p.task].kind != "result":  # results don't earn trust: see MAX_RESULTS_PER_DAY
                    self.accepted_count[p.author] += 1
        elif t == "patch.reverted":
            p = self.patches.get(ev.get("patch"))
            if p is None or p.verdict != "accepted" or not ev.get("merge_commit"):
                return "only an accepted patch can be reverted, with its revert commit"
            p.verdict, p.code, p.reason = "reverted", "reverted", ev.get("reason", "")
            self.main_tip = ev["merge_commit"]
            if self.tasks[p.task].kind != "result":
                self.accepted_count[p.author] -= 1
            self.falsified.add(p.author)
            self.falsified.update(v["author"] for v in self.verifications[p.id]
                                  if v["outcome"] == "pass" and (v["tree"] == p.tree or v["id"] in p.verified_by))
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
        found = [self.find_task(d) if isinstance(d, str) else None for d in ev.get("deps") or []]  # by id or key
        if not all(found):
            return "unknown dependency"
        deps = [t.id for t in found]
        key = ev.get("key") or task_id(ev)
        if any(x.key == key for x in self.tasks.values()):
            return f"a task with key {key} already exists"
        err = contract_error(ev)
        if err:
            return err
        depth, sketch = 0, None
        if ev.get("parent") or ev.get("sketch") or ev.get("path"):
            parent = self.tasks.get(ev.get("parent"))
            voted = ev.get("id") == self._by_vote and not ev.get("sketch") and not ev.get("path")
            if not voted:
                sketch = self.sketches.get(ev.get("sketch") or ev.get("path"))
                if not parent or not sketch or sketch.task != parent.id or sketch.verdict != "accepted":
                    return "a subtask needs a parent and an accepted sketch or path of that parent"
            if voted:
                err = self._subtask_error(parent, key, zone)
                if err:
                    return err
            elif not key.startswith(parent.key + "/") or not all(in_zone(z.rstrip("/"), parent.zone) for z in zone):
                return "a subtask must carry its parent's key and stay inside its zone"
            depth = parent.depth + 1
        tid = task_id(ev)
        self.tasks[tid] = Task(tid, key, ev["title"], zone, ev["accept"], ev.get("mode", "exclusive"),
                               ev.get("metric"), ev.get("goal", "min"), deps, ev.get("size", "M"),
                               ev.get("body", ""), ev["lamport"], ev.get("parent"), sketch.id if sketch else None,
                               depth, contract=dict(ev.get("contract") or {}))
        if sketch:
            sketch.child_ids.append(tid)
        return None

    def _subtask_error(self, parent: Task | None, key: str, zone: list) -> str | None:
        if parent is None or parent.cancelled:
            return "a subtask needs a parent task"
        if not key.startswith(parent.key + "/") or not zone or not all(in_zone(z.rstrip("/"), parent.zone) for z in zone):
            return "a subtask must carry its parent's key and stay inside its zone"
        if parent.depth + 1 > MAX_DEPTH:
            return f"can't split more than {MAX_DEPTH} levels deep"
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
        if self.policy["mode"] == "open":  # no integrator: every trusted node finalizes everything
            return self.trusted(author)
        holder = self.leader(now, self.root_of(tid)) or self.leader(now)
        return holder.author == author if holder else author in self.integrators  # no lease: anyone may

    def pending_roots(self) -> dict[str, int]:
        """Top-level tasks with work waiting for an integrator, and how much."""
        work = defaultdict(int)
        for p in self.patches.values():
            if p.verdict is None and (self.ready(p) or self.doomed(p)):
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
        err = None if known else E.check(ev)
        if not err:  # who acts now is active now: a vote counts its own author among the active owners
            st.names[ev["author"]] = ev["name"]
            st.last_seen[ev["author"]] = max(st.last_seen.get(ev["author"], 0), ev["ts"])
            err = st.apply(ev)
        if err:
            st.ignored.append((ev, err))
            continue
        st.events.append(ev)
        st.max_lamport = max(st.max_lamport, ev["lamport"])
        if st.proposals and ev["type"] not in ("proposal.made", "proposal.voted"):
            st.recount(ev["ts"])
    return st
