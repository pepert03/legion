/-
What each task must prove, as propositions. These are the statements of
google-deepmind/formal-conjectures' ErdosProblems/902.lean (commit 591675d), word for word.

Nobody changes this file: `check.py` checks each task's theorem against the proposition here.
-/
import Erdos902.Basic
import Mathlib.Analysis.SpecialFunctions.Log.Basic
import Mathlib.Order.Filter.AtTopBot.Basic

namespace Erdos902.Statements

open Filter

/-- Erdős [Er63c]: $2^{n+1} - 1 \le f(n)$. (`erdos_902.variants.erdos_lower`) -/
def LowerBound : Prop := ∀ n : ℕ, 2 ^ (n + 1) - 1 ≤ f n

/-- "It is easy to check that $f(2) = 7$." (`erdos_902.variants.f_two`) -/
def FTwo : Prop := f 2 = 7

/-- Half of Szekeres and Szekeres' $f(3) = 19$: a tournament on 19 vertices with property $S_3$. -/
def FThreeUpper : Prop := f 3 ≤ 19

/-- Szekeres and Szekeres [SzSz65]: $f(3) = 19$. (`erdos_902.variants.szekeres_f_three`) -/
def FThree : Prop := f 3 = 19

/-- Erdős [Er63c]: $\limsup_n f(n) 2^{-n} n^{-2} \le \log 2$. (`erdos_902.variants.erdos_upper`) -/
def UpperBound : Prop :=
  ∀ ε > (0 : ℝ), ∀ᶠ n : ℕ in atTop, (f n : ℝ) ≤ (Real.log 2 + ε) * n ^ 2 * 2 ^ n

/-- Szekeres and Szekeres [SzSz65]: $n 2^n \ll f(n)$. (`erdos_902.variants.szekeres_lower`) -/
def SzekeresLower : Prop :=
  ∃ c > (0 : ℝ), ∀ᶠ n : ℕ in atTop, c * n * 2 ^ n ≤ (f n : ℝ)

end Erdos902.Statements
