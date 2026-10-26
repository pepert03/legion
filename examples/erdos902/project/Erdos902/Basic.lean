/-
Definitions and known results for Erdős Problem 902, copied from google-deepmind/formal-conjectures
(FormalConjectures/ErdosProblems/902.lean and FormalConjecturesForMathlib/Combinatorics/Digraph/
Tournament.lean, commit 591675d), so this project only needs Mathlib.

Copyright 2026 The Formal Conjectures Authors. Licensed under the Apache License, Version 2.0:
https://www.apache.org/licenses/LICENSE-2.0

Nobody changes this file: every task's statement is built on it.
-/
import Mathlib.Combinatorics.Digraph.Basic
import Mathlib.Data.Fintype.Card
import Mathlib.Data.Real.Basic
import Mathlib.Tactic

namespace Erdos902

/-- A tournament: every pair of distinct vertices is joined by exactly one directed edge. -/
def IsTournament {V : Type*} (G : Digraph V) : Prop :=
  (∀ v, ¬ G.Adj v v) ∧ (∀ u v, u ≠ v → (G.Adj u v ↔ ¬ G.Adj v u))

/-- Schütte's property $S_n$: every set `E` of `n` vertices is dominated by a vertex outside it,
that is, some vertex `x ∉ E` has an edge to every vertex of `E`. -/
def HasPropertyS {V : Type*} (G : Digraph V) (n : ℕ) : Prop :=
  ∀ E : Finset V, E.card = n → ∃ x ∉ E, ∀ e ∈ E, G.Adj x e

/-- $f(n)$: the least number of vertices of a tournament with property $S_n$. -/
noncomputable def f (n : ℕ) : ℕ :=
  sInf {m : ℕ | n ≤ m ∧ ∃ G : Digraph (Fin m), IsTournament G ∧ HasPropertyS G n}

/-- Erdős's tournament on seven vertices, `a → a + 1, a + 2, a + 4` modulo 7, has property $S_2$. -/
theorem f_two_le : f 2 ≤ 7 := by
  refine Nat.sInf_le ⟨by norm_num, ⟨fun a b => b - a ∈ ({1, 2, 4} : Finset (Fin 7))⟩,
    ⟨by decide, by decide⟩, ?_⟩
  intro E hE
  obtain ⟨a, b, hab, rfl⟩ := Finset.card_eq_two.1 hE
  have key : ∀ a b : Fin 7, a ≠ b → ∃ x, x ≠ a ∧ x ≠ b ∧
      a - x ∈ ({1, 2, 4} : Finset (Fin 7)) ∧ b - x ∈ ({1, 2, 4} : Finset (Fin 7)) := by
    decide
  obtain ⟨x, hxa, hxb, ha, hb⟩ := key a b hab
  refine ⟨x, by simp [hxa, hxb], fun e he => ?_⟩
  rcases Finset.mem_insert.1 he with rfl | he
  · exact ha
  · rw [Finset.mem_singleton.1 he]
    exact hb

end Erdos902
