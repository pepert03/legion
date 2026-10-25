import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from textkit.cli import main


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        code = main(argv)
    return code, buf.getvalue()


class TestCli(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(run(["slug", "Hello World"]), (0, "hello-world\n"))

    def test_wrap(self):
        self.assertEqual(run(["wrap", "--width", "6", "abc de fgh"]), (0, "abc de\nfgh\n"))

    def test_unknown(self):
        self.assertEqual(run(["bogus"])[0], 2)
