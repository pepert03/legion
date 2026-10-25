import unittest

from mdlite.inline import render_inline as r


class TestText(unittest.TestCase):
    def test_plain_text_is_escaped(self):
        self.assertEqual(r('a <b> & "c"'), "a &lt;b&gt; &amp; &quot;c&quot;")
        self.assertEqual(r("<div>raw html</div>"), "&lt;div&gt;raw html&lt;/div&gt;")

    def test_backslash_escapes(self):
        self.assertEqual(r(r"\*not emphasis\*"), "*not emphasis*")
        self.assertEqual(r(r"\[not a link\](x)"), "[not a link](x)")
        self.assertEqual(r(r"\\"), "\\")
        self.assertEqual(r(r"\a \1"), r"\a \1")
        self.assertEqual(r(r"\<b\>"), "&lt;b&gt;")


class TestCodeSpans(unittest.TestCase):
    def test_code(self):
        self.assertEqual(r("use `print()`"), "use <code>print()</code>")

    def test_nothing_is_formatted_inside(self):
        self.assertEqual(r("`*a* [b](c) <d>`"), "<code>*a* [b](c) &lt;d&gt;</code>")
        self.assertEqual(r(r"`\*`"), r"<code>\*</code>")

    def test_backtick_runs(self):
        self.assertEqual(r("`` a`b ``"), "<code>a`b</code>")
        self.assertEqual(r("` `` `"), "<code>``</code>")
        self.assertEqual(r("`a``"), "`a``")

    def test_spaces_and_newlines(self):
        self.assertEqual(r("`  a  `"), "<code> a </code>")
        self.assertEqual(r("`   `"), "<code>   </code>")
        self.assertEqual(r("`a\nb`"), "<code>a b</code>")

    def test_unclosed(self):
        self.assertEqual(r("`a"), "`a")


class TestEmphasis(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(r("*a* _b_ **c** __d__"), "<em>a</em> <em>b</em> <strong>c</strong> <strong>d</strong>")

    def test_nested(self):
        self.assertEqual(r("**a *b* c**"), "<strong>a <em>b</em> c</strong>")
        self.assertEqual(r("*a **b** c*"), "<em>a <strong>b</strong> c</em>")
        self.assertEqual(r("***x***"), "<em><strong>x</strong></em>")
        self.assertEqual(r("_a **b** c_"), "<em>a <strong>b</strong> c</em>")

    def test_spaces_block_delimiters(self):
        self.assertEqual(r("a * b * c"), "a * b * c")
        self.assertEqual(r("*a *"), "*a *")
        self.assertEqual(r("** a**"), "** a**")

    def test_underscore_inside_words(self):
        self.assertEqual(r("snake_case_name"), "snake_case_name")
        self.assertEqual(r("_foo_bar"), "_foo_bar")

    def test_star_inside_words(self):
        self.assertEqual(r("foo*bar*baz"), "foo<em>bar</em>baz")

    def test_unmatched(self):
        self.assertEqual(r("*a"), "*a")
        self.assertEqual(r("**foo*"), "*<em>foo</em>")
        self.assertEqual(r("a**"), "a**")

    def test_with_code(self):
        self.assertEqual(r("*a `*` b*"), "<em>a <code>*</code> b</em>")


class TestStrikethrough(unittest.TestCase):
    def test_del(self):
        self.assertEqual(r("~~gone~~ and **~~both~~**"), "<del>gone</del> and <strong><del>both</del></strong>")

    def test_single_tilde(self):
        self.assertEqual(r("~a~ ~~b"), "~a~ ~~b")


class TestLinks(unittest.TestCase):
    def test_link(self):
        self.assertEqual(r("see [the docs](https://example.com/docs)"),
                         'see <a href="https://example.com/docs">the docs</a>')

    def test_title(self):
        self.assertEqual(r('[a](/u "The title")'), '<a href="/u" title="The title">a</a>')

    def test_formatted_text(self):
        self.assertEqual(r("[*em* and `code`](/u)"), '<a href="/u"><em>em</em> and <code>code</code></a>')

    def test_angle_brackets_and_empty_url(self):
        self.assertEqual(r("[a](</my url>)"), '<a href="/my url">a</a>')
        self.assertEqual(r("[a]()"), '<a href="">a</a>')

    def test_attributes_are_escaped(self):
        self.assertEqual(r('[x](/a?b=1&c=2 "say \\"hi\\"")'), '<a href="/a?b=1&amp;c=2" title="say &quot;hi&quot;">x</a>')

    def test_balanced_brackets(self):
        self.assertEqual(r("[a [b] c](/u)"), '<a href="/u">a [b] c</a>')

    def test_no_links_inside_links(self):
        self.assertEqual(r("[[a](/in)](/out)"), '[<a href="/in">a</a>](/out)')

    def test_not_links(self):
        self.assertEqual(r("[just brackets]"), "[just brackets]")
        self.assertEqual(r("[a] (b)"), "[a] (b)")
        self.assertEqual(r("[a](b c)"), "[a](b c)")

    def test_emphasis_does_not_cross_link_boundary(self):
        self.assertEqual(r("*[a*](/u)"), '*<a href="/u">a*</a>')


class TestImages(unittest.TestCase):
    def test_image(self):
        self.assertEqual(r("![a cat](/cat.png)"), '<img src="/cat.png" alt="a cat" />')

    def test_title_and_plain_alt(self):
        self.assertEqual(r('![a *b* `c`](/i.png "T")'), '<img src="/i.png" alt="a b c" title="T" />')

    def test_image_inside_link(self):
        self.assertEqual(r("[![logo](/l.png)](/home)"), '<a href="/home"><img src="/l.png" alt="logo" /></a>')

    def test_not_an_image(self):
        self.assertEqual(r("! [a](/u)"), '! <a href="/u">a</a>')


class TestAutolinks(unittest.TestCase):
    def test_autolink(self):
        self.assertEqual(r("<https://example.com/a?b=c&d>"),
                         '<a href="https://example.com/a?b=c&amp;d">https://example.com/a?b=c&amp;d</a>')
        self.assertEqual(r("<http://x.org>"), '<a href="http://x.org">http://x.org</a>')

    def test_not_autolinks(self):
        self.assertEqual(r("<ftp://x.org>"), "&lt;ftp://x.org&gt;")
        self.assertEqual(r("<https://a b>"), "&lt;https://a b&gt;")


class TestLineBreaks(unittest.TestCase):
    def test_hard_breaks(self):
        self.assertEqual(r("a  \nb"), "a<br />\nb")
        self.assertEqual(r("a\\\nb"), "a<br />\nb")

    def test_soft_break(self):
        self.assertEqual(r("a \n   b"), "a\nb")
        self.assertEqual(r("a\nb"), "a\nb")
