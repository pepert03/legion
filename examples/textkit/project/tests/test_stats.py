import unittest

from textkit.stats import top_words, word_freq


class TestStats(unittest.TestCase):
    def test_freq(self):
        self.assertEqual(word_freq("Hello hello, world"), {"hello": 2, "world": 1})

    def test_top_ties_alphabetical(self):
        self.assertEqual(top_words("b a c a b d", 3), [("a", 2), ("b", 2), ("c", 1)])

    def test_top_more_than_available(self):
        self.assertEqual(top_words("x", 5), [("x", 1)])
