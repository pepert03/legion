"""Trust score: how reliable an agent (and the person behind it) has proven to be.

It only counts evidence the integrator verified (verdicts, resolved or failed sketches) plus claims
left to expire. What an agent says about itself (reports) never counts.

The score is the mean of a Beta distribution: everyone starts at 50 with a weak prior, and each piece
of evidence moves it. Measured per agent, because evidence is signed by the agent's key; added up per
person, because that's who you decide to trust (an agent can be replaced by a fresh identity).
"""

from dataclasses import dataclass, field

from . import state as S

PRIOR = 2.0  # pseudo-observations on each side: a new agent sits at 50 and moves quickly at first

GOOD = {"accepted": 1.0, "competitive": 1.0, "sketch_resolved": 1.5, "verified": 0.25}
# How much each kind of rejection says about the agent. A conflict or someone merging first is
# mostly bad luck; failing tests or leaving the zone is not.
BAD = {"tests": 1.0, "zone": 1.5, "scaffold": 0.8, "invalid": 0.8, "metric": 0.5, "empty": 0.5,
       "missing": 0.3, "deps": 0.2, "conflict": 0.15, "done": 0.0, "base": 0.0, "no_gain": 0.0,
       "sketch_failed": 1.0, "abandoned": 0.4, "timeout": 0.1, "reverted": 4.0,
       # A verification the integrator's own check contradicted: a false pass would have let a broken patch in.
       "false_pass": 4.0, "false_fail": 1.0}
LABEL = {"accepted": "patch accepted", "competitive": "improved a competitive task",
         "sketch_resolved": "split a task that then got done", "tests": "submitted failing tests",
         "zone": "touched files outside its zone", "scaffold": "sketch without subtask tests",
         "invalid": "invalid sketch", "metric": "metric did not run", "empty": "empty patch",
         "missing": "patch not found", "deps": "submitted before its dependencies",
         "conflict": "conflict with main", "done": "task already done",
         "base": "built on a patch that was then rejected", "no_gain": "did not beat the best",
         "sketch_failed": "split a task whose parent then failed", "abandoned": "let a claim expire",
         "verified": "verified a patch correctly", "timeout": "its check ran out of time twice",
         "reverted": "an accepted patch of its was proved wrong and reverted", "false_pass": "verified as passing a patch that fails",
         "false_fail": "verified as failing a patch that passes"}


def legacy_code(reason: str) -> str:
    """Rejections from before codes existed (some still in Spanish), classified by their text."""
    r = (reason or "").lower()
    for code, words in (("zone", ("zone", "zona")), ("tests", ("acceptance", "aceptación")),
                        ("conflict", ("conflict",)), ("no_gain", ("improve", "mejora")),
                        ("done", ("already", "ya estaba")), ("deps", ("dependenc",)),
                        ("metric", ("metric", "métrica")), ("empty", ("nothing", "nada")),
                        ("scaffold", ("scaffold", "andamiaje"))):
        if any(w in r for w in words):
            return code
    return "missing"


@dataclass
class Trust:
    good: float = 0.0
    bad: float = 0.0
    counts: dict = field(default_factory=dict)

    def add(self, kind: str) -> None:
        if kind in GOOD:
            self.good += GOOD[kind]
        else:
            self.bad += BAD.get(kind, 0.5)
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def merge(self, other: "Trust") -> None:
        self.good += other.good
        self.bad += other.bad
        for k, v in other.counts.items():
            self.counts[k] = self.counts.get(k, 0) + v

    @property
    def score(self) -> int:
        return round(100 * (self.good + PRIOR) / (self.good + self.bad + 2 * PRIOR))

    @property
    def evidence(self) -> float:
        return self.good + self.bad

    def as_dict(self) -> dict:
        level = "new" if self.evidence < 2 else "high" if self.score >= 65 else "low" if self.score < 40 else "fair"
        return {"score": self.score, "level": level, "good": round(self.good, 2), "bad": round(self.bad, 2),
                "breakdown": [{"kind": k, "label": LABEL.get(k, k), "count": n,
                               "effect": "up" if k in GOOD else "none" if BAD.get(k) == 0 else "down"}
                              for k, n in sorted(self.counts.items(), key=lambda kv: -kv[1])]}


def owner_of(name: str) -> str:
    person, sep, num = name.rpartition("#")
    return person if sep and person and num.isdigit() else name


def compute(st: S.State, now: float) -> tuple[dict[str, Trust], dict[str, Trust]]:
    """(trust per agent key, trust per person)."""
    agents: dict[str, Trust] = {}
    names: dict[str, str] = {}
    get = lambda author: agents.setdefault(author, Trust())  # noqa: E731
    for ev in st.events:
        names[ev["author"]] = ev["name"]
    for p in st.patches.values():
        if p.verdict == "accepted" and st.tasks[p.task].kind != "result":  # results earn no trust: they're cheap
            get(p.author).add("competitive" if st.tasks[p.task].mode == "competitive" else "accepted")
        elif p.verdict == "rejected":
            get(p.author).add(p.code or legacy_code(p.reason))
    for k in st.sketches.values():
        if k.outcome == "resolved":
            get(k.author).add("sketch_resolved")
        elif k.outcome == "failed":
            get(k.author).add("sketch_failed")
        elif k.verdict == "rejected":
            get(k.author).add(k.code or legacy_code(k.reason))
    for p in st.patches.values():  # verifications, against the verdict reached on the same trees
        if not p.verdict or not p.tree:
            continue
        for v in st.verifications.get(p.id, ()):
            if v["tree"] != p.tree and v["id"] not in p.verified_by:  # judged on other trees: says nothing
                continue
            passed, accepted = v["outcome"] == "pass", p.verdict == "accepted"
            if passed == accepted and (accepted or p.code in ("tests", "reverted")):
                get(v["author"]).add("verified")
            elif passed and p.code in ("tests", "reverted"):
                get(v["author"]).add("false_pass")
            elif not passed and accepted:
                get(v["author"]).add("false_fail")
    for tid, claims in st.claims.items():
        for c in claims:
            delivered = any(p.author == c.author and p.lamport > c.lamport for p in st.patches_for(tid)) or \
                        any(k.author == c.author and k.lamport > c.lamport for k in st.sketches_for(tid))
            if not c.released and not c.active(now) and not delivered and not st.done(tid):
                get(c.author).add("abandoned")
    people: dict[str, Trust] = {}
    for author, t in agents.items():
        if author in st.integrators:
            continue  # the integrator judges; it isn't judged by its own verdicts
        people.setdefault(owner_of(names.get(author, author)), Trust()).merge(t)
    return agents, people
