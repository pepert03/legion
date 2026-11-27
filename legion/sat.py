"""A SAT solver for agents whose machine has none: a problem solved, and its answer certified, by a node that has one.

An agent without a solver could encode a question as a CNF but never settle it: in Erdős #902 the complete 57-vertex
S4 encoding (33 million clauses) was generated and left unsolved, and the cells of L3-11 where the conjecture bites
stayed open after an hour each of a toy solver, because no agent's machine had kissat or a proof checker.

    legion sat FILE [--seconds S] [--vars K]   ask: a CNF (.cnf, .cnf.gz, .cnf.xz) or a .py printing one
    legion sat                                 your requests and their outcomes
    legion sat --serve                         solve what was asked (a trusted agent with kissat: the verifier does)

The problem goes as a commit: main plus the file and, for a .py, the files beside it (its helpers). The answer comes
back as a sat.checked, in the asker's inbox: sat, unsat or unknown (out of time), and whether it is certified. A
model is checked here against every clause, and the values of its first K variables come back; a refutation is
certified when drat-trim verifies kissat's DRAT proof. An answer not certified is the solver's word only. Running a
.py runs the asker's code on the solving node: only trusted agents ask, as for Lean.
"""

import gzip
import lzma
import os
import re
import shutil
import time
from pathlib import Path

from . import coordgit
from . import integrator
from . import state as S
from .ctx import Ctx, Fail

MAX_FILE = 5_000_000  # bytes a problem (and the files beside a .py) may have: bigger, send the script that makes it
SECONDS = 1200  # solving time asked for by default
GEN_TIMEOUT = 900  # seconds a .py may take to print its CNF
SERVE_MAX = 1  # requests answered by one `sat --serve`: a solve may take an hour, and the patches wait meanwhile
OUT = 1500  # characters of output sent back
STATUS = {10: "sat", 20: "unsat"}  # kissat's exit codes; anything else after a clean run: unknown


def tool(name: str) -> str | None:
    """A solver's path: LEGION_<NAME> (LEGION_KISSAT, LEGION_DRAT_TRIM), or on the PATH."""
    return os.environ.get("LEGION_" + re.sub(r"\W", "_", name).upper()) or shutil.which(name)


def files_for(path: Path) -> list[Path]:
    """What goes with a problem: a CNF alone; a script with the files beside it, which it may import or read."""
    if path.suffix != ".py":
        return [path]
    return [path] + sorted(p for p in path.parent.iterdir() if p.is_file() and p != path)


def request(c: Ctx, file: str, seconds: int, n: int) -> dict:
    """Ask for the problem `file` (a path in this clone) to be solved: a commit of main plus it goes with the event."""
    from .cli import result_commit  # the commit a result goes as: main plus its files
    path = Path(file)
    root = Path(c.repo).resolve()
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        raise Fail(f"{file} isn't in this clone: put it in the folder of the task it's for", 2) from None
    if not rel.endswith(S.SAT_INPUT) or not path.is_file():
        raise Fail(f"{file}: a CNF of this clone (.cnf, .cnf.gz, .cnf.xz) or a .py that prints one", 2)
    files = files_for(path)
    if sum(f.stat().st_size for f in files) > MAX_FILE:
        raise Fail(f"{file} (with the files beside it) has over {MAX_FILE // 1_000_000} MB: send a .py that prints "
                   "the CNF, in a folder of its own", 2)
    if not 60 <= seconds <= S.SAT_MAX_SECONDS:
        raise Fail(f"--seconds: from 60 to {S.SAT_MAX_SECONDS}", 2)
    if not 0 <= n <= S.SAT_MAX_VARS:
        raise Fail(f"--vars: at most {S.SAT_MAX_VARS} (put the variables you need first in the encoding)", 2)
    coordgit.refresh_base(c.repo)
    rels = [f.resolve().relative_to(root).as_posix() for f in files]
    commit = result_commit(c.repo, coordgit.base_ref(c.repo), rels, f"SAT solve of {rel}", c.ident)
    k = sum(r["ev"]["author"] == c.me for r in c.st.sat.values()) + 1
    branch = coordgit.patch_branch(c.repo, "sat", c.ident.short, k)
    coordgit.publish_patch(c.repo, branch, commit)
    ev = c.emit("sat.requested", path=rel, commit=commit, branch=branch, seconds=seconds, vars=n)
    c.sync()
    return ev


