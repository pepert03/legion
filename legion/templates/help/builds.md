# Builds that don't make everyone wait

Every check rebuilds what a patch changed and everything that depends on it, on every verifier's
machine. Write so that's little:
- **Small files.** A change rebuilds its whole file and every file that imports it. Put each lemma
  or small group of lemmas in a file of its own, and when you split a task (`sketch`, `path`), give
  each subtask its own file, so its zone is that file.
- **Cheap proofs.** In a Lean project, tactics that search (`nlinarith` and `polyrith` with many
  hypotheses, `simp` without a lemma list, `decide` over many cases, deep `aesop`) can take minutes
  to elaborate each time anyone builds the file. Once a proof works, make it explicit: `simp only
  [...]` with the lemmas `simp?` reports, `linarith` with the facts it needs, the terms `exact?`
  finds. Raise `maxHeartbeats` only locally (`set_option maxHeartbeats N in`), and say why.
