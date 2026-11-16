"""只测试代理函数，不加载本地配置、不登录、不启动 HTTP 服务。

运行：python -B -m unittest discover -s front_end/tests -v
"""

import ast
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock


class ProxyTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "serve.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "call_plugin")
        self.auth = Mock(return_value="test-token")
        self.http = Mock()
        namespace = {"json": json, "urllib": urllib, "ASTRBOT": "http://unused.invalid",
                     "PLUGIN": "astrbot_plugin_shihai", "ensure_token": self.auth,
                     "_http_json": self.http}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        self.call_plugin = namespace["call_plugin"]

    @staticmethod
    def error(code, content):
        return urllib.error.HTTPError("http://unused.invalid", code, "test", {}, io.BytesIO(content))

    def test_structured_errors_reach_the_page_without_escaped_json(self):
        payload = {"status": "error", "message": "存在心理状态文件，请先启用识海心理插件"}
        self.http.side_effect = self.error(502, json.dumps(payload).encode())
        status, result = self.call_plugin("POST", "cmd", {"op": "tune", "key": "E", "value": 0.9})
        self.assertEqual(status, 502)
        self.assertEqual(result, payload)
        self.assertIn("先启用识海心理插件", result["message"])
        self.auth.assert_called_once_with(force=False)

    def test_unauthorized_request_still_refreshes_token_once(self):
        self.http.side_effect = [self.error(401, b'{}'), (200, {"status": "ok"})]
        self.assertEqual(self.call_plugin("GET", "state"), (200, {"status": "ok"}))
        self.assertEqual(self.auth.call_count, 2)
        self.auth.assert_called_with(force=True)

    def test_non_json_failure_remains_an_error(self):
        self.http.side_effect = self.error(502, b'<html>upstream failed</html>')
        with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
            self.call_plugin("POST", "cmd", {})


if __name__ == "__main__":
    unittest.main()
