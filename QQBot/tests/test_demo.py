"""Exercise the real mind with fake framework events and temporary JSON files."""

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot_plugin_shihai_demo"


class Event:
    def __init__(self, sender="USER_A", text="你好", platform="qqbot_deepseek", origin=None):
        self.sender = sender
        self.message_str = text
        self.platform = platform
        self.unified_msg_origin = origin or f"{platform}:FriendMessage:{sender}"
        self.extras = {}

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "BOT"

    def get_platform_id(self):
        return self.platform

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def plain_result(self, text):
        return text


class Request:
    def __init__(self, prompt="你好"):
        self.prompt = prompt
        self.system_prompt = "原有系统提示"


class Log:
    def __init__(self):
        self.errors = []

    def info(self, *args):
        pass

    def exception(self, *args):
        self.errors.append(args)


def decorator(*args, **kwargs):
    return lambda function: function


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shihai-qqbot-test-")
        self.data_root = Path(self.temp.name).resolve()
        self.log = Log()
        api = types.ModuleType("astrbot.api")
        api.logger = self.log
        events = types.ModuleType("astrbot.api.event")
        events.AstrMessageEvent = Event
        events.filter = types.SimpleNamespace(on_llm_request=decorator, command=decorator)
        provider = types.ModuleType("astrbot.api.provider")
        provider.ProviderRequest = Request
        star = types.ModuleType("astrbot.api.star")

        class Star:
            def __init__(self, context):
                self.context = context

        star.Star = Star
        star.Context = object
        star.register = decorator
        star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: self.data_root)
        self.framework = patch.dict(sys.modules, {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": api,
            "astrbot.api.event": events,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star,
        })
        self.framework.start()
        self.package = "_shihai_demo_test_" + uuid.uuid4().hex
        parent = types.ModuleType(self.package)
        parent.__path__ = [str(PLUGIN_DIR)]
        sys.modules[self.package] = parent
        spec = importlib.util.spec_from_file_location(self.package + ".main", PLUGIN_DIR / "main.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        self.plugin = self.module.ShihaiDemo(object())
        self.plugins = [self.plugin]

    async def asyncTearDown(self):
        for plugin in self.plugins:
            await plugin.terminate()
        self.framework.stop()
        for name in list(sys.modules):
            if name == self.package or name.startswith(self.package + "."):
                sys.modules.pop(name)
        # Only recursively clean up the temporary directory created above.
        self.assertEqual(self.data_root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.data_root.name.startswith("shihai-qqbot-test-"))
        self.temp.cleanup()

    async def test_platform_filter(self):
        event = Event(platform="different_platform")
        request = Request()
        await self.plugin.inject_state(event, request)
        self.assertFalse(self.plugin.engines)
        self.assertEqual(request.system_prompt, "原有系统提示")
        self.assertEqual([text async for text in self.plugin.show_state(event)], [])

    async def test_empty_and_self_messages_are_ignored(self):
        for event in [Event(text=""), Event(sender="BOT"), Event(sender="")]:
            await self.plugin.inject_state(event, Request(event.message_str))
        self.assertFalse(self.plugin.engines)

    async def test_emotion_memory_and_style_enter_request(self):
        event = Event(text="今天特别开心，太棒了，谢谢你！")
        request = Request(event.message_str)
        await self.plugin.inject_state(event, request)
        _, engine = self.plugin._get_engine(event)
        self.assertGreater(engine.state.e["happy"], 0)
        self.assertGreater(len(engine.graph.nodes), 0)
        self.assertIn("【意图包】", request.system_prompt)
        self.assertIn("【表达参数】", request.system_prompt)
        self.assertTrue(request._shihai_demo_injected)
        self.assertEqual(request.prompt, event.message_str)
        self.assertFalse(self.log.errors)

    async def test_same_request_and_event_are_not_observed_twice(self):
        event = Event(text="开心！谢谢你！")
        _, engine = self.plugin._get_engine(event)
        with patch.object(engine, "observe_user_text", wraps=engine.observe_user_text) as observe:
            request = Request(event.message_str)
            await self.plugin.inject_state(event, request)
            first_prompt = request.system_prompt
            await self.plugin.inject_state(event, request)
            await self.plugin.inject_state(event, Request(event.message_str))
            self.assertEqual(observe.call_count, 1)
            self.assertEqual(first_prompt, request.system_prompt)
            self.assertEqual(request.system_prompt.count("【意图包】"), 1)

    async def test_users_in_same_group_have_separate_memories(self):
        origin = "qqbot_deepseek:GroupMessage:TEST_GROUP"
        first = Event("USER_A", "我最喜欢的水果是蓝莓。", origin=origin)
        second = Event("USER_B", "我的宠物是一只橘猫。", origin=origin)
        for event in [first, second]:
            await self.plugin.inject_state(event, Request(event.message_str))
        first_key, first_engine = self.plugin._get_engine(first)
        second_key, second_engine = self.plugin._get_engine(second)
        self.assertNotEqual(first_key, second_key)
        self.assertIsNot(first_engine, second_engine)
        self.assertFalse(any("橘猫" in node["content"] for node in first_engine.graph.nodes.values()))
        self.assertFalse(any("蓝莓" in node["content"] for node in second_engine.graph.nodes.values()))

    async def test_json_persistence_and_restart(self):
        event = Event(text="今天特别开心，我最喜欢的水果是蓝莓。")
        await self.plugin.inject_state(event, Request(event.message_str))
        key, engine = self.plugin._get_engine(event)
        self.assertTrue((self.data_root / key / "mind_state.json").is_file())
        self.assertTrue((self.data_root / key / "shihai_graph.json").is_file())
        restarted = self.module.ShihaiDemo(object())
        self.plugins.append(restarted)
        _, restored = restarted._get_engine(event)
        self.assertEqual(engine.state.e, restored.state.e)
        self.assertEqual(len(engine.graph.nodes), len(restored.graph.nodes))
        self.assertTrue(any("蓝莓" in node["content"] for node in restored.graph.nodes.values()))
        self.assertFalse(restarted.last_injected)

    async def test_status_command_reports_injection(self):
        event = Event(text="今天开心！")
        await self.plugin.inject_state(event, Request(event.message_str))
        results = [result async for result in self.plugin.show_state(event)]
        self.assertEqual(len(results), 1)
        self.assertIn("本次启动后已注入：是", results[0])
        self.assertIn("主动外发：关闭", results[0])

    async def test_tick_does_not_schedule_proactive_message(self):
        _, engine = self.plugin._get_engine(Event())
        engine.state.urge = 3.0
        engine._last_tick_at -= 3600
        with patch.object(engine, "_emit_fire_pending") as fire:
            engine.tick(proactive_enabled=False)
        fire.assert_not_called()
        self.assertFalse(engine.state.fire_pending)

    async def test_failure_does_not_break_original_model_request(self):
        event = Event()
        request = Request()
        _, engine = self.plugin._get_engine(event)
        with patch.object(engine, "snapshot", side_effect=RuntimeError("simulated snapshot error")):
            await self.plugin.inject_state(event, request)
        self.assertEqual(request.system_prompt, "原有系统提示")
        self.assertFalse(getattr(request, "_shihai_demo_injected", False))
        self.assertTrue(self.log.errors)

    async def test_heartbeat_starts_once_and_shuts_down(self):
        await self.plugin.initialize()
        task = self.plugin._ticker
        await self.plugin.initialize()
        self.assertIs(self.plugin._ticker, task)
        await asyncio.sleep(0)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.plugin._ticker)


if __name__ == "__main__":
    unittest.main()
