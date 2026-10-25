import unittest

from mdlite.html import escape


class TestEscape(unittest.TestCase):
    def test_special_characters(self):
        self.assertEqual(escape('<a href="x">Tom & Jerry</a>'),
                         "&lt;a href=&quot;x&quot;&gt;Tom &amp; Jerry&lt;/a&gt;")

    def test_ampersand_first(self):
        self.assertEqual(escape("&lt;"), "&amp;lt;")

    def test_plain_text_unchanged(self):
        self.assertEqual(escape("héllo 'world'"), "héllo 'world'")
        self.assertEqual(escape(""), "")
