# Erdős Problem 902: Schütte tournaments, in Lean

A **tournament** is a contest where every pair of players meets once and one of them wins. It has
**property Sₙ** if, for any n players, someone else beat all of them. Schütte asked how small such a
tournament can be: **f(n)** is the least number of players. Erdős (1963) showed

$$2^{n+1} - 1 \le f(n) \ll n^2 2^n,$$

and that f(1) = 3 and f(2) = 7; Szekeres and Szekeres (1965) found f(3) = 19 and n 2ⁿ ≪ f(n). The exact
growth of f(n) is still open ([erdosproblems.com/902](https://www.erdosproblems.com/902)).

These results are known on paper, but most have **no formal proof**. Google DeepMind's
[formal-conjectures](https://github.com/google-deepmind/formal-conjectures) states them in Lean with
`sorry` where the proofs should be. This project writes those proofs.

| Task | Statement | Status on paper |
|---|---|---|
| lower-bound | 2ⁿ⁺¹ − 1 ≤ f(n) | Erdős 1963, short |
| f-two | f(2) = 7 | follows from the lower bound |
| f-three-upper | f(3) ≤ 19 | a 19-player tournament |
| f-three | f(3) = 19 | Szekeres and Szekeres 1965 |
| upper-bound | f(n) ≤ (log 2 + ε) n² 2ⁿ | Erdős 1963, probabilistic |
| szekeres-lower | n 2ⁿ ≪ f(n) | Szekeres and Szekeres 1965 |

```
Erdos902/Basic.lean        the definitions, copied from formal-conjectures (fixed)
Erdos902/Statements.lean   what each task must prove (fixed)
Erdos902/<Task>.lean       one file per task: replace the sorry
check.py                   python check.py lower_bound   -> PROVED, or why not
```

A proof counts when `check.py` says so: it compiles, has exactly the type in `Statements.lean`, and uses
no `sorry` and no axiom beyond Lean's standard three.

## Setup

Lean 4 (v4.33.1, set in `lean-toolchain`) and Mathlib. Once per machine:

```sh
# Windows (PowerShell): install elan, Lean's version manager
curl.exe -O --location https://raw.githubusercontent.com/leanprover/elan/master/elan-init.ps1
powershell -ExecutionPolicy Bypass -f elan-init.ps1 -NoPrompt 1 -DefaultToolchain none
# Mac or Linux
curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
```

Then, in this folder: `lake exe cache get` (downloads Mathlib already built, a few GB) and `lake build`.
