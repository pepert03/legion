import unittest

from textkit.slug import slugify


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_accents_and_spaces(self):
        self.assertEqual(slugify("  Crème  brûlée  "), "creme-brulee")

    def test_symbols(self):
        self.assertEqual(slugify("C++ & Python!"), "c-python")

    def test_empty(self):
        self.assertEqual(slugify(""), "")
