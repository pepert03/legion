"""Lean for agents whose machine has none: a file compiled by a node that has it.

An agent without Lean could write a proof but never compile it: lines that needed Lean (`--needs lake`) went
to nobody, and a patch sent to find out cost a rejection and its author's trust. In Erdős #902 every Instinct agent
lacked Lean, and the line formalising f(4) >= 51 waited half a day for an agent that had it.

    legion lean FILE          ask: the file goes as a commit (main plus the file), named by a lean.requested
    legion lean               your requests and their outcomes
    legion lean --serve       compile what was asked (a trusted agent with Lean: the project's verifier does)

The outcome comes back as a lean.checked: whether it compiles and the compiler's last lines, in the asker's
inbox. Compiling runs the file's code (Lean can run IO at compile time) on the compiling node: only trusted agents
ask, and the node runs it as it runs the patches it verifies.
"""

import re
import sys
import time
from pathlib import Path

from . import coordgit
from . import integrator
from . import state as S
from .ctx import Ctx, Fail

MAX_FILE = 300_000  # bytes of a file to compile
TIMEOUT = 1800  # seconds a compile may take, the build of what it imports included
SERVE_MAX = 6  # requests answered by one `lean --serve`
OUT = 1800  # characters of the compiler's output sent back

# Run in a folder holding the request's commit: build the project's modules the file imports, then compile it.
# With the project's own check.py (Mathlib from the machine's shared copy, see its ensure_packages) when there is
# one, with lake alone when not.
RUNNER = r'''
import pathlib, re, subprocess, shutil, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
mods = [m for line in text.splitlines() for m in (re.match(r"\s*import\s+(.+)", line).group(1).split()
                                                  if re.match(r"\s*import\s+(.+)", line) else [])]
local = [m for m in mods if pathlib.Path(*m.split(".")).with_suffix(".lean").is_file()]
if pathlib.Path("check.py").is_file():
    sys.path.insert(0, ".")
    import check
    for m in local:
        check.build(m)
    code, out = check.lean_file(text)
    print(out)
    sys.exit(code)
lake = shutil.which("lake") or str(pathlib.Path.home() / ".elan" / "bin" / "lake")
for m in local:
    p = subprocess.run([lake, "build", m], capture_output=True, text=True)
    if p.returncode:
        print((p.stdout + p.stderr)[-3000:])
        sys.exit(1)
p = subprocess.run([lake, "env", "lean", str(path)], capture_output=True, text=True)
print(p.stdout + p.stderr)
sys.exit(p.returncode)
'''


def request(c: Ctx, file: str) -> dict:
    """Ask for `file` (a path in this clone) to be compiled: a commit of main plus the file goes with the event."""
    from .cli import result_commit  # the commit a result goes as: main plus its files
    path = Path(file)
    try:
        rel = path.resolve().relative_to(Path(c.repo).resolve()).as_posix()
    except ValueError:
        raise Fail(f"{file} isn't in this clone: put it in the folder of the task it's for", 2) from None
    if not rel.endswith(".lean") or not path.is_file():
        raise Fail(f"{file}: a .lean file of this clone", 2)
    if path.stat().st_size > MAX_FILE:
        raise Fail(f"{file} has over {MAX_FILE // 1000} kB: compile its parts one at a time", 2)
    coordgit.refresh_base(c.repo)
    commit = result_commit(c.repo, coordgit.base_ref(c.repo), [rel], f"Lean compile of {rel}", c.ident)
    n = sum(r["ev"]["author"] == c.me for r in c.st.lean.values()) + 1
    branch = coordgit.patch_branch(c.repo, "lean", c.ident.short, n)
    coordgit.publish_patch(c.repo, branch, commit)
    ev = c.emit("lean.requested", path=rel, commit=commit, branch=branch)
    c.sync()
    return ev


def serve(c: Ctx, now: float, most: int = SERVE_MAX, log=print) -> int:
    """Compile what was asked and nobody answered, oldest first: how many answered."""
    done = 0
    for ev in c.st.lean_pending(now)[:most]:
        if not coordgit.need_commit(c.repo, ev["commit"], ev["branch"], 20):
            log(f"legion: the commit of Lean compile {ev['id'][:8]} ({ev['name']}) hasn't come yet: next time")
            continue
        folder = integrator.checkout(c.repo, ev["commit"], integrator.common_dir(c.repo) / "legion-review" / "lean")
        script = folder.parent / "lean-runner.py"
        script.write_text(RUNNER, encoding="utf-8")
        cmd = f'python "{script}" "{ev["path"]}"'
        code, out = integrator.run_cmd(cmd, folder, TIMEOUT)
        ok = code == 0
        text = out.strip()
        if code is None:
            text = f"timed out after {TIMEOUT} s"
        text = text[-OUT:] if text else ("no output: it compiles" if ok else "it failed with no output")
        c.emit("lean.checked", request=ev["id"], ok=ok, output=text)
        c.sync()
        log(f"legion: compiled {ev['path']} for {ev['name']}: {'it compiles' if ok else 'it fails'}")
        done += 1
    return done


def cmd_lean(args):
    c = Ctx()
    now = time.time()
    if args.serve:
        if not c.st.trusted(c.me):
            raise Fail("only a trusted agent compiles for others", 3)
        n = serve(c, now, args.max)
        print(f"{n} Lean compile(s) answered; {len(c.st.lean_pending(time.time()))} waiting.")
        return 0
    if args.file:
        if not c.st.trusted(c.me):
            raise Fail("a trusted agent asks for a Lean compile: one that has done some of the work here", 3)
        ev = request(c, args.file)
        print(f"Asked: Lean compile {ev['id'][:8]} of {ev['path']}. A node with Lean compiles it on its next pass "
              "(the project's verifier, every 20 minutes or so), and what the compiler says comes to you "
              "(`python -m legion inbox`; `python -m legion lean` lists your requests). Go on meanwhile.")
        return 0
    mine = [r for r in c.st.lean.values() if r["ev"]["author"] == c.me][-10:]
    if not mine:
        print("You haven't asked for a Lean compile. `python -m legion lean FILE` asks for one.")
        return 0
    for r in mine:
        ev, ch = r["ev"], r["checked"]
        state = "waiting" if ch is None else ("compiles" if ch["ok"] else "fails")
        print(f"[{ev['id'][:8]}] {ev['path']}: {state}" + (f"\n    {' '.join(ch['output'].split())[-400:]}" if ch else ""))
    return 0


def add_parser(sub) -> None:
    s = sub.add_parser("lean", help="compile a Lean file on a node that has Lean, when your machine has none")
    s.add_argument("file", nargs="?", help="the .lean file to compile (in this clone)")
    s.add_argument("--serve", action="store_true", help="compile what others asked for (needs Lean here)")
    s.add_argument("--max", type=int, default=SERVE_MAX, help="--serve: how many at most")
    s.set_defaults(func=cmd_lean)
