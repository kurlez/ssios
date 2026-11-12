"""Creative and hypothetical sentences stay in chat. Real calendar and mail requests do not."""

from __future__ import annotations

import unittest

from ssios.chat_router import classify

# Near misses: a meeting, weather, or mail word inside writing or a definition.
NEAR = (
    "帮我写一个小故事，描述一次积极开会但是却没有结果的经历。",
    "帮我写一个关于开会的故事",
    "写一首关于天气的诗",
    "写一篇文章，讲一次没有结果的会议",
    "总结一下什么是邮件",
    "什么是会议",
    "开会是什么意思",
    "假如开会没有结果会怎样",
    "想象一次没有人发言的会议",
    "假设明天开会，故事会怎么写",
    "write a short story about a meeting that had no outcome",
    "write a poem about the weather",
    "write an essay about email",
    "explain what a meeting is",
    "what is email",
    "what is a meeting",
    "what if a meeting has no outcome",
    "suppose I had a meeting that went nowhere",
    "imagine an email that was never sent",
    "describe a hypothetical meeting with no result",
)

# A keyword alone is not a calendar change.
BARE = ("开会", "会议", "meeting")

# Real requests.
HITS = (
    ("今天天气怎么样", "skill", "weather"),
    ("What's the weather in Seattle?", "skill", "weather"),
    ("查一下我的邮件", "connector", "gmail"),
    ("check my inbox", "connector", "gmail"),
    ("这周我有什么安排", "connector", "calendar"),
    ("what's on my calendar this week", "connector", "calendar"),
    ("明天下午有空吗", "connector", "calendar"),
    ("下周三下午三点和张三开会一小时", "connector", "calendar"),
    ("schedule a meeting tomorrow at 3pm", "connector", "calendar"),
    ("add a dentist appointment to my calendar tomorrow at 4", "connector", "calendar"),
    ("book a meeting with Ada on Friday at 10", "connector", "calendar"),
    ("把周五的牙医改到下午四点", "connector", "calendar"),
    ("取消明天的站会", "connector", "calendar"),
    ("安排明天下午三点开会", "connector", "calendar"),
    ("创建一个会议，下周一上午十点", "connector", "calendar"),
    ("约张三明天下午三点开会", "connector", "calendar"),
)


class RouterNearMissTest(unittest.TestCase):
    def test_near_misses_stay_in_chat(self) -> None:
        self.assertGreaterEqual(len(NEAR) + len(BARE) + len(HITS), 30)
        for message in NEAR + BARE:
            decision = classify(message)
            self.assertEqual(decision.route, "direct", message)
            self.assertEqual(decision.services, (), message)
            self.assertNotEqual(decision.skill, "weather", message)

    def test_real_requests_keep_their_route(self) -> None:
        for message, route, kind in HITS:
            decision = classify(message)
            self.assertEqual(decision.route, route, message)
            if route == "skill":
                self.assertEqual(decision.skill, kind, message)
            else:
                self.assertIn(kind, decision.services, message)
