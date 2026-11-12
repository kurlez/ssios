"""model-bench scores a stand-in Ollama. It does not open a socket or write a file."""

from __future__ import annotations

import json
from pathlib import Path

from ssios.errors import SsiosError
from ssios.model_bench import TURNS, run_bench


def _reply(name: str, arguments: dict, *, as_text: bool = False) -> bytes:
    if as_text:
        body = "```json\n" + json.dumps({"name": name, "arguments": arguments}) + "\n```"
        message = {"role": "assistant", "content": body}
    else:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    return json.dumps({"message": message}).encode()


class ModelBenchTest(unittest_case := __import__("unittest").TestCase):
    def test_rates_memory_and_muse_name_use_the_stand_in(self) -> None:
        script = []
        for index, turn in enumerate(TURNS):
            if index == 1:
                script.append(_reply("weather", {"place": "Seattle"}))
            elif index == 3:
                script.append(_reply(turn["tool"], {}))
            elif index == 5:
                script.append(_reply(turn["tool"], {"url": "https://example.com/harbor"}, as_text=True))
            elif turn["tool"] == "gmail.search":
                script.append(_reply(turn["tool"], {"q": "in:inbox"}))
            elif turn["tool"] == "gmail.read":
                script.append(_reply(turn["tool"], {"id": "msg-1"}))
            elif turn["tool"] == "drive.read":
                script.append(_reply(turn["tool"], {"id": "file-1"}))
            elif turn["tool"] == "tasks.add":
                script.append(_reply(turn["tool"], {"title": "Call the harbor desk"}))
            elif turn["tool"] == "weather":
                script.append(_reply(turn["tool"], {"place": "Seattle"}))
            else:
                script.append(_reply(turn["tool"], {"q": "studio"} if "q" in turn["required"] else {}))
        seen = []

        def opener(url: str, payload):
            seen.append((url, payload))
            if payload is None:
                body = {"models": [{"name": "muse-glimmer:30b-mlx", "size": 20 * 1024**3, "size_vram": 19 * 1024**3}]}
                return 200, json.dumps(body).encode()
            return 200, script.pop(0)

        before = set(Path(".").glob("model-bench*"))
        report = run_bench(model="muse-glimmer:30b-mlx", base_url="http://127.0.0.1:11434", opener=opener)
        self.assertEqual(set(Path(".").glob("model-bench*")), before)
        self.assertEqual(report["model"], "muse-glimmer:30b-mlx")
        self.assertEqual(report["turns"], 10)
        self.assertEqual(report["tool_call_as_text_count"], 1)
        self.assertLess(report["correct_tool_rate"], 1)
        self.assertGreater(report["valid_tool_call_rate"], 0.5)
        self.assertTrue(report["write_needs_approval"])
        self.assertEqual(report["peak_memory"]["size_vram"], 19 * 1024**3)
        self.assertIn("under 3GB", report["warning"])
        self.assertTrue(seen[0][1]["think"] is False)
        self.assertIn("gmail.search", json.dumps(seen[0][1]["tools"]))
        with self.assertRaises(SsiosError):
            run_bench(model="muse-glimmer:30b-mlx", base_url="https://example.com", opener=opener)
