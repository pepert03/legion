"""The integrator reviews pending patches and sketches, and is the only one who moves main forward."""

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import coordgit
from . import state as S
from .coordgit import git, run
from .ctx import Ctx, Fail

NUM = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

# Every rejection carries a code, so the trust score can tell "your tests fail" from bad luck.
# missing · empty · zone · conflict · done · deps · tests · metric · no_gain · invalid · scaffold


def tail(text: str, n: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


PYTHON = re.compile(r"^(python3?|py)(?=\s|$)")


def run_cmd(cmd: str, cwd, timeout: int) -> tuple[int | None, str]:
    # Tasks say `python ...`, but a machine may only have python3, or a venv: use the Python running legion.
    cmd = PYTHON.sub(lambda _: f'"{sys.executable}"', cmd.strip(), count=1)
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, f"timed out ({timeout}s)"


def measure(cmd: str, cwd, timeout: int) -> tuple[float | None, str]:
    code, out = run_cmd(cmd, cwd, timeout)
    nums = NUM.findall(out) if code == 0 else []
    return (float(nums[-1]) if nums else None), out


def better(value: float, ref: float, goal: str) -> bool:
    return value < ref if goal == "min" else value > ref


def reject(reason: str, code: str):
    return "rejected", {"reason": reason, "code": code}


def prepare_merge(c: Ctx, commit: str, branch: str, allowed: list, msg: str):
    """Fetch the commit, check it only touches `allowed`, and merge it into main without any worktree.

    Returns (merged, head), ("rejected", payload), or None to retry later.
    """
    repo = c.repo
    coordgit.refresh_base(repo)
    main = coordgit.base_ref(repo)
    # Merge on top of the main the integrators signed last, and say so (the event's base): if another
    # integrator moves main in the meantime, the state discards this verdict and the patch is redone.
    tip = c.st.main_tip
    if tip and not coordgit.has_commit(repo, tip):
        return None  # that main hasn't reached this node yet
    if not coordgit.fetch_patch(repo, branch):
        return reject(f"branch {branch} not found", "missing")
    if not coordgit.has_commit(repo, commit):
        if coordgit.is_p2p(repo):
            return None  # the event arrived before the commit: gossip will bring it on a later sync
        return reject("the commit is not on its branch", "missing")
    head = tip or git(repo, "rev-parse", main + "^{commit}")
    base = git(repo, "merge-base", head, commit)
    files = [f for f in git(repo, "diff", "--name-only", base, commit).splitlines() if f]
    if not files:
        return reject("it changes nothing", "empty")
    outside = [f for f in files if not S.in_zone(f, allowed)]
    if outside:
        return reject(f"touches files outside its zone ({', '.join(allowed)}): {', '.join(outside)}", "zone")
    # The merge runs on git objects (no worktree, no stash) and commands run on a plain copy of the files
    # in a short temporary path, so Windows' 260-character limit never gets in the way.
    # No container yet: with code from strangers this needs a sandbox.
    m = run(repo, "merge-tree", "--write-tree", head, commit)
    if m.returncode == 1:
        return reject(f"conflicts with main: update your branch from {main}", "conflict")
    if m.returncode:
        raise coordgit.GitError(f"git merge-tree failed: {m.stderr.decode('utf-8', 'replace').strip()}")
    tree = m.stdout.decode().split()[0]
    merged = git(repo, "commit-tree", tree, "-p", head, "-p", commit, "-m", msg, env=coordgit.author_env(c.ident))
    return merged, head


def review_sketch(c: Ctx, k: S.Sketch) -> tuple[str, dict] | None:
    """A sketch is reviewed without running anything: its subtasks aren't done, so their tests fail.

    What makes it safe without a human audit: it can't leave the parent's zone, and the parent only
    counts as done if, at the end, it passes its own acceptance command, the one a human did audit.
    """
    st, task = c.st, c.st.tasks[k.task]
    if st.done(task.id) or task.cancelled:
        return reject("the task was already done", "done")
    err = st.check_children(task, k.children)
    if err:
        return reject(err, "invalid")
    tests_dir = S.sketch_dir(task.id)
    if not coordgit.has_commit(c.repo, k.commit) and not coordgit.is_p2p(c.repo):
        coordgit.fetch_patch(c.repo, k.branch)
    if coordgit.has_commit(c.repo, k.commit):
        main = coordgit.base_ref(c.repo)
        base = git(c.repo, "merge-base", main, k.commit)
        files = git(c.repo, "diff", "--name-only", base, k.commit).splitlines()
        if not any(S.in_zone(f, [tests_dir]) for f in files):
            return reject(f"the scaffold must include the subtasks' tests in {tests_dir}/", "scaffold")
    prepared = prepare_merge(c, k.commit, k.branch, task.zone + [tests_dir],
                             f"legion: sketch of {task.key} ({k.name}, {k.id[:8]})")
    if prepared is None or isinstance(prepared, tuple) and prepared[0] == "rejected":
        return prepared
    merged, head = prepared
    return "accepted", {"merge_commit": merged, "base": head}


def create_children(c: Ctx, k: S.Sketch) -> None:
    parent = c.st.tasks[k.task]
    ids = {}
    for ch in k.children:
        ev = c.emit("task.created", key=f"{parent.key}/{ch['key']}", title=ch["title"], zone=ch["zone"],
                    accept=ch["accept"], mode="exclusive", deps=[ids[d] for d in ch.get("deps") or []],
                    size=ch.get("size", "S"), body=ch.get("body", ""), parent=parent.id, sketch=k.id)
        ids[ch["key"]] = S.task_id(ev)


def resolve_parents(c: Ctx, timeout: int, log=print) -> int:
    """Once every subtask of a sketch is done, the parent passes (or not) its own test."""
    done = 0
    while True:
        c.reload()
        ready = c.st.ready_to_resolve()
        if not ready:
            return done
        k = ready[0]
        task = c.st.tasks[k.task]
        coordgit.refresh_base(c.repo)
        head = git(c.repo, "rev-parse", coordgit.base_ref(c.repo) + "^{commit}")
        tmp = Path(tempfile.mkdtemp(prefix="lgr-"))
        try:
            code, out = run_cmd(task.accept, export(c.repo, head, tmp / "m"), timeout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if code == 0:
            c.emit("task.resolved", task=task.id, sketch=k.id)
            log(f"RESOLVED {task.key}: its {len(k.child_ids)} subtasks are done and it passes its own test")
        else:
            c.emit("sketch.failed", sketch=k.id, task=task.id,
                   reason=f"with every subtask done, the parent's test still fails:\n{tail(out)}")
            log(f"SKETCH FAILED for {task.key} by {k.name}: the task is open again")
        c.sync()
        done += 1


def review(c: Ctx, p: S.Patch, timeout: int) -> tuple[str, dict] | None:
    """("accepted" | "rejected", payload), or None if main moved and it must be retried."""
    st, repo, task = c.st, c.repo, c.st.tasks[p.task]
    if task.mode == "exclusive" and st.done(task.id):
        return reject("the task was already done by another patch", "done")
    if not st.deps_ok(task):
        return reject("its dependencies are not accepted yet", "deps")
    prepared = prepare_merge(c, p.commit, p.branch, task.zone, f"legion: {task.key} ({p.name}, patch {p.id[:8]})")
    if prepared is None or isinstance(prepared, tuple) and prepared[0] == "rejected":
        return prepared
    merged, head = prepared
    tmp = Path(tempfile.mkdtemp(prefix="lgr-"))
    try:
        baseline = None
        if task.metric and st.best(task.id) is None:
            baseline, _ = measure(task.metric, export(repo, head, tmp / "base"), timeout)
        wt = export(repo, merged, tmp / "m")
        code, out = run_cmd(task.accept, wt, timeout)
        if code != 0:
            return reject(f"the acceptance command fails:\n{tail(out)}", "tests")
        payload = {}
        if task.metric:
            value, out = measure(task.metric, wt, timeout)
            if value is None:
                return reject(f"the metric did not print a number:\n{tail(out)}", "metric")
            ref = st.best(task.id) if st.best(task.id) is not None else baseline
            if ref is not None and not better(value, ref, task.goal):
                return reject(f"does not improve the metric: {value:g} vs {ref:g}", "no_gain")
            payload["metric"] = value
        payload["merge_commit"] = merged
        payload["base"] = head
        return "accepted", payload
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def export(repo, commit: str, dest: Path) -> Path:
    """Copy a commit's files to dest, without history (git archive)."""
    data = git(repo, "archive", "--format=tar", commit, binary=True)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python < 3.12: git archive never produces absolute paths or ..
            tar.extractall(dest)
    return dest


STATUS_REF = "refs/heads/legion-status"


def status_markdown(st: S.State, now: float) -> str:
    from .cli import AGENT_PROMPT, status_table
    project = st.project.get("project") if st.project else "?"
    lines = [f"# {project}: Legion status", "",
             f"Generated by the integrator from the events on the `coord` branch (up to {st.max_lamport}).",
             "This branch rewrites itself: don't commit to it.", "",
             "```", status_table(st, now), "```", ""]
    judged = [p for p in sorted(st.patches.values(), key=lambda p: p.lamport) if p.verdict][-15:]
    if judged:
        lines += ["## Latest verdicts", ""]
        for p in reversed(judged):
            reason = f": {p.reason.splitlines()[0]}" if p.reason else ""
            metric = f" (metric {p.metric:g})" if p.metric is not None else ""
            lines.append(f"- **{st.tasks[p.task].key}** by {p.name}: {p.verdict}{metric}{reason}")
        lines.append("")
    lines += ["## Shared digest", "", st.digest["text"].strip() if st.digest else "(no digest yet)", "",
              "## How to contribute", "",
              "1. `python -m legion join <this-repository-url> --name YOUR_NAME`",
              f"2. Open your agent in the new folder and tell it: *{AGENT_PROMPT}*", ""]
    return "\n".join(lines)


def publish_status(c: Ctx, text: str) -> bool:
    """Publish the status as README.md of the legion-status branch, which GitHub shows when selected."""
    blob = git(c.repo, "hash-object", "-w", "--stdin", input=text.encode("utf-8"))
    tree = git(c.repo, "mktree", input=f"100644 blob {blob}\tREADME.md\n".encode())
    commit = git(c.repo, "commit-tree", tree, "-m", "legion status", env=coordgit.author_env(c.ident))
    if coordgit.is_p2p(c.repo):  # without origin, the branch stays in the integrator's repository
        return run(c.repo, "update-ref", STATUS_REF, commit).returncode == 0
    return run(c.repo, "push", "-q", "-f", "origin", f"{commit}:{STATUS_REF}").returncode == 0


def follow_main(c: Ctx) -> None:
    """Make the published main match the one the state says is official (after a takeover, or a lost race)."""
    tip = c.st.main_tip
    if tip and coordgit.has_commit(c.repo, tip) and coordgit.rev(c.repo, coordgit.MAIN) != tip:
        coordgit.publish_main(c.repo, tip)


def emit_verdict(c: Ctx, type: str, log, **payload) -> bool:
    """Publish a verdict; if another integrator moved main first, the state refuses it and it's retried."""
    try:
        c.emit(type, **payload)
    except Fail as e:
        log(f"{type} not recorded ({e}); will review it again on the new main")
        c.sync()
        return False
    c.sync()
    follow_main(c)
    return True


def finish_interrupted(c: Ctx, log) -> None:
    """An integrator can die between accepting a sketch and creating its subtasks (separate events).
    Whoever integrates next finishes the job, so the parent is never left split with nothing under it."""
    for k in list(c.st.sketches.values()):
        if k.verdict == "accepted" and not k.outcome and not k.child_ids:
            create_children(c, k)
            c.sync()
            log(f"created the subtasks of the interrupted sketch for {c.st.tasks[k.task].key}")


def integrate_once(c: Ctx, timeout: int, log=print, keep_branches: bool = False) -> int:
    c.sync()
    c.require_integrator()
    follow_main(c)
    finish_interrupted(c, log)
    pending = sorted((p for p in c.st.patches.values() if p.verdict is None), key=lambda p: (p.lamport, p.id))
    done = 0
    sketches = sorted((k for k in c.st.sketches.values() if k.verdict is None), key=lambda k: (k.lamport, k.id))
    for k in sketches:
        c.reload()
        k = c.st.sketches[k.id]
        if k.verdict:
            continue
        try:
            result = review_sketch(c, k)
        except coordgit.GitError as e:
            log(f"git error reviewing sketch {k.id[:8]}; will retry: {e}")
            continue
        if result is None:
            continue
        verdict, payload = result
        if not emit_verdict(c, "sketch." + verdict, log, sketch=k.id, task=k.task, **payload):
            continue
        if verdict == "accepted":
            create_children(c, c.st.sketches[k.id])
            c.sync()
        if not keep_branches:
            coordgit.drop_patch(c.repo, k.branch)
        key = c.st.tasks[k.task].key
        log(f"SKETCH {verdict.upper()} {key} by {k.name}"
            + (f": {len(k.children)} subtasks" if verdict == "accepted" else f": {payload['reason'].splitlines()[0]}"))
        done += 1
    for p in pending:
        c.reload()
        p = c.st.patches[p.id]
        if p.verdict:
            continue
        try:
            result = review(c, p, timeout)
        except coordgit.GitError as e:  # a git failure on this machine is not the patch's fault: no rejection
            log(f"git error reviewing {p.id[:8]}; will retry: {e}")
            continue
        if result is None:
            log(f"patch {p.id[:8]} is not ready yet (main moved or its commit hasn't arrived); will retry")
            continue
        verdict, payload = result
        if not emit_verdict(c, "patch." + verdict, log, patch=p.id, task=p.task, **payload):
            continue
        if not keep_branches:  # the patch is in main or discarded; its commit is still referenced in coord
            coordgit.drop_patch(c.repo, p.branch)
        key = c.st.tasks[p.task].key
        extra = f" (metric {payload['metric']:g})" if "metric" in payload else ""
        log(f"{verdict.upper()} {key} by {p.name}{extra}"
            + (f": {payload['reason'].splitlines()[0]}" if verdict == "rejected" else ""))
        done += 1
    try:
        done += resolve_parents(c, timeout, log)
    except coordgit.GitError as e:
        log(f"git error checking tasks with subtasks; will retry: {e}")
    return done


# --- the integration lease ---------------------------------------------------------------
#
# Any current integrator can integrate, but only one at a time: whoever holds the lease. It's renewed
# on every pass; if its holder disappears it expires and the next integrator in line takes over.
# Correctness doesn't depend on it (that's the compare-and-swap on main); it only avoids two
# integrators reviewing the same patches at once.

LEASE_SETTLE = float(os.environ.get("LEGION_CLAIM_SETTLE", "1.0"))


def hold_lease(c: Ctx, ttl: int) -> tuple[bool, str | None]:
    """(am I the integrator now, who is). Takes the lease if it's free, renews it if it's mine."""
    c.sync()
    now = time.time()
    lead = c.st.leader(now)
    if lead and lead.author == c.me:
        if now - lead.last_beat > ttl / 2:  # each renewal is an event everyone keeps: not more often than needed
            c.emit("integrator.renew")
            c.sync()
        return True, lead.name
    if lead:
        release_lease(c)  # a stale lease of mine would only muddy who's next
        return False, lead.name
    c.emit("integrator.lease", ttl=ttl)
    c.sync()
    time.sleep(LEASE_SETTLE)
    c.sync()
    lead = c.st.leader(time.time())
    if lead and lead.author == c.me:
        return True, lead.name
    release_lease(c)
    return False, lead.name if lead else None


def release_lease(c: Ctx) -> None:
    if any(l.author == c.me and not l.released and l.active(time.time()) for l in c.st.leases):
        try:
            c.emit("integrator.release")
            c.sync()
        except Fail:
            pass
