# -*- coding: utf-8 -*-
"""TranscriptStore 单元测试（临时库，不触碰真实数据）。

运行：python test_transcript_store.py
"""
import sys
import tempfile
import time
from pathlib import Path

# 让独立运行时能 import 插件 modules
PLUGIN = (Path(__file__).resolve().parents[1] / "data" / "plugins" / "astrbot_plugin_shihai_behavior")
sys.path.insert(0, str(PLUGIN))

from modules.transcript_store import TranscriptStore  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ts_test_")) / "t.db"
    store = TranscriptStore(tmp)
    failures = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), "-", name)
        if not cond:
            failures.append(name)

    base = time.time()
    tid = "turn-1"
    i1 = store.append(role="user", content="明天下午三点我们去星海广场看日落",
                      origin="qq:FriendMessage:123", sender_id="123",
                      sender_name="鑫鑫", msg_id="m1", turn_id=tid, ts=base)
    i2 = store.append(role="assistant", content="好呀，我记住啦，明天一起去看日落",
                      origin="qq:FriendMessage:123", sender_id="999",
                      sender_name="小满", msg_id="m2", turn_id=tid, ts=base + 1)
    check("插入返回自增 id", isinstance(i1, int) and i2 == i1 + 1)

    # 去重：相同 msg_id 应返回 None，不新增行
    dup = store.append(role="user", content="重复消息",
                       msg_id="m1", ts=base + 2)
    check("msg_id 去重返回 None", dup is None)
    check("去重后总数仍为 2", store.count() == 2)

    # 空内容拒绝
    check("空内容返回 None", store.append(role="user", content="   ") is None)

    # 中文检索：FTS/LIKE 任一通道命中即可
    hits = store.search("星海广场", limit=5)
    check("中文关键词检索命中", len(hits) == 1 and "星海广场" in hits[0]["content"])

    # 部分词（二元组）
    hits2 = store.search("日落")
    check("共同关键词命中两条", len(hits2) == 2)

    # 角色过滤
    only_user = store.search("日落", role="user")
    check("role 过滤只留 user", len(only_user) == 1 and only_user[0]["role"] == "user")

    # 时间下界
    hits3 = store.search("日落", since=base + 5)
    check("since 过滤无结果", hits3 == [])

    # 取整轮
    turn = store.get_turn(tid)
    check("get_turn 按序返回两条", len(turn) == 2
          and turn[0]["role"] == "user" and turn[1]["role"] == "assistant")

    # 时间区间
    rng = store.get_range(base, base + 1)
    check("get_range 命中两条", len(rng) == 2)

    # 片段截断
    store.append(role="user", content="x" * 500, msg_id="m3", ts=base + 3)
    snip = store.search("x" * 500, snippet=50)
    check("长文本片段截断", len(snip) == 1 and len(snip[0]["content"]) <= 51)

    check("latest_ts 正确", abs(store.latest_ts() - (base + 3)) < 1e-6)

    # 持久化：重新打开同一文件，数据仍在
    store.close()
    store2 = TranscriptStore(tmp)
    check("重开后数据持久（3 条）", store2.count() == 3)
    store2.close()

    print("\n结果：%s，失败 %d 项" %
          ("全部通过" if not failures else "存在失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
