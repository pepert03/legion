import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def mdlite(*args, input=None):
    return subprocess.run([sys.executable, "-m", "mdlite", *args], cwd=ROOT, input=input,
                          capture_output=True, text=True, encoding="utf-8")


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.dir.name, "doc.md")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("# Hi\n\nSome *text* — ñ.\n")

    def tearDown(self):
        self.dir.cleanup()

    def test_file_to_stdout(self):
        p = mdlite(self.src)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "<h1>Hi</h1>\n<p>Some <em>text</em> — ñ.</p>\n")

    def test_stdin_and_output_file(self):
        out = os.path.join(self.dir.name, "out.html")
        p = mdlite("-", "-o", out, input="**x**\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "")
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "<p><strong>x</strong></p>\n")

    def test_toc(self):
        p = mdlite("-", "--toc", input="# A\n## B\n")
        self.assertEqual(p.stdout, '<nav class="toc">\n<ul>\n<li><a href="#a">A</a>\n<ul>\n<li><a href="#b">B</a></li>\n'
                                   '</ul>\n</li>\n</ul>\n</nav>\n<h1 id="a">A</h1>\n<h2 id="b">B</h2>\n')

    def test_title_makes_a_page(self):
        p = mdlite("-", "--title", "Notes & <more>", input="hi\n")
        self.assertEqual(p.stdout, '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
                                   "<title>Notes &amp; &lt;more&gt;</title>\n</head>\n<body>\n<p>hi</p>\n"
                                   "</body>\n</html>\n")

    def test_missing_file(self):
        p = mdlite(os.path.join(self.dir.name, "nope.md"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("nope.md", p.stderr)
        self.assertEqual(p.stdout, "")
