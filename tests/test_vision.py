"""An attached image is read by the local vision model. The text model is not asked."""

from __future__ import annotations

from ssios.chat import iter_turn
from ssios.errors import SsiosError
from ssios.files import FileLibrary
from support import PlaneTest

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fe33d32f0000000049454e44ae426082"
)


class VisionTurnTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", "correct-horse-battery")
        self.account = self.plane.accounts.get(next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner"))
        self.saved = FileLibrary(self.plane).add(self.account["id"], PNG, name="shot.png")
        self.called = []

        def stream(messages, *, cancel_event, deadline):
            del messages, cancel_event, deadline
            self.called.append("text")
            yield "Synthetic local answer. No tools were used."

        self.plane.llm.stream_chat = stream

    def test_an_image_is_captioned_and_the_text_model_stays_idle(self) -> None:
        self.plane.llm.vision_script = ["A one-pixel synthetic picture."]
        events = list(iter_turn(self.plane, self.account, "What is in this picture?", "auto", "127.0.0.1", file_ids=[self.saved["id"]]))
        blob = " ".join(str(event) for event in events)
        self.assertIn("A one-pixel synthetic picture.", blob)
        self.assertIn(self.saved["url"], blob)
        self.assertNotIn("127.0.0.1", self.saved["url"])
        self.assertTrue(self.saved["url"].startswith("/ui/files/"))
        self.assertEqual(self.called, [])

    def test_a_missing_vision_model_names_the_pull_command(self) -> None:
        def describe(prompt, images, *, model, deadline):
            del prompt, images, deadline
            raise SsiosError("vision_unavailable", f"The vision model {model} is not installed. Run: ollama pull {model}", 503)

        self.plane.llm.describe_image = describe
        events = list(iter_turn(self.plane, self.account, "What is in this picture?", "auto", "127.0.0.1", file_ids=[self.saved["id"]]))
        blob = " ".join(str(event) for event in events)
        self.assertIn("ollama pull qwen2.5vl:7b", blob)
        self.assertIn(self.saved["url"], blob)
        self.assertEqual(self.called, [])
        self.assertEqual(self.plane.settings.vision_model, "qwen2.5vl:7b")
        self.assertEqual(self.plane.settings.codex_model, "qwen3:14b")
