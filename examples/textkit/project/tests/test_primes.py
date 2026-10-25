import unittest

from textkit.primes import count_primes


class TestPrimes(unittest.TestCase):
    def test_small(self):
        self.assertEqual([count_primes(n) for n in (0, 1, 2, 3, 10, 100)], [0, 0, 0, 1, 4, 25])

    def test_bigger(self):
        self.assertEqual(count_primes(10_000), 1229)
