import unittest

from textkit.wrap import wrap


class TestWrap(unittest.TestCase):
    def test_greedy(self):
        self.assertEqual(wrap("the quick brown fox jumps", 10), ["the quick", "brown fox", "jumps"])

    def test_exact_width(self):
        self.assertEqual(wrap("abc de", 6), ["abc de"])

    def test_long_word(self):
        self.assertEqual(wrap("extraordinarily short", 5), ["extraordinarily", "short"])

    def test_empty(self):
        self.assertEqual(wrap("", 5), [])

    def test_bad_width(self):
        with self.assertRaises(ValueError):
            wrap("hello", 0)
