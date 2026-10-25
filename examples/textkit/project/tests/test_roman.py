import unittest

from textkit.roman import from_roman, to_roman


class TestRoman(unittest.TestCase):
    def test_known(self):
        cases = {1: "I", 4: "IV", 9: "IX", 14: "XIV", 40: "XL", 90: "XC", 400: "CD", 1994: "MCMXCIV", 3999: "MMMCMXCIX"}
        for n, s in cases.items():
            self.assertEqual(to_roman(n), s)
            self.assertEqual(from_roman(s), n)

    def test_roundtrip(self):
        for n in range(1, 4000):
            self.assertEqual(from_roman(to_roman(n)), n)

    def test_range(self):
        with self.assertRaises(ValueError):
            to_roman(0)
        with self.assertRaises(ValueError):
            to_roman(4000)
