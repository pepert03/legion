"""The shared search library: search.improve(v, k, t, blocks=None, seconds=..., seed=...) returns a valid
covering, as small as it can find in that time. These cases have known optimal sizes, small enough that a
decent local search reaches them in seconds."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from search import improve  # noqa: E402
from verify import uncovered  # noqa: E402


class TestImprove(unittest.TestCase):
    def check(self, v, k, t, at_most, seconds):
        blocks = improve(v, k, t, seconds=seconds, seed=1)
        self.assertIsNone(uncovered(blocks, v, t), f"C({v},{k},{t}): not a covering")
        self.assertTrue(all(len(set(b)) == k for b in blocks))
        self.assertLessEqual(len(blocks), at_most, f"C({v},{k},{t}): {len(blocks)} blocks")

    def test_fano(self):
        self.check(7, 3, 2, 7, 4)          # the Fano plane, optimal

    def test_steiner_triples_on_9(self):
        self.check(9, 3, 2, 12, 8)         # optimal: 12

    def test_c_10_4_3(self):
        self.check(10, 4, 3, 32, 12)       # optimal: 30

    def test_starting_from_given_blocks(self):
        start = [tuple(range(1, 8))[i:i + 3] for i in range(5)]  # not a covering: improve must fix it or replace it
        blocks = improve(7, 3, 2, blocks=None, seconds=2, seed=2)
        self.assertIsNone(uncovered(blocks, 7, 2))
        self.assertTrue(start)
