"""Whole documents, end to end: every part of mdlite working together."""

import unittest

from mdlite import to_html

README = """\
# mdlite

A *small* Markdown converter, with **no dependencies**.

## Install

```sh
pip install mdlite
```

## Use

1. Write `notes.md`.
2. Run:

       python -m mdlite notes.md -o notes.html

3. Open it. See [the spec](SPEC.md "What it supports").

> **Note:** raw <b>HTML</b> is escaped,
> not passed through.

| Feature | Status |
|:--------|:------:|
| Lists   | ✓ |
| Tables  | ✓ |

---
Made with ~~magic~~ care.
"""

README_HTML = """\
<h1>mdlite</h1>
<p>A <em>small</em> Markdown converter, with <strong>no dependencies</strong>.</p>
<h2>Install</h2>
<pre><code class="language-sh">pip install mdlite
</code></pre>
<h2>Use</h2>
<ol>
<li><p>Write <code>notes.md</code>.</p></li>
<li><p>Run:</p>
<pre><code>python -m mdlite notes.md -o notes.html
</code></pre></li>
<li><p>Open it. See <a href="SPEC.md" title="What it supports">the spec</a>.</p></li>
</ol>
<blockquote>
<p><strong>Note:</strong> raw &lt;b&gt;HTML&lt;/b&gt; is escaped,
not passed through.</p>
</blockquote>
<table>
<thead>
<tr>
<th style="text-align: left">Feature</th>
<th style="text-align: center">Status</th>
</tr>
</thead>
<tbody>
<tr>
<td style="text-align: left">Lists</td>
<td style="text-align: center">✓</td>
</tr>
<tr>
<td style="text-align: left">Tables</td>
<td style="text-align: center">✓</td>
</tr>
</tbody>
</table>
<hr />
<p>Made with <del>magic</del> care.</p>
"""

TODO = """\
- [ ] not a checkbox, just brackets
- nested:
  - *one*
  - two
    1. deep
- end
"""

TODO_HTML = """\
<ul>
<li>[ ] not a checkbox, just brackets</li>
<li>nested:
<ul>
<li><em>one</em></li>
<li>two
<ol>
<li>deep</li>
</ol></li>
</ul></li>
<li>end</li>
</ul>
"""


class TestDocuments(unittest.TestCase):
    def test_readme(self):
        self.assertEqual(to_html(README), README_HTML)

    def test_nested_lists(self):
        self.assertEqual(to_html(TODO), TODO_HTML)

    def test_toc(self):
        out = to_html("# A\n\n## B\n\n> # Quoted\n\n## B\n", toc=True)
        self.assertEqual(out, '<nav class="toc">\n<ul>\n<li><a href="#a">A</a>\n<ul>\n<li><a href="#b">B</a></li>\n'
                              '<li><a href="#b-1">B</a></li>\n</ul>\n</li>\n</ul>\n</nav>\n'
                              '<h1 id="a">A</h1>\n<h2 id="b">B</h2>\n<blockquote>\n<h1>Quoted</h1>\n</blockquote>\n'
                              '<h2 id="b-1">B</h2>\n')

    def test_empty(self):
        self.assertEqual(to_html(""), "")

    def test_renderer_and_toc_use_the_inline_formatter(self):
        from mdlite.nodes import Document, Paragraph
        from mdlite.render import render
        from mdlite.toc import toc_html
        self.assertEqual(render(Document([Paragraph("*hi* & bye")])), "<p><em>hi</em> &amp; bye</p>\n")
        self.assertEqual(toc_html([(1, "*A* & B", "a-b")]), '<ul>\n<li><a href="#a-b"><em>A</em> &amp; B</a></li>\n</ul>')