def plain_cnf(folder: Path, rel: str, work: Path) -> tuple[Path | None, str]:
    """The problem as a plain DIMACS file (a proof checker reads only that), or None and why not."""
    src, out = folder / rel, work / "problem.cnf"
    if rel.endswith(".py"):
        code, text = integrator.run_cmd(f'python "{src.name}" > "{out}"', src.parent, GEN_TIMEOUT)
        if code is None:
            return None, f"the script printing the CNF ran over {GEN_TIMEOUT} s"
        if code:
            return None, f"the script printing the CNF failed (exit {code}): {text.strip()[-OUT:]}"
        return out, ""
    if rel.endswith((".gz", ".xz")):
        with (gzip.open if rel.endswith(".gz") else lzma.open)(src, "rb") as f, open(out, "wb") as g:
            shutil.copyfileobj(f, g, 1 << 20)
        return out, ""
    return src, ""


def model_of(output: str) -> dict[int, bool]:
    """The assignment in a solver's `v` lines."""
    value = {}
    for line in output.splitlines():
        if line.startswith("v "):
            for tok in line[2:].split():
                x = int(tok)
                if x:
                    value[abs(x)] = x > 0
    return value


def check_model(cnf: Path, value: dict[int, bool]) -> str | None:
    """None if the assignment satisfies every clause of the CNF, else which clause it falsifies."""
    n, ok, inside = 0, False, False
    with open(cnf, "rb") as f:
        for line in f:
            if not line.strip() or line[:1] in (b"c", b"p", b"%"):
                continue
            for tok in line.split():
                x = int(tok)
                if x == 0:
                    n += 1
                    if not ok:
                        return f"clause {n} is false under the model"
                    ok = inside = False
                else:
                    inside = True
                    if not ok and value.get(abs(x)) == (x > 0):
                        ok = True
    return "the CNF ends in the middle of a clause" if inside else None


def solve(cnf: Path, work: Path, seconds: int, n: int) -> dict:
    """kissat on the CNF; its answer checked: the model against the clauses, a refutation's proof by drat-trim."""
    kissat, checker = tool("kissat"), tool("drat-trim")
    proof = work / "proof.drat"
    t0 = time.time()
    cmd = f'"{kissat}" -q --time={seconds} "{cnf}"' + (f' "{proof}" --no-binary' if checker else "")
    code, out = integrator.run_cmd(cmd, work, seconds + 120)
    took = time.time() - t0
    outcome = STATUS.get(code, "unknown" if code in (0, None) else "error")
    said = [line for line in out.splitlines() if not line.startswith("v ")]
    ans = {"outcome": outcome, "certified": False, "assignment": "",
           "output": f"kissat: {outcome} after {took:.0f} s" + ("" if outcome != "error" else
                                                                ": " + "\n".join(said)[-OUT:])}
    if outcome == "sat":
        value = model_of(out)
        wrong = check_model(cnf, value)
        ans["certified"] = wrong is None
        ans["output"] += "; the model satisfies every clause" if wrong is None else f"; but {wrong}"
        ans["assignment"] = " ".join(str(v if value.get(v, False) else -v) for v in range(1, n + 1))
    elif outcome == "unsat" and checker:
        t1 = time.time()
        code, text = integrator.run_cmd(f'"{checker}" "{cnf}" "{proof}" -t {seconds}', work, seconds + 120)
        verified = code is not None and "s VERIFIED" in text
        ans["certified"] = verified
        ans["output"] += (f"; drat-trim verified its proof in {time.time() - t1:.0f} s" if verified else
                          "; drat-trim did not verify its proof: "
                          + ("out of time" if code is None or "TIMEOUT" in text else text.strip()[-600:]))
    elif outcome == "unsat":
        ans["output"] += "; no proof checker here, so the refutation is kissat's word only"
    return ans


