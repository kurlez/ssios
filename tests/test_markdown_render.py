"""Notes Markdown: the forms Obsidian writes render, and nothing in a note runs as HTML."""

from __future__ import annotations

import unittest

from ssios.markdown_render import inline, render


def _link(url: str) -> str:
    if url.startswith(("https://", "http://")):
        return url
    if url.startswith(("javascript:", "data:")):
        return ""
    return "/ui/vault/note?path=" + url if not url.endswith(".png") else "/ui/vault/asset?path=" + url


class MarkdownTest(unittest.TestCase):
    def test_the_note_from_the_report_renders_bold_nested_lists_links_and_a_rule(self) -> None:
        text = (
            "# 0xQuit：Binance 热钱包被盗分析\n\n"
            "- **来源**：X（Twitter）@0xQuit\n"
            "- **发布时间**：\n"
            "  - 主分析帖：2026-10-29 14:09 PDT（2026-10-29 21:09 UTC）\n"
            "  - 修正帖：2026-10-29 16:07 PDT\n"
            "- **原文链接**：\n"
            "\t- 主分析帖：https://x.com/0xQuit/status/2115913970075009439\n"
            "- **收录范围说明**：两条串（conversation）中的自回复。\n"
            "---\n\n"
            "## Part 1 中文译文\n"
        )
        out = render(text, _link)
        self.assertIn('<h1 id="0xquitbinance-热钱包被盗分析">0xQuit：Binance 热钱包被盗分析</h1>', out)
        self.assertIn("<li><strong>来源</strong>：X（Twitter）@0xQuit</li>", out)
        self.assertIn("<li><strong>发布时间</strong>：\n<ul><li>主分析帖：2026-10-29 14:09 PDT（2026-10-29 21:09 UTC）</li><li>修正帖：2026-10-29 16:07 PDT</li></ul></li>", out)
        self.assertIn('<ul><li>主分析帖：<a href="https://x.com/0xQuit/status/2115913970075009439" rel="noopener noreferrer">https://x.com/0xQuit/status/2115913970075009439</a></li></ul>', out)
        self.assertNotIn("**", out)
        self.assertNotIn("<p>-", out)
        self.assertNotIn("---", out)
        self.assertTrue(out.index("</ul>\n<hr>") < out.index('<h2 id="part-1-中文译文">Part 1 中文译文</h2>'))

    def test_blocks(self) -> None:
        out = render(
            "1. one\n2. two\n   - [x] done\n   - [ ] todo\n\n"
            "> [!warning] 注意\n> 内容 **粗**\n\n> plain quote\n> second line\n\n"
            "| 名称 | 数量 |\n|:--|--:|\n| a \\| b | **2** |\n| `x|y` |\n\n"
            "```python\nprint('<hi>')\n# not a heading\n```\n\n"
            "line one\nline two\n\n***\n\n###### six\n####### seven\n",
            _link,
        )
        self.assertIn('<ol><li>one</li><li>two\n<ul><li class="task"><input type="checkbox" disabled checked> done</li><li class="task"><input type="checkbox" disabled> todo</li></ul></li></ol>', out)
        self.assertIn('<ol start="7"><li>seven</li></ol>', render("7. seven\n", _link))
        self.assertEqual(render("- [ ] todo\n\n1. first\n2. second\n- bullet\n", _link), '<ul><li class="task"><input type="checkbox" disabled> todo</li></ul>\n<ol><li>first</li><li>second</li></ol>\n<ul><li>bullet</li></ul>')
        self.assertIn('<div class="callout callout-warning"><div class="callout-title">注意</div><p>内容 <strong>粗</strong></p></div>', out)
        self.assertIn("<blockquote><p>plain quote<br>second line</p></blockquote>", out)
        self.assertIn('<th style="text-align:left">名称</th><th style="text-align:right">数量</th>', out)
        self.assertIn('<td style="text-align:left">a | b</td><td style="text-align:right"><strong>2</strong></td>', out)
        self.assertIn('<td style="text-align:left"><code>x|y</code></td><td style="text-align:right"></td>', out)
        self.assertIn('<pre><code class="language-python">print(<span class="tok-s">&#x27;&lt;hi&gt;&#x27;</span>)\n<span class="tok-c"># not a heading</span></code></pre>', out)
        self.assertIn("<p>line one<br>line two</p>", out)
        self.assertIn("<hr>", out)
        self.assertIn('<h6 id="six">six</h6>', out)
        self.assertIn("<p>####### seven</p>", out)
        loose = render("- a\n\n- b\n\n  more\n\nafter\n", _link)
        self.assertIn("<li><p>b</p>\n<p>more</p></li>", loose)
        self.assertTrue(loose.endswith("<p>after</p>"))

    def test_inline_marks_links_and_tags(self) -> None:
        self.assertEqual(inline("***a*** **b** *c* _d_ __e__ ~~f~~ ==g==", _link), "<strong><em>a</em></strong> <strong>b</strong> <em>c</em> <em>d</em> <strong>e</strong> <del>f</del> <mark>g</mark>")
        self.assertEqual(inline("snake_case_name and 2 * 3 * 4 and a\\*b\\*", _link), "snake_case_name and 2 * 3 * 4 and a*b*")
        self.assertEqual(inline("`**not bold**` and ``a ` b``", _link), "<code>**not bold**</code> and <code>a ` b</code>")
        self.assertEqual(inline("[[Budget]] [[pier story|the story]] ![[dock.png]]", _link), '<a href="/ui/vault/note?path=Budget" rel="noopener noreferrer">Budget</a> <a href="/ui/vault/note?path=pier story" rel="noopener noreferrer">the story</a> <img alt="" src="/ui/vault/asset?path=dock.png" loading="lazy">')
        self.assertEqual(inline("见 https://example.com/a(b)c）。还有 <https://example.com/x>", _link), '见 <a href="https://example.com/a(b)c" rel="noopener noreferrer">https://example.com/a(b)c</a>）。还有 <a href="https://example.com/x" rel="noopener noreferrer">https://example.com/x</a>')
        self.assertEqual(inline("[**bold** link](https://example.com \"title\")", _link), '<a href="https://example.com" rel="noopener noreferrer"><strong>bold</strong> link</a>')
        self.assertEqual(inline("#harbor 和 #计划/2026 but not a#b or C# code", _link), '<span class="tag">#harbor</span> 和 <span class="tag">#计划/2026</span> but not a#b or C# code')

    def test_nothing_in_a_note_runs(self) -> None:
        out = render(
            "<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1)) ![y](javascript:alert(1))\n\n"
            "[z](data:text/html,<b>) <javascript:alert(1)>\n\n| <b>h</b> |\n|---|\n| <i>c</i> |\n\n- <u>li</u>\n\n> <s>q</s>\n\n```<script>\n</script>\n```\n\n[a](https://example.com/\"onmouseover=\"alert(1))\n",
            _link,
        )
        for bad in ("<script", "<img src=x", "<b>", "<i>", "<u>", "<s>", 'href="javascript', 'src="javascript', 'href="data:', '"onmouseover="'):
            self.assertNotIn(bad, out, bad)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertEqual(render("- " * 400 + "deep\n", _link).count("<ul>") <= 13, True)
        self.assertEqual(inline("\x000\x00 **b**", _link), "0 <strong>b</strong>")


