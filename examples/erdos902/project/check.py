"""Is a task's theorem proved? Every task's acceptance command.

    python check.py THEOREM          e.g. python check.py lower_bound
    python check.py --module MODULE  e.g. python check.py --module Erdos902.LowerBound.Induction
    python check.py --tournament K FILE   e.g. python check.py --tournament 4 solutions/S4.txt
    python check.py --setup          once per clone: Mathlib, shared by every clone on this computer

THEOREM mode: builds the theorem's module and checks, with Lean itself, that `Erdos902.THEOREM` has exactly
the type of its proposition in Erdos902/Statements.lean, and that it rests on no `sorry` and no axiom beyond
Lean's standard three (propext, Classical.choice, Quot.sound). MODULE mode (for the lemmas of a sketch):
every declaration the module defines rests on nothing more than those three.

TOURNAMENT mode (for the search tasks, no Lean): FILE is a tournament, its number of vertices m on the first
line and then m lines of m characters, the j-th character of line i being 1 when i beats j (0 on the
diagonal). It checks that it's a tournament with property S_K, every K vertices beaten by one other vertex,
and prints its number of vertices last, which is the search tasks' metric.

Exits with 0 when everything holds; otherwise prints why and exits with 1.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# theorem -> (its module, its proposition in Statements). Nobody changes this table.
TASKS = {
    "lower_bound": ("Erdos902.LowerBound", "LowerBound"),
    "f_two": ("Erdos902.FTwo", "FTwo"),
    "f_three_upper": ("Erdos902.FThreeUpper", "FThreeUpper"),
    "f_three": ("Erdos902.FThree", "FThree"),
    "upper_bound": ("Erdos902.UpperBound", "UpperBound"),
    "szekeres_lower": ("Erdos902.SzekeresLower", "SzekeresLower"),
    # The open part (see the end of Statements.lean).
    "f_four_upper": ("Erdos902.FFourUpper", "FFourUpper"),
    "lower_constant": ("Erdos902.LowerConstant", "LowerConstant"),
    "upper_constant": ("Erdos902.UpperConstant", "UpperConstant"),
    "order_upper": ("Erdos902.OrderUpper", "OrderUpper"),
    "order_lower": ("Erdos902.OrderLower", "OrderLower"),
}
STANDARD = {"propext", "Classical.choice", "Quot.sound"}


SHARED = Path(os.environ.get("LEGION_LEAN_PACKAGES", Path.home() / ".legion" / "lean-packages" / "erdos902"))


_packages_ready = False


def ensure_packages():
    """Mathlib, already built, for this copy of the project. The integrator checks patches in fresh copies,
    which have no .lake/: link it to a shared one instead of downloading gigabytes for every review."""
    global _packages_ready
    if _packages_ready:
        return
    _packages_ready = True
    packages = ROOT / ".lake" / "packages"
    if (packages / "mathlib").exists() and not built(packages) and built(SHARED):
        # A download left halfway (lake would build Mathlib from source, for hours): use the shared one instead.
        packages.rename(packages.with_name(f"packages.partial-{os.getpid()}"))
    if built(packages):
        if not SHARED.exists():  # the first copy on this machine that has it shares it with the rest
            SHARED.parent.mkdir(parents=True, exist_ok=True)
            link(SHARED, packages)
        return
    if (SHARED / "mathlib").exists():
        packages.parent.mkdir(parents=True, exist_ok=True)
        link(packages, SHARED)
        if (packages / "mathlib").exists():
            return
    print("Downloading Mathlib, already built (the first time on this machine: a few GB)...", flush=True)
    lake("exe", "cache", "get")


def built(packages):
    return (packages / "mathlib" / ".lake" / "build" / "lib").is_dir()


def link(path, target):
    if os.name == "nt":  # a junction: no administrator needed, unlike a symlink
        subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)], capture_output=True)
    else:
        path.symlink_to(target, target_is_directory=True)


def lake(*args, timeout=3600):
    exe = shutil.which("lake") or str(Path.home() / ".elan" / "bin" / "lake")
    return subprocess.run([exe, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def lean_file(text):
    ensure_packages()
    d = ROOT / ".lake" / "legion-check"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "Check.lean"
    path.write_text(text, encoding="utf-8")
    p = lake("env", "lean", str(path))
    return p.returncode, p.stdout + p.stderr


def build(module):
    ensure_packages()  # sketches' tests call this directly: without it, lake builds Mathlib from source
    p = lake("build", module)
    if p.returncode:
        out = (p.stdout + p.stderr).strip().splitlines()
        errors = [l for l in out if "error" in l.lower()][:15] or out[-15:]
        fail(f"{module} doesn't compile:\n" + "\n".join(errors))


def fail(msg):
    print(f"NOT PROVED: {msg}")
    raise SystemExit(1)


def check_axioms(output, what):
    used = set()
    for m in re.finditer(r"depends on axioms: \[([^\]]*)\]", output):
        used |= {a.strip() for a in m.group(1).split(",") if a.strip()}
    if "sorryAx" in used or "declaration uses 'sorry'" in output:
        fail(f"{what} still relies on sorry")
    extra = used - STANDARD
    if extra:
        fail(f"{what} relies on axioms beyond the standard ones: {', '.join(sorted(extra))}")


def check_theorem(name):
    ensure_packages()
    if name not in TASKS:
        fail(f"unknown theorem {name}; the tasks are: {', '.join(TASKS)}")
    module, prop = TASKS[name]
    build(module)
    code, out = lean_file(f"""import {module}
