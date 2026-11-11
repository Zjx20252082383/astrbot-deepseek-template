# -*- coding: utf-8 -*-
"""把 livingmemory conversations.db 的逐字 messages 迁移到 transcript.db。

幂等：用原表稳定 id 生成 msg_id（lm-<id>），重复运行不会产生重复行。
用法：
  python migrate_livingmemory_transcript.py --dry-run   # 只预览
  python migrate_livingmemory_transcript.py             # 实际迁移
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号")
PLUGIN = ROOT / "data" / "plugins" / "astrbot_plugin_shihai_behavior"
SRC = ROOT / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "conversations.db"
DST = ROOT / "data" / "plugin_data" / "astrbot_plugin_qq_toolbox" / "memory_v2" / "transcript.db"

sys.path.insert(0, str(PLUGIN))
from modules.transcript_store import TranscriptStore  # noqa: E402


def read_rows():
    con = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, session_id, role, content, sender_id, sender_name,"
        " group_id, timestamp FROM messages ORDER BY id").fetchall()
    con.close()
    return rows


def to_item(r):
    ts = float(r["timestamp"]) if r["timestamp"] is not None else None
    return {
        "role": r["role"],
        "content": r["content"] or "",
        "ts": ts,
        "origin": r["session_id"],
        "sender_id": r["sender_id"],
        "sender_name": r["sender_name"],
        "group_id": r["group_id"],
        "kind": "text",
        # 原表稳定 id 作幂等键；同会话同时间戳的 user/assistant 归为一轮
        "msg_id": f"lm-{r['id']}",
        "turn_id": f"{r['session_id']}#{r['timestamp']}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = read_rows()
    items = [to_item(r) for r in rows]
    nonempty = [it for it in items if it["content"].strip()]
    print(f"源表 messages：{len(rows)} 条；有效非空：{len(nonempty)} 条")
    ts_values = [it["ts"] for it in nonempty if it["ts"]]
    if ts_values:
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
        print(f"时间范围：{fmt(min(ts_values))} 至 {fmt(max(ts_values))}")

    if args.dry_run:
        print("[dry-run] 未写入。目标库：", DST)
        return 0

    DST.parent.mkdir(parents=True, exist_ok=True)
    store = TranscriptStore(DST)
    before = store.count()
    added = store.append_many(nonempty)
    after = store.count()
    print(f"迁移前 {before} 条；本次新增 {added} 条；迁移后 {after} 条")

    # 验证：抽样检索一条真实内容
    sample_kw = nonempty[0]["content"][:6]
    hits = store.search(sample_kw, limit=1)
    print("抽样检索（关键词 %r）：%s" %
          (sample_kw, "命中" if hits else "未命中"))
    store.close()
    print("迁移完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
