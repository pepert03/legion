"""Is a task's theorem proved? Every task's acceptance command.

    python check.py THEOREM          e.g. python check.py lower_bound
    python check.py --module MODULE  e.g. python check.py --module Erdos902.LowerBound.Induction

THEOREM mode: builds the theorem's module and checks, with Lean itself, that `Erdos902.THEOREM` has exactly
the type of its proposition in Erdos902/Statements.lean, and that it rests on no `sorry` and no axiom beyond
Lean's standard three (propext, Classical.choice, Quot.sound). MODULE mode (for the lemmas of a sketch):
every declaration the module defines rests on nothing more than those three.

Exits with 0 when everything holds; otherwise prints why and exits with 1.
"""

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
}
STANDARD = {"propext", "Classical.choice", "Quot.sound"}


def lake(*args, timeout=3600):
    exe = shutil.which("lake") or str(Path.home() / ".elan" / "bin" / "lake")
    return subprocess.run([exe, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def lean_file(text):
    d = ROOT / ".lake" / "legion-check"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "Check.lean"
    path.write_text(text, encoding="utf-8")
    p = lake("env", "lean", str(path))
    return p.returncode, p.stdout + p.stderr


def build(module):
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


def main(argv):
    if len(argv) == 2 and argv[0] == "--module":
        check_module(argv[1])
    elif len(argv) == 1 and not argv[0].startswith("-"):
        check_theorem(argv[0])
    else:
        print(__doc__.strip().splitlines()[0])
        print("usage: python check.py THEOREM | python check.py --module MODULE")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