def serve(c: Ctx, now: float, most: int = SERVE_MAX, log=print) -> int:
    """Solve what was asked and nobody answered, oldest first: how many answered."""
    if not tool("kissat"):
        log("legion: no kissat here (put it on the PATH, or in LEGION_KISSAT): no SAT solve answered")
        return 0
    done = 0
    for ev in c.st.sat_pending(now)[:most]:
        if not coordgit.need_commit(c.repo, ev["commit"], ev["branch"], 20):
            log(f"legion: the commit of SAT solve {ev['id'][:8]} ({ev['name']}) hasn't come yet: next time")
            continue
        base = integrator.common_dir(c.repo) / "legion-review"
        folder = integrator.checkout(c.repo, ev["commit"], base / "sat")
        work = base / "sat-work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            cnf, why = plain_cnf(folder, ev["path"], work)
            ans = solve(cnf, work, ev["seconds"], ev.get("vars", 0)) if cnf else \
                {"outcome": "error", "certified": False, "assignment": "", "output": why}
        finally:
            shutil.rmtree(work, ignore_errors=True)  # a proof can take gigabytes
        c.emit("sat.checked", request=ev["id"], **ans)
        c.sync()
        log(f"legion: solved {ev['path']} for {ev['name']}: {ans['outcome']}"
            + (" (certified)" if ans["certified"] else ""))
        done += 1
    return done


def cmd_sat(args):
    c = Ctx()
    now = time.time()
    if args.serve:
        if not c.st.trusted(c.me):
            raise Fail("only a trusted agent solves for others", 3)
        n = serve(c, now, args.max)
        print(f"{n} SAT solve(s) answered; {len(c.st.sat_pending(time.time()))} waiting.")
        return 0
    if args.file:
        if not c.st.trusted(c.me):
            raise Fail("a trusted agent asks for a SAT solve: one that has done some of the work here", 3)
        ev = request(c, args.file, args.seconds, args.vars)
        print(f"Asked: SAT solve {ev['id'][:8]} of {ev['path']}, {ev['seconds']} s. A node with kissat solves it on "
              "its next pass (the project's verifier, one problem every 20 minutes or so), and the answer comes to "
              "you, certified or not (`python -m legion inbox`; `python -m legion sat` lists your requests). "
              "Go on meanwhile.")
        return 0
    mine = [r for r in c.st.sat.values() if r["ev"]["author"] == c.me][-10:]
    if not mine:
        print("You haven't asked for a SAT solve. `python -m legion sat FILE` asks for one.")
        return 0
    for r in mine:
        ev, ch = r["ev"], r["checked"]
        state = "waiting" if ch is None else ch["outcome"] + (" (certified)" if ch["certified"] else "")
        print(f"[{ev['id'][:8]}] {ev['path']}: {state}" + (f"\n    {' '.join(ch['output'].split())[-400:]}" if ch else "")
              + (f"\n    model: {ch['assignment'][:300]}" if ch and ch.get("assignment") else ""))
    return 0


def add_parser(sub) -> None:
    s = sub.add_parser("sat", help="solve a SAT problem on a node with kissat and a proof checker, when you have none")
    s.add_argument("file", nargs="?", help="the problem: a CNF (.cnf, .cnf.gz, .cnf.xz) or a .py that prints one")
    s.add_argument("--seconds", type=int, default=SECONDS, help=f"solving time (60 to {S.SAT_MAX_SECONDS})")
    s.add_argument("--vars", type=int, default=0,
                   help=f"if satisfiable, send back the values of variables 1..K (at most {S.SAT_MAX_VARS})")
    s.add_argument("--serve", action="store_true", help="solve what others asked for (needs kissat here)")
    s.add_argument("--max", type=int, default=SERVE_MAX, help="--serve: how many at most")
    s.set_defaults(func=cmd_sat)