class MathAndImageTest(unittest.TestCase):
    def test_math_is_passed_through_for_katex_and_prices_are_left_alone(self) -> None:
        out = inline(r"质能 $E=mc^2$ 与 \(a_b\)，价格 $12.50 and $3，\$5，`$x$`，$$\frac{a}{b}$$", _link)
        self.assertIn('<span class="math" data-display="0">E=mc^2</span>', out)
        self.assertIn('<span class="math" data-display="0">a_b</span>', out)
        self.assertIn("价格 $12.50 and $3，$5，<code>$x$</code>", out)
        self.assertIn('<span class="math" data-display="1">\\frac{a}{b}</span>', out)
        self.assertEqual(inline(r"$a_1 * b_2 * c$ and $x<y$", _link), '<span class="math" data-display="0">a_1 * b_2 * c</span> and <span class="math" data-display="0">x&lt;y</span>')
        block = render("前文\n$$\n\\int_0^1 x^2\\,dx = \\frac{1}{3}\n$$\n后文\n\n$$a<b$$\n\n\\[\nx_1\n\\]\n", _link)
        self.assertIn('<p>前文</p>\n<div class="math math-block" data-display="1">\\int_0^1 x^2\\,dx = \\frac{1}{3}</div>\n<p>后文</p>', block)
        self.assertIn('<div class="math math-block" data-display="1">a&lt;b</div>', block)
        self.assertIn('<div class="math math-block" data-display="1">x_1</div>', block)
        self.assertNotIn("<script", render("$$\n</div><script>alert(1)</script>\n$$\n $<img src=x>$", _link))

    def test_images_take_a_size_and_a_placed_note_is_a_link(self) -> None:
        out = inline("![[Pasted image 2026.png|300]] ![[a.png|300x200]] ![[Note]] ![cap|120](b.png) ![远程](https://e.com/x.png) ![[a.png|not a size]]", _link)
        self.assertIn('<img alt="" src="/ui/vault/asset?path=Pasted image 2026.png" width="300" loading="lazy">', out)
        self.assertIn('<img alt="" src="/ui/vault/asset?path=a.png" width="300" height="200" loading="lazy">', out)
        self.assertIn('<a href="/ui/vault/note?path=Note" rel="noopener noreferrer">Note</a>', out)
        self.assertIn('<img alt="cap" src="/ui/vault/asset?path=b.png" width="120" loading="lazy">', out)
        self.assertIn('<a class="remote-image" href="https://e.com/x.png" rel="noopener noreferrer">远程</a>', out)
        self.assertNotIn('src="https://', out)
        self.assertTrue(out.endswith('<img alt="" src="/ui/vault/asset?path=a.png" loading="lazy">'))
