import unittest

from mdlite.nodes import (BlockQuote, CodeBlock, Document, Heading, List, ListItem, Paragraph, Table,
                          ThematicBreak)
from mdlite.render import render


def mark(text):
    """A stand-in for the inline formatter, so these tests only check the renderer."""
    return f"[{text}]"


def html(*blocks):
    return render(Document(list(blocks)), inline=mark)


class TestBlocks(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(html(), "")

    def test_heading_and_paragraph(self):
        self.assertEqual(html(Heading(2, "T"), Paragraph("p")), "<h2>[T]</h2>\n<p>[p]</p>\n")

    def test_heading_id(self):
        self.assertEqual(html(Heading(1, "T", "my-id")), '<h1 id="my-id">[T]</h1>\n')

    def test_code_is_escaped_not_formatted(self):
        self.assertEqual(html(CodeBlock("<b> & *x*\n")), "<pre><code>&lt;b&gt; &amp; *x*\n</code></pre>\n")
        self.assertEqual(html(CodeBlock("x\n", "py")), '<pre><code class="language-py">x\n</code></pre>\n')

    def test_thematic_break(self):
        self.assertEqual(html(ThematicBreak()), "<hr />\n")

    def test_quote(self):
        self.assertEqual(html(BlockQuote([Paragraph("a"), Paragraph("b")])),
                         "<blockquote>\n<p>[a]</p>\n<p>[b]</p>\n</blockquote>\n")
        self.assertEqual(html(BlockQuote([])), "<blockquote>\n</blockquote>\n")


class TestLists(unittest.TestCase):
    def test_tight(self):
        lst = List(False, [ListItem([Paragraph("a")]), ListItem([Paragraph("b")])])
        self.assertEqual(html(lst), "<ul>\n<li>[a]</li>\n<li>[b]</li>\n</ul>\n")

    def test_loose(self):
        lst = List(False, [ListItem([Paragraph("a")]), ListItem([Paragraph("b"), Paragraph("c")])], tight=False)
        self.assertEqual(html(lst), "<ul>\n<li><p>[a]</p></li>\n<li><p>[b]</p>\n<p>[c]</p></li>\n</ul>\n")

    def test_ordered_start(self):
        self.assertEqual(html(List(True, [ListItem([Paragraph("x")])], start=1)), "<ol>\n<li>[x]</li>\n</ol>\n")
        self.assertEqual(html(List(True, [ListItem([Paragraph("x")])], start=3)),
                         '<ol start="3">\n<li>[x]</li>\n</ol>\n')

    def test_nested(self):
        inner = List(False, [ListItem([Paragraph("b")])])
        lst = List(True, [ListItem([Paragraph("a"), inner])])
        self.assertEqual(html(lst), "<ol>\n<li>[a]\n<ul>\n<li>[b]</li>\n</ul></li>\n</ol>\n")

    def test_other_blocks_in_tight_items(self):
        lst = List(False, [ListItem([Paragraph("a"), CodeBlock("c\n")]), ListItem([])])
        self.assertEqual(html(lst), "<ul>\n<li>[a]\n<pre><code>c\n</code></pre></li>\n<li></li>\n</ul>\n")


class TestTables(unittest.TestCase):
    def test_table(self):
        t = Table(["A", "B"], [None, "center"], [["1", "2"]])
        self.assertEqual(html(t), "<table>\n<thead>\n<tr>\n<th>[A]</th>\n<th style=\"text-align: center\">[B]</th>\n"
                                  "</tr>\n</thead>\n<tbody>\n<tr>\n<td>[1]</td>\n"
                                  "<td style=\"text-align: center\">[2]</td>\n</tr>\n</tbody>\n</table>\n")

    def test_no_body(self):
        self.assertEqual(html(Table(["A"], ["right"], [])),
                         '<table>\n<thead>\n<tr>\n<th style="text-align: right">[A]</th>\n</tr>\n</thead>\n</table>\n')
