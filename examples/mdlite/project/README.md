# mdlite

A small Markdown to HTML converter in pure Python, with no dependencies: headings, paragraphs,
emphasis, code, links and images, quotes, nested lists, tables, strikethrough and a table of contents.

```sh
python -m mdlite notes.md -o notes.html --toc --title "My notes"
```

```python
from mdlite import to_html
to_html("# Hi\n\nSome *text*.")   # '<h1>Hi</h1>\n<p>Some <em>text</em>.</p>\n'
```

What it understands, exactly, is in [SPEC.md](SPEC.md); the tests in `tests/` are the contract.

```
mdlite/
  nodes.py     the document tree every part shares (fixed)
  blocks/      text -> tree
  inline/      the text inside blocks -> HTML
  render.py    tree -> HTML
  toc.py       heading ids and the table of contents
  html.py      escaping
  cli.py       the command line
```

Run every test with `python -m unittest discover -s tests -q`, and the benchmark with
`python bench_mdlite.py`.
