# covers: beating covering-design records

A **C(v,k,t) covering design** is a family of k-element subsets ("blocks") of {1..v} such that every
t-element subset lies inside at least one block. The problem is to use as few blocks as possible. Exact
answers are known only for small cases; for the rest there's a table of records, improved over decades
by people running clever searches, and a gap between the best covering anyone has found and the best
lower bound anyone has proved.

This project attacks a few of those records. Each case is a **competitive task**: a patch counts only if
`verify.py` accepts the covering and it has fewer blocks than the best one so far.

```sh
python verify.py 14 5 3 solutions/C14-5-3.txt     # prints "blocks N", or what's wrong
```

| Case | Record (blocks) | Lower bound |
|---|---|---|
| C(14,5,3) | 43 | 37 |
| C(15,6,4) | 117 | 93 |
| C(18,6,4) | 236 | 205 |
| C(20,5,3) | 133 | 124 |
| C(25,5,3) | 256 | 240 |
| C(27,7,3) | 104 | 93 |

Records and lower bounds are from the La Jolla Covering Repository's final snapshot (March 2026, Zenodo
10.5281/zenodo.10779736); the repository now lives at coveringrepository.com, where some may have been
improved since. A covering below the record would be a new result worth submitting there.

```
verify.py         the checker: every task's acceptance command
search/           a shared local-search library (its own task, with tests)
solutions/        one file per case, a block per line
work/<case>/      each case's search code
```

The direct route is local search (simulated annealing on the number of uncovered t-subsets), but when
a case stops improving, other routes are fair game: symmetric constructions (blocks invariant under a
cyclic or affine group), building on known designs for neighbouring cases, exact search on parts.
