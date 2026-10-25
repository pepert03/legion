import unittest

from mdlite.nodes import BlockQuote, Document, Heading, Paragraph
from mdlite.toc import add_ids, slugify, toc_html


def plain(text):
    return text


class TestSlugify(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(slugify("Hello, *World*!"), "hello-world")
        self.assertEqual(slugify("Use `to_html()` now"), "use-tohtml-now")
        self.assertEqual(slugify("Ünïcode Títle 2"), "ünïcode-títle-2")
        self.assertEqual(slugify("a - b"), "a---b")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "section")


class TestIds(unittest.TestCase):
    def test_top_level_headings_get_unique_ids(self):
        quoted = Heading(2, "Inside")
        doc = Document([Heading(1, "Intro"), Paragraph("x"), Heading(2, "Intro"), BlockQuote([quoted]),
                        Heading(2, "Intro"), Heading(3, "Intro 1")])
        entries = add_ids(doc)
        self.assertEqual(entries, [(1, "Intro", "intro"), (2, "Intro", "intro-1"), (2, "Intro", "intro-2"),
                                   (3, "Intro 1", "intro-1-1")])
        self.assertEqual([b.id for b in doc.children if isinstance(b, Heading)],
                         ["intro", "intro-1", "intro-2", "intro-1-1"])
        self.assertIsNone(quoted.id)

    def test_suffix_skips_taken_ids(self):
        doc = Document([Heading(1, "a-1"), Heading(1, "a"), Heading(1, "a")])
        self.assertEqual([e[2] for e in add_ids(doc)], ["a-1", "a", "a-2"])


class TestTocHtml(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(toc_html([], inline=plain), "")

    def test_flat(self):
        self.assertEqual(toc_html([(2, "A", "a"), (2, "B", "b")], inline=plain),
                         '<ul>\n<li><a href="#a">A</a></li>\n<li><a href="#b">B</a></li>\n</ul>')

    def test_nested(self):
        entries = [(1, "A", "a"), (2, "B", "b"), (3, "C", "c"), (2, "D", "d"), (1, "E", "e")]
        self.assertEqual(toc_html(entries, inline=plain), "\n".join([
            "<ul>",
            '<li><a href="#a">A</a>',
            "<ul>",
            '<li><a href="#b">B</a>',
            "<ul>",
            '<li><a href="#c">C</a></li>',
            "</ul>",
            "</li>",
            '<li><a href="#d">D</a></li>',
            "</ul>",
            "</li>",
            '<li><a href="#e">E</a></li>',
            "</ul>",
        ]))

    def test_jumps_nest_one_level(self):
        self.assertEqual(toc_html([(1, "A", "a"), (4, "B", "b"), (2, "C", "c")], inline=plain), "\n".join([
            "<ul>", '<li><a href="#a">A</a>', "<ul>", '<li><a href="#b">B</a></li>', '<li><a href="#c">C</a></li>',
            "</ul>", "</li>", "</ul>"]))

    def test_starts_deep(self):
        self.assertEqual(toc_html([(3, "A", "a"), (1, "B", "b")], inline=plain),
                         '<ul>\n<li><a href="#a">A</a></li>\n<li><a href="#b">B</a></li>\n</ul>')
