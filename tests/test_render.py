"""The vendored markdown renderer escapes HTML and keeps currency dollars."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "src" / "ssios" / "vendor" / "render.js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderTest(unittest.TestCase):
    def test_xss_math_cjk_and_currency(self) -> None:
        script = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const html = globalThis.ssiosRender(process.argv[2]);
process.stdout.write(html);
"""
        source = "\n".join(
            [
                "### Title",
                "",
                "- one",
                "- two",
                "",
                "Cost is $12.50 today.",
                "",
                "公式\\(V-E+F=2\\)成立",
                "",
                "<script>alert(1)</script>",
                "",
                "[ok](https://example.com)",
                "",
                "[bad](javascript:alert(1))",
            ]
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script, str(RENDER), source],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        html = result.stdout
        self.assertIn("<h3>Title</h3>", html)
        self.assertIn("<li>one</li>", html)
        self.assertIn("$12.50", html)
        self.assertIn("公式", html)
        self.assertIn("V-E+F=2", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertNotIn("javascript:", html)


if __name__ == "__main__":
    unittest.main()
