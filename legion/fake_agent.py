"""A fake agent to exercise the protocol without spending tokens.

It uses the CLI like a real agent, but instead of programming it copies prepared solutions.
Sometimes it errs on purpose: it first tries a buggy version, submits without checking, or
touches files outside its zone, to exercise reports and rejections.
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path


def sh(*cmd: str, shell: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd[0] if shell else cmd, shell=shell, capture_output=True,
                          encoding="utf-8", errors="replace")


def legion(*args: str) -> subprocess.CompletedProcess:
    return sh(sys.executable, "-m", "legion", *args)


def git(*args: str) -> str:
    p = sh("git", *args)
    if p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class FakeAgent:
    def __init__(self, solutions: Path, rng: random.Random, p_bug: float, p_liar: float, p_outside: float):
        self.solutions = solutions
        self.rng = rng
        self.p_bug, self.p_liar, self.p_outside = p_bug, p_liar, p_outside

    def status(self) -> dict:
        return json.loads(legion("status", "--json").stdout)

    def fresh(self, tid: str) -> None:
        p = legion("start", tid, "--force")
        if p.returncode:
            raise RuntimeError(f"start failed: {p.stderr.strip()}")

    def apply(self, name: str) -> None:
        src = self.solutions / name
        for f in src.rglob("*"):
            if f.is_file() and f.name != "NOTES.txt":
                dest = Path(f.relative_to(src))
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)

    def notes(self, name: str) -> str:
        p = self.solutions / name / "NOTES.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def commit(self, msg: str) -> None:
        git("add", "-A")
        git("commit", "-qm", msg)

    def submit(self, tid: str, check: bool = True) -> str | None:
        p = legion("submit", tid, "--json", *([] if check else ["--no-check"]))
        if p.returncode:
            log(f"submit refused locally: {p.stderr.strip().splitlines()[0] if p.stderr.strip() else p.returncode}")
            return None
        return json.loads(p.stdout)["patch"]

    def wait_verdict(self, pid: str, timeout: float = 240) -> tuple[str, str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for p in self.status()["patches"]:
                if p["id"] == pid and p["verdict"]:
                    return p["verdict"], p["reason"]
            time.sleep(1.5)
        return "timeout", "the integrator did not answer in time"

    def report(self, tid: str, outcome: str, notes: str) -> None:
        legion("report", tid, "--outcome", outcome, "--notes", notes[:1900])

    def propose_sketch(self, task: dict) -> None:
        """Split the task with the scaffold prepared in solutions/<key>.sketch."""
        tid, key = task["id"], task["key"]
        src = self.solutions / f"{key}.sketch"
        self.fresh(tid)
        tests = Path("legion_sketches") / tid
        for f in src.rglob("*"):
            if not f.is_file() or f.name == "sketch.json":
                continue
            rel = f.relative_to(src)
            dest = tests / rel.relative_to("_tests") if rel.parts[0] == "_tests" else rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
        spec = (src / "sketch.json").read_text(encoding="utf-8").replace("{tests}", tests.as_posix())
        Path("..", f"sketch-{tid}.json").write_text(spec, encoding="utf-8")
        self.commit(f"{key}: scaffold to split the task")
        p = legion("sketch", tid, str(Path("..", f"sketch-{tid}.json")), "--json")
        if p.returncode:
            log(f"{key}: couldn't propose the sketch: {p.stderr.strip()}")
            legion("release", tid)
            return
        sid = json.loads(p.stdout)["sketch"]
        log(f"{key}: proposing to split it into subtasks (sketch {sid[:8]})")
        deadline = time.time() + 240
        while time.time() < deadline:
            k = next((k for k in self.status()["sketches"] if k["id"] == sid), None)
            if k and k["verdict"]:
                log(f"{key}: sketch {k['verdict']}" + (f": {', '.join(k['children'])}" if k["children"] else
                                                      f" ({k['reason']})"))
                return
            time.sleep(1.5)

    def propose_path(self, task: dict) -> None:
        """On a stuck task, open the new route prepared in solutions/<key>.path (same layout as a sketch)."""
        tid, key = task["id"], task["key"]
        src = self.solutions / f"{key}.path"
        self.fresh(tid)
        tests = Path("legion_sketches") / tid
        for f in src.rglob("*"):
            if not f.is_file() or f.name == "path.json":
                continue
            rel = f.relative_to(src)
            dest = tests / rel.relative_to("_tests") if rel.parts[0] == "_tests" else rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
        spec = (src / "path.json").read_text(encoding="utf-8").replace("{tests}", tests.as_posix())
        Path("..", f"path-{tid}.json").write_text(spec, encoding="utf-8")
        self.commit(f"{key}: files for a new path")
        p = legion("path", tid, str(Path("..", f"path-{tid}.json")), "--json")
        if p.returncode:
            log(f"{key}: couldn't propose a path: {p.stderr.strip()}")
            return
        log(f"{key} is stuck ({task['failures']} failures): proposing a new path ({json.loads(p.stdout)['path'][:8]})")

    def work(self, task: dict) -> None:
        tid, key = task["id"], task["key"]
        log(f"got {key}")
        failed = any(k["task"] == tid and k["outcome"] == "failed" for k in self.status()["sketches"])
        if (self.solutions / f"{key}.sketch").exists() and not failed:
            self.propose_sketch(task)
            return  # if the sketch already failed once, it solves the task whole
        key = key.replace("/", "~")  # the solution for summary/fmt lives in summary~fmt
        buggy = f"{key}.buggy"
        has_buggy = (self.solutions / buggy).exists()
        for attempt in range(3):
            if attempt and legion("claim", tid).returncode:  # renews the claim, or gets it back if it expired
                log(f"{key}: can't claim it any more; leaving it")
                return
            self.fresh(tid)
            roll = self.rng.random() if attempt == 0 else 1.0
            if has_buggy and roll < self.p_bug:
                self.apply(buggy)
                if sh(task["accept"], shell=True).returncode:
                    log(f"{key}: my first version fails the tests; noting it and redoing it")
                    self.report(tid, "failure", f"First attempt discarded locally. {self.notes(buggy)}")
                    continue
            if has_buggy and roll < self.p_bug + self.p_liar:
                self.apply(buggy)
                self.commit(f"{key}: first version")
                log(f"{key}: submitting without testing")
                pid = self.submit(tid, check=False)
            elif roll < self.p_bug + self.p_liar + self.p_outside:
                self.apply(key)
                with open("README.md", "a", encoding="utf-8") as f:
                    f.write(f"\nNote from {key}.\n")
                self.commit(f"{key}: solution (and a README tweak)")
                log(f"{key}: submitting with a file outside my zone")
                pid = self.submit(tid, check=False)
            else:
                self.apply(key)
                self.commit(f"{key}: solution")
                legion("heartbeat", tid)
                pid = self.submit(tid)
            if not pid:
                continue
            verdict, reason = self.wait_verdict(pid)
            log(f"{key}: verdict {verdict}" + (f" ({reason.splitlines()[0]})" if reason else ""))
            if verdict == "accepted":
                self.report(tid, "success", f"Implemented {task['key']} following the task description.")
                return
            self.report(tid, "failure", f"Patch rejected: {reason}")
        legion("release", tid)

    def compete(self, task: dict) -> None:
        tid, key = task["id"], task["key"]
        variants = sorted(p.name for p in self.solutions.glob(f"{key}.v*"))
        if not variants:
            return
        variant = self.rng.choice(variants)
        self.fresh(tid)
        self.apply(variant)
        self.commit(f"{key}: {variant}")
        log(f"competing on {key} with {variant}")
        pid = self.submit(tid)
        if pid:
            verdict, reason = self.wait_verdict(pid)
            log(f"{key}: verdict {verdict}" + (f" ({reason.splitlines()[0]})" if reason else ""))
            self.report(tid, "success" if verdict == "accepted" else "failure",
                        f"{self.notes(variant)} Result: {verdict}. {reason}".strip())

    def run(self, deadline: float) -> None:
        tried, proposed = set(), set()
        while time.time() < deadline:
            p = legion("claim", "--json")
            if p.returncode == 6:  # new here: it starts at the hub
                legion("chat", "Starting: I read the hub and the literature; nothing to add yet.")
                continue
            if p.returncode == 0:
                task = json.loads(p.stdout)["task"]
                if task["mode"] == "competitive":  # handed out once the exclusive tasks are done
                    tried.add(task["id"])
                    self.compete(task)
                    continue
                try:
                    self.work(task)
                except Exception as e:  # an agent that fails must let go of the task, not hold it
                    log(f"error working on {task['key']}: {e}; releasing the task")
                    legion("release", task["id"])
                continue
            if p.returncode == 3:
                log("another agent claimed the same task first; trying another")
                continue
            if p.returncode not in (4, 5):
                log(f"claim failed ({p.returncode}): {p.stderr.strip()}")
                time.sleep(2)
                continue
            st = self.status()
            stuck = [t for t in st["tasks"] if t.get("stalled") and not t.get("open_paths")
                     and (self.solutions / f"{t['key']}.path").exists() and t["id"] not in proposed]
            if stuck:
                proposed.add(stuck[0]["id"])
                self.propose_path(stuck[0])
                continue
            comp = [t for t in st["tasks"] if t["mode"] == "competitive" and t["id"] not in tried]
            if comp:
                tried.add(comp[0]["id"])
                self.compete(comp[0])
                continue
            if st["exclusive_done"]:
                log("no tasks left; done")
                return
            time.sleep(2)  # some tasks are blocked or in review
        log("out of time")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solutions", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--p-bug", type=float, default=0.3)
    ap.add_argument("--p-liar", type=float, default=0.15)
    ap.add_argument("--p-outside", type=float, default=0.1)
    a = ap.parse_args()
    agent = FakeAgent(Path(a.solutions), random.Random(a.seed), a.p_bug, a.p_liar, a.p_outside)
    agent.run(time.time() + a.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
