import unittest

from mdlite.blocks import parse_blocks
from mdlite.nodes import (BlockQuote, CodeBlock, Document, Heading, List, ListItem, Paragraph, Table,
                          ThematicBreak)


def blocks(text):
    return parse_blocks(text).children


class TestHeadings(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(blocks("# One\n###### Six"), [Heading(1, "One"), Heading(6, "Six")])

    def test_closing_hashes_and_spaces(self):
        self.assertEqual(blocks("##   Title  ##  "), [Heading(2, "Title")])
        self.assertEqual(blocks("# C#"), [Heading(1, "C#")])
        self.assertEqual(blocks("#"), [Heading(1, "")])

    def test_not_headings(self):
        self.assertEqual(blocks("#hashtag"), [Paragraph("#hashtag")])
        self.assertEqual(blocks("####### seven"), [Paragraph("####### seven")])


class TestParagraphsAndBreaks(unittest.TestCase):
    def test_paragraphs(self):
        # Leading spaces go; trailing ones stay (they can mean a line break) except on the last line.
        self.assertEqual(blocks("one  \n  two  \n\n\nthree   "), [Paragraph("one  \ntwo"), Paragraph("three")])

    def test_thematic_breaks(self):
        self.assertEqual(blocks("---\n* * *\n___"), [ThematicBreak()] * 3)
        self.assertEqual(blocks("text\n---\nmore"), [Paragraph("text"), ThematicBreak(), Paragraph("more")])
        self.assertEqual(blocks("--"), [Paragraph("--")])

    def test_empty_document(self):
        self.assertEqual(parse_blocks(""), Document([]))
        self.assertEqual(parse_blocks("\n  \n"), Document([]))

    def test_line_endings_and_tabs(self):
        self.assertEqual(blocks("a\r\nb\rc"), [Paragraph("a\nb\nc")])
        self.assertEqual(blocks("\tcode"), [CodeBlock("code\n")])


class TestCode(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(blocks("```python\nx = 1\n\n  y\n```"), [CodeBlock("x = 1\n\n  y\n", "python")])

    def test_fence_info_and_tildes(self):
        self.assertEqual(blocks("~~~ js extra\na\n~~~"), [CodeBlock("a\n", "js")])
        self.assertEqual(blocks("```\n```"), [CodeBlock("", None)])

    def test_closing_fence_rules(self):
        # The closing fence must be the same character and at least as long.
        self.assertEqual(blocks("````\n```\n~~~~\n````"), [CodeBlock("```\n~~~~\n")])
        self.assertEqual(blocks("```\nnever closed"), [CodeBlock("never closed\n")])

    def test_indented_fence(self):
        self.assertEqual(blocks("  ```\n  a\n    b\n c\n  ```"), [CodeBlock("a\n  b\nc\n")])

    def test_fence_interrupts_paragraph(self):
        self.assertEqual(blocks("para\n```\ncode\n```"), [Paragraph("para"), CodeBlock("code\n")])

    def test_indented(self):
        self.assertEqual(blocks("    a\n      b\n\n    c\n\n\nafter"),
                         [CodeBlock("a\n  b\n\nc\n"), Paragraph("after")])

    def test_indented_does_not_interrupt_paragraph(self):
        self.assertEqual(blocks("para\n    still para"), [Paragraph("para\nstill para")])

    def test_markdown_inside_code_is_kept(self):
        self.assertEqual(blocks("```\n# not a heading\n- nor a list\n```"),
                         [CodeBlock("# not a heading\n- nor a list\n")])


class TestQuotes(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(blocks("> a\n>b\n> \n> c"), [BlockQuote([Paragraph("a\nb"), Paragraph("c")])])

    def test_nested_content(self):
        self.assertEqual(blocks("> # T\n> > inner\n> - item"),
                         [BlockQuote([Heading(1, "T"), BlockQuote([Paragraph("inner")]),
                                      List(False, [ListItem([Paragraph("item")])])])])

    def test_no_lazy_continuation(self):
        self.assertEqual(blocks("> quoted\nnot quoted"), [BlockQuote([Paragraph("quoted")]), Paragraph("not quoted")])

    def test_interrupts_paragraph(self):
        self.assertEqual(blocks("para\n> q"), [Paragraph("para"), BlockQuote([Paragraph("q")])])


def ul(*items, tight=True):
    return List(False, [ListItem(list(i)) for i in items], 1, tight)


def ol(start, *items, tight=True):
    return List(True, [ListItem(list(i)) for i in items], start, tight)


class TestLists(unittest.TestCase):
    def test_bullets(self):
        self.assertEqual(blocks("- a\n- b\n- c"), [ul([Paragraph("a")], [Paragraph("b")], [Paragraph("c")])])

    def test_ordered_and_start(self):
        self.assertEqual(blocks("3. a\n4. b"), [ol(3, [Paragraph("a")], [Paragraph("b")])])
        self.assertEqual(blocks("007) x"), [ol(7, [Paragraph("x")])])

    def test_different_markers_make_new_lists(self):
        self.assertEqual(blocks("- a\n* b\n1. c\n1) d"),
                         [ul([Paragraph("a")]), ul([Paragraph("b")]), ol(1, [Paragraph("c")]), ol(1, [Paragraph("d")])])

    def test_continuation_lines(self):
        self.assertEqual(blocks("- first\n  line two\n- second"),
                         [ul([Paragraph("first\nline two")], [Paragraph("second")])])

    def test_nested(self):
        self.assertEqual(blocks("- a\n  - b\n    - c\n  - d\n- e"),
                         [ul([Paragraph("a"), ul([Paragraph("b"), ul([Paragraph("c")])], [Paragraph("d")])],
                             [Paragraph("e")])])

    def test_content_column_follows_the_marker(self):
        self.assertEqual(blocks("10. ten\n    sub\n1.  x\n    y"),
                         [ol(10, [Paragraph("ten\nsub")], [Paragraph("x\ny")])])

    def test_code_inside_item(self):
        self.assertEqual(blocks("- item\n\n  ```\n  code\n  ```"),
                         [ul([Paragraph("item"), CodeBlock("code\n")], tight=False)])

    def test_loose(self):
        self.assertEqual(blocks("- a\n\n- b"), [ul([Paragraph("a")], [Paragraph("b")], tight=False)])
        self.assertEqual(blocks("- a\n\n  more\n- b"),
                         [ul([Paragraph("a"), Paragraph("more")], [Paragraph("b")], tight=False)])

    def test_blank_inside_nested_list_keeps_outer_tight(self):
        self.assertEqual(blocks("- a\n  - b\n\n  - c\n- d"),
                         [ul([Paragraph("a"), ul([Paragraph("b")], [Paragraph("c")], tight=False)], [Paragraph("d")])])

    def test_ends_at_unindented_line(self):
        self.assertEqual(blocks("- a\n\nafter"), [ul([Paragraph("a")]), Paragraph("after")])
        self.assertEqual(blocks("- a\nnot lazy"), [ul([Paragraph("a")]), Paragraph("not lazy")])

    def test_interrupts_paragraph(self):
        self.assertEqual(blocks("intro\n- a"), [Paragraph("intro"), ul([Paragraph("a")])])

    def test_thematic_break_is_not_a_list(self):
        self.assertEqual(blocks("* * *"), [ThematicBreak()])

    def test_marker_needs_a_space(self):
        self.assertEqual(blocks("-not a list\n1.nor this"), [Paragraph("-not a list\n1.nor this")])


class TestTables(unittest.TestCase):
    def test_table(self):
        self.assertEqual(blocks("| A | B | C | D |\n|---|:--|:-:|--:|\n| 1 | 2 | 3 | 4 |"),
                         [Table(["A", "B", "C", "D"], [None, "left", "center", "right"], [["1", "2", "3", "4"]])])

    def test_optional_outer_pipes_and_ragged_rows(self):
        self.assertEqual(blocks("a | b\n--- | ---\n1 |\n1 | 2 | 3\n\nafter"),
                         [Table(["a", "b"], [None, None], [["1", ""], ["1", "2"]]), Paragraph("after")])

    def test_escaped_pipe(self):
        self.assertEqual(blocks("| x |\n|---|\n| a \\| b |"), [Table(["x"], [None], [["a | b"]])])

    def test_header_only(self):
        self.assertEqual(blocks("| h |\n| - |"), [Table(["h"], [None], [])])

    def test_body_ends_at_line_without_pipe(self):
        self.assertEqual(blocks("|a|\n|-|\n|1|\nplain"), [Table(["a"], [None], [["1"]]), Paragraph("plain")])

    def test_not_a_table(self):
        self.assertEqual(blocks("| a | b |\n|---|\n| 1 | 2 |"), [Paragraph("| a | b |\n|---|\n| 1 | 2 |")])
        self.assertEqual(blocks("a | b\nno delimiter"), [Paragraph("a | b\nno delimiter")])


class TestMixed(unittest.TestCase):
    def test_document(self):
        text = "# Title\n\nSome *text*.\n\n> quote\n\n1. one\n2. two\n\n```\ncode\n```\n---"
        self.assertEqual(blocks(text), [Heading(1, "Title"), Paragraph("Some *text*."),
                                        BlockQuote([Paragraph("quote")]),
                                        ol(1, [Paragraph("one")], [Paragraph("two")]),
                                        CodeBlock("code\n"), ThematicBreak()])