import Erdos902.Statements
-- The theorem must have exactly the type of the fixed statement:
example : Erdos902.Statements.{prop} := Erdos902.{name}
#print axioms Erdos902.{name}
""")
    if code:
        fail(f"Erdos902.{name} doesn't have the type of Statements.{prop}, or doesn't exist:\n{out.strip()[-1500:]}")
    check_axioms(out, f"Erdos902.{name}")
    print(f"PROVED: Erdos902.{name} : Statements.{prop}, using only {', '.join(sorted(STANDARD))}")


def check_module(module):
    ensure_packages()
    if not re.fullmatch(r"Erdos902(\.[A-Za-z0-9_]+)+", module):
        fail("a module is like Erdos902.LowerBound.Induction")
    build(module)
    code, out = lean_file(f"""import {module}
open Lean Elab Command in
#eval show CommandElabM Unit from do
  let env ← getEnv
  let some idx := env.getModuleIdx? `{module} | throwError "module {module} isn't loaded"
  let names := env.header.moduleData[idx.toNat]!.constNames
  for n in names do
    let axs ← liftCoreM (Lean.collectAxioms n)
    unless axs.isEmpty do
      logInfo m!"'{{n}}' depends on axioms: {{axs.toList}}"
  logInfo m!"checked {{names.size}} declarations"
""")
    if code:
        fail(f"couldn't inspect {module}:\n{out.strip()[-1500:]}")
    check_axioms(out.replace("'", ""), module)
    count = re.search(r"checked (\d+) declarations", out)
    print(f"PROVED: every declaration in {module} ({count.group(1) if count else '?'}) uses only standard axioms")


def read_tournament(path):
    lines = [l.strip() for l in Path(path).read_text(encoding="utf-8").split() if l.strip()]
    if not lines or not lines[0].isdigit():
        fail(f"{path}: the first line must be the number of vertices")
    m, rows = int(lines[0]), lines[1:]
    if len(rows) != m or any(len(r) != m or set(r) - {"0", "1"} for r in rows):
        fail(f"{path}: expected {m} lines of {m} characters 0 or 1")
    for i in range(m):
        if rows[i][i] != "0":
            fail(f"vertex {i} beats itself")
        for j in range(i + 1, m):
            if rows[i][j] == rows[j][i]:
                fail(f"vertices {i} and {j}: exactly one of them must beat the other")
    return m, rows


def check_tournament(k, path):
    m, rows = read_tournament(path)
    if m < k + 1:
        fail(f"with {m} vertices no set of {k} can be dominated by another vertex")
    # beaten_by[v]: the vertices that beat v, as a bit mask. A set is dominated when their masks share a bit.
    beaten_by = [sum(1 << i for i in range(m) if rows[i][v] == "1") for v in range(m)]
    full = (1 << m) - 1

    def extend(start, mask, chosen):
        if len(chosen) == k:
            return
        for v in range(start, m - (k - len(chosen) - 1)):
            inter = mask & beaten_by[v]
            if not inter:
                fail(f"no vertex beats all of {sorted(chosen + [v])}: the tournament doesn't have property S_{k}")
            extend(v + 1, inter, chosen + [v])

    extend(0, full, [])
    print(f"PROVED: a tournament with property S_{k} on {m} vertices")
    print(m)


def setup():
    ensure_packages()
    packages = ROOT / ".lake" / "packages"
    if not built(packages):
        fail("Mathlib isn't ready: `lake exe cache get` failed (no network?); run `python check.py --setup` again")
    shared = "shared with the other clones on this computer" if packages.resolve() != packages.absolute() else "here"
    print(f"READY: Mathlib, already built, {shared} ({packages.resolve()})")


def main(argv):
    if argv == ["--setup"]:
        setup()
    elif len(argv) == 3 and argv[0] == "--tournament" and argv[1].isdigit():
        check_tournament(int(argv[1]), argv[2])
    elif len(argv) == 2 and argv[0] == "--module":
        check_module(argv[1])
    elif len(argv) == 1 and not argv[0].startswith("-"):
        check_theorem(argv[0])
    else:
        print(__doc__.strip().splitlines()[0])
        print("usage: python check.py THEOREM | --module MODULE | --tournament K FILE | --setup")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
