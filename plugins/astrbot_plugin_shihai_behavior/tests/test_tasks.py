"""固定时间与模拟 QQ 发送的生活任务回归测试。"""

import ast
import json
import runpy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


PLUGIN = Path(__file__).resolve().parents[1]
TaskEngine = runpy.run_path(str(PLUGIN / "behavior" / "tasks.py"))["TaskEngine"]


def at(hour, minute=0, day=20):
    return datetime(2026, 11, day, hour, minute).timestamp()


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("模拟发送不应等待外部 I/O")


class TaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="shihai-task-test-", dir=PLUGIN.parents[1])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sms = self.root / "sms.json"
        self.sent = []
        self.engine = TaskEngine(self.root, send_text=self.send, sms_records_path=self.sms)

    async def send(self, text):
        self.sent.append(text)
        return True

    def task(self, serial=1):
        records = json.loads(self.sms.read_text(encoding="utf-8")) if self.sms.exists() else []
        records.append({"dedup": str(serial), "ts": at(14, serial),
                        "express": {"carrier": "测试快递"}})
        self.sms.write_text(json.dumps(records), encoding="utf-8")
        return self.engine.ingest_sms(at(15))[0]

    def test_sms_to_archive_via_qq_replies(self):
        task = self.task()
        task_id = task["id"]
        self.assertEqual(immediate(self.engine.tick(at(15))), [task_id])
        self.assertIn(f"确认 {task_id}", self.sent[-1])
        reply = self.engine.handle_text("确认", at(15))
        self.assertIn("16:00 出发（预计 16:08 到达）", reply)
        self.assertEqual(task["remind_at"], at(16))
        self.assertEqual(immediate(self.engine.tick(at(15, 59))), [])
        self.assertEqual(immediate(self.engine.tick(at(16))), [task_id])
        self.assertIn(f"已取 {task_id}", self.sent[-1])
        self.assertEqual(immediate(self.engine.tick(at(16, 1))), [])
        self.assertIn("已归档", self.engine.handle_text(f"已取 {task_id}", at(16, 9)))
        self.assertEqual(TaskEngine(self.root).store.find(task_id)["status"], "archived")

    def test_arrival_at_or_after_closing_cannot_be_scheduled(self):
        task = self.task()
        for now in (at(17, 52), at(17, 56), at(18, 10)):
            with self.subTest(now=now):
                reply = self.engine.handle_text(f"确认 {task['id']}", now)
                self.assertIn("今天已来不及", reply)
                self.assertEqual(task["status"], "expired")
                self.assertIsNone(task["remind_at"])
        self.engine.handle_text(f"确认 {task['id']}", at(15, day=21))
        self.assertEqual(task["remind_at"], at(16, day=21))

    def test_class_ending_after_closing_does_not_roll_closing_to_tomorrow(self):
        task = self.task()
        self.engine.set_context(class_end="19:00")
        self.assertIn("今天已来不及", self.engine.handle_text("确认", at(15)))
        self.assertEqual(task["status"], "expired")
        self.assertEqual(task["window_end"], at(18))

    def test_missed_reminder_is_not_sent_when_arrival_is_no_longer_possible(self):
        for now in (at(17, 56), at(18, 10)):
            with self.subTest(now=now):
                task = self.task(serial=len(self.engine.store.tasks) + 1)
                self.engine.handle_text("确认", at(15))
                self.assertEqual(immediate(self.engine.tick(now)), [])
                self.assertEqual(task["status"], "expired")
        self.assertEqual(self.sent, [])

    def test_defer_cannot_schedule_a_reminder_past_the_pickup_window(self):
        task = self.task()
        self.engine.handle_text("确认", at(15))
        self.assertIn("16:30", self.engine.handle_text("延后", at(15, 30)))
        self.assertEqual(task["remind_at"], at(16, 30))
        self.assertIn("今天已来不及", self.engine.handle_text("延后", at(17, 30)))
        self.assertEqual(immediate(self.engine.tick(at(18, 30))), [])
        self.assertEqual(task["status"], "expired")

    def test_legacy_plan_is_rechecked_before_sending_after_restart(self):
        task = self.task()
        task.update(status="planned", window_start=at(15, 50),
                    window_end=at(18), remind_at=at(15, 40))
        self.engine.store.save()
        restored = TaskEngine(self.root, send_text=self.send, sms_records_path=self.sms)
        self.assertEqual(immediate(restored.tick(at(15, 50))), [])
        self.assertEqual(immediate(restored.tick(at(16))), [task["id"]])
        self.assertIn("16:00 出发", self.sent[-1])

    def test_legacy_defer_without_deadline_expires_on_its_original_day(self):
        task = self.task()
        task.update(status="deferred", window_end=None, remind_at=at(18, 30))
        self.assertEqual(immediate(self.engine.tick(at(16, day=21))), [])
        self.assertEqual(task["status"], "expired")

    def test_everyday_messages_do_not_change_tasks(self):
        task = self.task()
        for message in ("这份作业完成了", "今天考试不行", "收到", "报告晚点完成",
                        "还没取快递", "快递已取了吗？", "不是忽略快递"):
            with self.subTest(message=message):
                self.assertIsNone(self.engine.handle_text(message, at(15)))
        self.assertEqual(task["status"], "pending_confirm")

    def test_multiple_tasks_require_the_id_written_by_the_user(self):
        first = self.task()
        second = self.task(serial=2)
        for reply in ("确认", "已取", "忽略", "延后"):
            self.assertIn("有多条待办", self.engine.handle_text(reply, at(15)))
        self.assertIsNone(self.engine.handle_text(
            f"已取 {first['id']}", at(15), expected_action="done",
            expected_task_id=second["id"]))
        self.assertEqual(first["status"], "pending_confirm")
        self.assertEqual(second["status"], "pending_confirm")
        self.engine.handle_text(f"已取 {first['id']}！", at(15))
        self.assertEqual(first["status"], "archived")
        self.assertEqual(second["status"], "pending_confirm")
        self.engine.handle_text(f"已取 {first['id']}", at(15))
        self.assertEqual(second["status"], "pending_confirm")

    def test_inflight_reminder_does_not_undo_a_user_receipt(self):
        task = self.task()
        self.engine.handle_text("确认", at(15))

        async def acknowledge_during_send(_):
            self.engine.handle_text(f"已取 {task['id']}", at(16))
            return True

        self.engine.send_text = acknowledge_during_send
        self.assertEqual(immediate(self.engine.tick(at(16))), [task["id"]])
        self.assertEqual(task["status"], "archived")

    def test_plugin_handlers_check_the_original_private_message(self):
        source = PLUGIN / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        names = {"_is_owner_private_chat", "_apply_task_tool", "task_observe", "task_done"}
        methods = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                node.decorator_list = []
                methods.append(node)
        namespace = {"AstrMessageEvent": object}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
        handler = type("TaskHandlers", (), {name: namespace[name] for name in names})()
        handler.owner_qid = "owner"
        handler.task_engine = self.engine
        task = self.task()

        async def replies(event):
            return [reply async for reply in handler.task_observe(event)]

        event = SimpleNamespace(message_str="这份作业完成了",
                                get_sender_id=lambda: "owner", is_private_chat=lambda: True,
                                plain_result=lambda reply: SimpleNamespace(stop_event=lambda: reply))
        self.assertEqual(immediate(replies(event)), [])
        self.assertIn("明确回执", immediate(handler.task_done(event, task["id"])))
        event.message_str = f"已取 {task['id']}"
        event.is_private_chat = lambda: False
        self.assertEqual(immediate(replies(event)), [])
        self.assertIn("主人的私聊", immediate(handler.task_done(event, task["id"])))
        self.assertEqual(task["status"], "pending_confirm")
        event.is_private_chat = lambda: True
        event.get_sender_id = lambda: "guest"
        self.assertEqual(immediate(replies(event)), [])
        self.assertIn("主人的私聊", immediate(handler.task_done(event, task["id"])))
        self.assertEqual(task["status"], "pending_confirm")
        event.get_sender_id = lambda: "owner"
        self.assertIn("已归档", immediate(handler.task_done(event, task["id"])))
        event.message_str = f"忽略 {self.task(serial=2)['id']}"
        self.assertIn("已忽略", immediate(replies(event))[0])


if __name__ == "__main__":
    unittest.main()
