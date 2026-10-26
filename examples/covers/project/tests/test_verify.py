import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from verify import rank, uncovered  # noqa: E402

FANO = [(1, 2, 3), (1, 4, 5), (1, 6, 7), (2, 4, 6), (2, 5, 7), (3, 4, 7), (3, 5, 6)]


def run(v, k, t, text):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
    try:
        return subprocess.run([sys.executable, os.path.join(ROOT, "verify.py"), str(v), str(k), str(t), f.name],
                              capture_output=True, text=True)
    finally:
        os.unlink(f.name)


class TestVerify(unittest.TestCase):
    def test_fano_plane_is_a_7_3_2_covering(self):
        p = run(7, 3, 2, "# the Fano plane\n" + "\n".join(" ".join(map(str, b)) for b in FANO) + "\n")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "blocks 7"))

    def test_commas_and_blank_lines(self):
        p = run(7, 3, 2, "\n\n".join(",".join(map(str, b)) for b in FANO))
        self.assertEqual(p.returncode, 0)

    def test_a_missing_pair_is_reported(self):
        p = run(7, 3, 2, "\n".join(" ".join(map(str, b)) for b in FANO[:-1]))
        self.assertEqual(p.returncode, 1)
        self.assertIn("no block contains", p.stderr)

    def test_malformed_blocks(self):
        for text in ("1 2", "1 1 2", "0 1 2", "1 2 8", "a b c"):
            self.assertEqual(run(7, 3, 2, text).returncode, 1, text)

    def test_rank_is_a_bijection(self):
        from itertools import combinations
        ranks = [rank(s, 9) for s in combinations(range(1, 10), 4)]
        self.assertEqual(sorted(ranks), list(range(126)))

    def test_uncovered(self):
        self.assertIsNone(uncovered(FANO, 7, 2))
        self.assertEqual(uncovered(FANO[1:], 7, 2), (1, 2))
