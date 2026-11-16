"""调参回归：真实插件/心智核及临时 JSON，不启动 AstrBot、QQ 或模型。

运行：python -B -m unittest discover -s plugins/astrbot_plugin_shihai/tests -v
"""

import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]


def decorator(*args, **kwargs):
    return lambda function: function


def immediate(coroutine):
    """框架请求替身立即返回；禁止测试意外等待真实异步 I/O。"""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    else:
        raise AssertionError("测试调用意外等待了真实 I/O")
    finally:
        coroutine.close()


class Config(dict):
    def __init__(self, values=None):
        super().__init__(values or {})
        self.saved = dict(self)
        self.fail_save = False

    def save_config(self):
        if self.fail_save:
            raise PermissionError("模拟配置写入失败")
        self.saved = dict(self)


class Context:
    def __init__(self):
        self.stars = {}

    def get_registered_star(self, name):
        return self.stars.get(name)

    def register_web_api(self, *args):
        pass

    def add(self, name, plugin):
        # 与 AstrBot StarMetadata 一致：运行实例在 star_cls 上。
        self.stars[name] = types.SimpleNamespace(
            name=name, activated=True, star_cls=plugin)


class TuningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shihai-tuning-test-", dir=ROOT)
        self.data_root = Path(self.temp.name).resolve()
        self.packages = []
        api = types.ModuleType("astrbot.api")
        api.AstrBotConfig = Config
        api.logger = Mock()
        event = types.ModuleType("astrbot.api.event")
        event.AstrMessageEvent = object
        event.filter = types.SimpleNamespace(
            on_llm_request=decorator, llm_tool=decorator, on_decorating_result=decorator)
        provider = types.ModuleType("astrbot.api.provider")
        provider.ProviderRequest = object
        star = types.ModuleType("astrbot.api.star")

        class Star:
            def __init__(self, context):
                self.context = context

        star.Star = Star
        star.Context = Context
        star.register = decorator
        paths = types.ModuleType("astrbot.core.utils.astrbot_path")
        paths.get_astrbot_plugin_data_path = lambda: str(self.data_root)
        self.framework = patch.dict(sys.modules, {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star,
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
            "astrbot.core.utils.astrbot_path": paths,
        })
        self.framework.start()
        self.addCleanup(self.clean)
        self.core_module = self.load_plugin("astrbot_plugin_shihai")
        self.psyche_module = self.load_plugin("astrbot_plugin_shihai_psyche")
        self.guard_module = self.load_plugin("astrbot_plugin_shihai_guard")
        self.context = Context()
        self.core = self.core_module.ShihaiPlugin(self.context, Config({"E": 0.6}))
        self.context.add("astrbot_plugin_shihai", self.core)
        self.psyche = self.psyche_module.PsychePlugin(self.context, Config())
        self.context.add("astrbot_plugin_shihai_psyche", self.psyche)

    def clean(self):
        self.framework.stop()
        for name in list(sys.modules):
            if any(name == package or name.startswith(package + ".")
                   for package in self.packages):
                sys.modules.pop(name)
        # 只递归清理本测试在工作区内创建的已验证临时目录。
        self.assertEqual(self.data_root.parent, ROOT.resolve())
        self.assertTrue(self.data_root.name.startswith("shihai-tuning-test-"))
        self.temp.cleanup()

    def load_plugin(self, name):
        directory = ROOT / "plugins" / name
        package = "_tuning_test_" + uuid.uuid4().hex
        self.packages.append(package)
        parent = types.ModuleType(package)
        parent.__path__ = [str(directory)]
        sys.modules[package] = parent
        spec = importlib.util.spec_from_file_location(package + ".main", directory / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def web(self, body):
        async def get_json(**kwargs):
            return body

        quart = types.ModuleType("quart")
        quart.request = types.SimpleNamespace(get_json=get_json)
        with patch.dict(sys.modules, {"quart": quart}):
            result = immediate(self.core._web_cmd())
        return result if isinstance(result, tuple) else (result, 200)

    def saved_psyche(self):
        return json.loads(Path(self.psyche.state_path).read_text(encoding="utf-8"))

    def tick_twice(self):
        for _ in range(2):
            self.core.engine.tick(self.core.engine._last_tick_at + 60,
                                  proactive_enabled=False, psyche_path=self.core.psyche_path)

    def test_web_values_survive_heartbeats_and_psyche_flush(self):
        changes = {"O": 0.81, "C": 0.82, "E": 0.93, "A": 0.24, "N": 0.85,
                   "eff": 0.91, "send_threshold": 2.4, "tantrum_threshold": 1.3}
        for key, value in changes.items():
            with self.subTest(key=key):
                result, status = self.web({"op": "tune", "key": key, "value": value})
                self.assertEqual(status, 200)
                self.assertEqual(result["value"], value)
                self.assertEqual(result["key"], key)
                self.assertEqual(self.core._param_value(key), value)
        self.psyche._flush()
        self.tick_twice()
        for key, value in changes.items():
            self.assertEqual(self.core._param_value(key), value)
            section = "psi" if key in "OCEAN" else "theta"
            self.assertEqual(self.saved_psyche()[section][key], value)

    def test_core_tool_uses_psyche_owner(self):
        reply = immediate(self.core.shihai_tune(None, "E", "0.92"))
        self.assertIn("立即生效", reply)
        self.assertEqual(self.psyche.state["psi"]["E"], 0.92)
        self.assertEqual(self.saved_psyche()["psi"]["E"], 0.92)
        self.tick_twice()
        self.assertEqual(self.core.engine.state.psi["E"], 0.92)

    def test_psyche_tool_updates_core_and_expression_source_immediately(self):
        for key, core_key, value in (("E", "E", 0.89), ("att", "send_threshold", 2.3),
                                     ("tantrum", "tantrum_threshold", 1.2), ("eff", "eff", 0.88)):
            reply = immediate(self.psyche.psyche_tune(None, key, str(value)))
            self.assertIn("立即生效", reply)
            self.assertEqual(self.core._param_value(core_key), value)
        guard = self.guard_module.ShihaiGuardPlugin(self.context, Config())
        self.assertEqual(guard._psi()["E"], 0.89)
        snapshot = json.loads(Path(self.core.state_path).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["psi"]["E"], 0.89)

    def test_parameter_panel_reads_effective_values(self):
        self.web({"op": "tune", "key": "E", "value": 0.94})
        self.assertEqual(self.core.config["E"], 0.6)  # 旧核心配置不再是人格权威来源。
        result, status = self.web({"op": "params"})
        self.assertEqual(status, 200)
        self.assertEqual(next(row["value"] for row in result["params"] if row["key"] == "E"), 0.94)

    def test_immediate_application_does_not_depend_on_rereading_file(self):
        with patch.object(self.core.engine, "sync_psyche"):
            result, status = self.web({"op": "tune", "key": "E", "value": 0.93})
            self.assertEqual(status, 200)
            self.assertEqual(result["state"]["psi"]["E"], 0.93)
            immediate(self.psyche.psyche_tune(None, "att", "2.2"))
            self.assertEqual(self.core.engine.state.theta["send_threshold"], 2.2)

    def test_restart_reads_psyche_before_first_tick(self):
        self.web({"op": "tune", "key": "E", "value": 0.95})
        self.core = self.core_module.ShihaiPlugin(self.context, Config({"E": 0.1}))
        self.context.add("astrbot_plugin_shihai", self.core)
        self.assertEqual(self.core.engine.state.psi["E"], 0.95)
        self.psyche = self.psyche_module.PsychePlugin(self.context, Config({"E_init": 0.2}))
        self.context.add("astrbot_plugin_shihai_psyche", self.psyche)
        self.tick_twice()
        self.assertEqual(self.core.engine.state.psi["E"], 0.95)
        self.assertEqual(self.psyche.state["psi"]["E"], 0.95)

    def test_disabled_psyche_with_old_file_rejects_tuning(self):
        self.context.stars["astrbot_plugin_shihai_psyche"].activated = False
        before = self.saved_psyche()
        result, status = self.web({"op": "tune", "key": "E", "value": 0.99})
        self.assertEqual(status, 502)
        self.assertIn("先启用识海心理插件", result["message"])
        self.assertEqual(self.saved_psyche(), before)
        self.assertEqual(self.core.engine.state.psi["E"], 0.6)
        self.assertIn("调参失败", immediate(self.core.shihai_tune(None, "E", "0.99")))

    def test_unloaded_psyche_with_old_file_rejects_tuning(self):
        self.context.stars.pop("astrbot_plugin_shihai_psyche")
        result, status = self.web({"op": "tune", "key": "E", "value": 0.99})
        self.assertEqual(status, 502)
        self.assertIn("先启用识海心理插件", result["message"])

    def test_old_psyche_version_requests_upgrade(self):
        self.context.stars["astrbot_plugin_shihai_psyche"].star_cls = object()
        result, status = self.web({"op": "tune", "key": "E", "value": 0.99})
        self.assertEqual(status, 502)
        self.assertIn("同时更新识海心理插件", result["message"])

    def test_standalone_core_keeps_working_without_psyche_file(self):
        self.context.stars.pop("astrbot_plugin_shihai_psyche")
        Path(self.psyche.state_path).unlink()
        result, status = self.web({"op": "tune", "key": "E", "value": 0.73})
        self.assertEqual(status, 200)
        self.assertEqual(result["value"], 0.73)
        self.tick_twice()
        restored = self.core_module.ShihaiPlugin(self.context, Config(self.core.config.saved))
        self.assertEqual(restored.engine.state.psi["E"], 0.73)
        self.assertFalse(Path(self.psyche.state_path).exists())

    def test_failed_psyche_save_preserves_memory_file_and_returns_error(self):
        before = copy.deepcopy(self.psyche.state)
        with patch.object(self.psyche_module.profile.os, "replace",
                          side_effect=PermissionError("模拟心理状态写入失败")):
            result, status = self.web({"op": "tune", "key": "E", "value": 0.99})
            self.assertEqual(status, 502)
            self.assertIn("写入失败", result["message"])
            reply = immediate(self.psyche.psyche_tune(None, "E", "0.99"))
            self.assertIn("调参失败", reply)
        self.assertEqual(self.psyche.state, before)
        self.assertEqual(self.saved_psyche(), before)
        self.assertEqual(self.core.engine.state.psi["E"], 0.6)
        self.assertFalse(list(self.data_root.rglob("*.tmp")))

    def test_failed_core_config_save_does_not_apply_value(self):
        self.core.config.fail_save = True
        before = self.core.engine.emotion.tau["happy"]
        result, status = self.web({"op": "tune", "key": "tau_happy", "value": 12})
        self.assertEqual(status, 502)
        self.assertIn("配置写入失败", result["message"])
        self.assertEqual(self.core.engine.emotion.tau["happy"], before)
        self.assertNotIn("tau_happy", self.core.config)

    def test_nonfinite_values_are_rejected_without_changing_state(self):
        before = self.saved_psyche()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                result, status = self.web({"op": "tune", "key": "E", "value": value})
                self.assertEqual(status, 400)
                self.assertIn("有限数字", result["message"])
                self.assertIn("调参失败", immediate(self.core.shihai_tune(None, "E", str(value))))
        self.assertEqual(self.saved_psyche(), before)

    def test_response_reports_clamped_value(self):
        result, status = self.web({"op": "tune", "key": "E", "value": 9})
        self.assertEqual(status, 200)
        self.assertEqual(result["value"], 1)
        self.assertEqual(result["state"]["psi"]["E"], 1)
        self.assertEqual(self.saved_psyche()["psi"]["E"], 1)

    def test_dynamics_and_goal_tuning_do_not_write_psyche_file(self):
        before = self.saved_psyche()
        self.web({"op": "tune", "key": "tau_happy", "value": 12})
        self.web({"op": "tune", "key": "g_memory", "value": 0.9})
        self.assertEqual(self.core.engine.emotion.tau["happy"], 12)
        self.assertEqual(self.core.engine.state.goals["g_memory"], 0.9)
        self.tick_twice()
        self.assertNotEqual(self.core.engine.state.goals["g_memory"], 0.9)
        self.assertEqual(self.saved_psyche(), before)


if __name__ == "__main__":
    unittest.main()
