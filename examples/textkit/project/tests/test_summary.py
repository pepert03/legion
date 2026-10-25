import unittest

from textkit.summary import summarize


class TestSummary(unittest.TestCase):
    def test_short_text(self):
        self.assertEqual(summarize("The dog and the cat. The dog sleeps."),
                         "Words: 8\nReading: 1 min\nFrequent: the (3), dog (2), and (1)")

    def test_reading_time_rounds_up(self):
        text = " ".join(["word"] * 401)
        self.assertEqual(summarize(text).splitlines()[1], "Reading: 3 min")

    def test_empty(self):
        self.assertEqual(summarize(""), "Words: 0\nReading: 0 min\nFrequent: -")
