"""Check a covering design: python verify.py V K T FILE

A C(v,k,t) covering design is a family of k-element subsets ("blocks") of {1..v} such that every t-element
subset of {1..v} lies inside at least one block. The fewer blocks, the better.

FILE has one block per line: k distinct integers between 1 and v, separated by spaces or commas. Blank
lines and lines starting with # are ignored.

Prints `blocks N` and exits with 0 if FILE is a valid covering; otherwise says what's wrong (a malformed
line, or a t-subset no block covers) and exits with 1.
"""

import sys
from itertools import combinations
from math import comb


def read_blocks(path, v, k):
    blocks = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                block = [int(x) for x in line.replace(",", " ").split()]
            except ValueError:
                raise ValueError(f"line {n}: not a list of integers: {line[:60]}")
            if len(block) != k or len(set(block)) != k:
                raise ValueError(f"line {n}: a block needs {k} distinct points, got {block}")
            if not all(1 <= x <= v for x in block):
                raise ValueError(f"line {n}: points must be between 1 and {v}: {block}")
            blocks.append(tuple(sorted(block)))
    return blocks


def rank(subset, v):
    """The position of a sorted subset among all subsets of its size, in colexicographic order."""
    return sum(comb(x - 1, i + 1) for i, x in enumerate(subset))


def uncovered(blocks, v, t):
    """A t-subset that no block contains, or None if the blocks cover them all."""
    covered = bytearray(comb(v, t))
    for block in blocks:
        for s in combinations(block, t):
            covered[rank(s, v)] = 1
    if all(covered):
        return None
    for s in combinations(range(1, v + 1), t):
        if not covered[rank(s, v)]:
            return s


def main(argv):
    if len(argv) != 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    v, k, t, path = int(argv[0]), int(argv[1]), int(argv[2]), argv[3]
    if not 1 <= t <= k <= v:
        print("need 1 <= t <= k <= v", file=sys.stderr)
        return 2
    try:
        blocks = read_blocks(path, v, k)
    except (OSError, ValueError) as e:
        print(f"invalid: {e}", file=sys.stderr)
        return 1
    missing = uncovered(blocks, v, t)
    if missing:
        print(f"invalid: no block contains {set(missing)}", file=sys.stderr)
        return 1
    print(f"blocks {len(blocks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
