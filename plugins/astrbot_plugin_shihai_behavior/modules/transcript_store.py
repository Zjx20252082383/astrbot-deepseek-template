"""逐字对话归档存储（方案 A）。

独立于 memory_v2 的 JSON 摘要层：每轮 user / assistant 逐字原文持续写入
本地 SQLite（WAL、append-only），并通过 FTS5 + 字符二元组提供按需检索。

设计约束：
- 只存文本，不存图片/音频二进制；
- 写入是本地操作、不产生任何 API 调用；
- 内容不进入 LLM 固定前缀，故不影响前缀缓存命中率；
- 单文件 *.db 已被 .gitignore 排除，不入版本库。

本模块的同步方法均为毫秒级本地操作；在异步上下文中请用 ``asyncio.to_thread``
包裹，避免阻塞事件循环。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

# 单条检索结果的返回上限与片段长度（防止一次取回过多文本）
DEFAULT_LIMIT = 5
DEFAULT_SNIPPET = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  origin      TEXT,
  role        TEXT NOT NULL,
  sender_id   TEXT,
  sender_name TEXT,
  group_id    TEXT,
  content     TEXT NOT NULL,
  kind        TEXT DEFAULT 'text',
  msg_id      TEXT,
  turn_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_origin_ts ON transcript(origin, ts);
CREATE INDEX IF NOT EXISTS idx_ts ON transcript(ts);
CREATE UNIQUE INDEX IF NOT EXISTS uq_msg
  ON transcript(msg_id) WHERE msg_id IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  content, origin, content='transcript', content_rowid='id',
  tokenize='unicode61');

CREATE TRIGGER IF NOT EXISTS tr_ai AFTER INSERT ON transcript BEGIN
  INSERT INTO transcript_fts(rowid, content, origin)
  VALUES (new.id, new.content, new.origin);
END;
CREATE TRIGGER IF NOT EXISTS tr_ad AFTER DELETE ON transcript BEGIN
  INSERT INTO transcript_fts(transcript_fts, rowid, content, origin)
  VALUES ('delete', old.id, old.content, old.origin);
END;
CREATE TRIGGER IF NOT EXISTS tr_au AFTER UPDATE ON transcript BEGIN
  INSERT INTO transcript_fts(transcript_fts, rowid, content, origin)
  VALUES ('delete', old.id, old.content, old.origin);
  INSERT INTO transcript_fts(rowid, content, origin)
  VALUES (new.id, new.content, new.origin);
END;
"""


def _bigrams(text: str) -> list[str]:
    """字符二元组（含单字兜底），用于中文模糊匹配。"""
    t = "".join(str(text or "").split())
    if not t:
        return []
    if len(t) == 1:
        return [t]
    return [t[i:i + 2] for i in range(len(t) - 1)]


class TranscriptStore:
    """逐字归档库的同步封装。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------- 写入 --------------------

    def append(self, *, role: str, content: str,
               ts: float | None = None, origin: str | None = None,
               sender_id: str | None = None, sender_name: str | None = None,
               group_id: str | None = None, kind: str = "text",
               msg_id: str | None = None, turn_id: str | None = None) -> int | None:
        """插入一条逐字消息；带 msg_id 时按其去重，返回新行 id（重复返回 None）。"""
        body = str(content or "").strip()
        if not body:
            return None
        when = float(ts) if ts is not None else time.time()
        with self._lock:
            if msg_id:
                row = self._conn.execute(
                    "SELECT id FROM transcript WHERE msg_id=? LIMIT 1",
                    (msg_id,)).fetchone()
                if row is not None:
                    return None
            cur = self._conn.execute(
                "INSERT INTO transcript(ts, origin, role, sender_id, sender_name,"
                " group_id, content, kind, msg_id, turn_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (when, origin, str(role), sender_id, sender_name, group_id,
                 body, kind, msg_id, turn_id))
            return int(cur.lastrowid)

    # -------------------- 读取 --------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in row.keys()}

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM transcript").fetchone()[0])

    def latest_ts(self) -> float:
        with self._lock:
            v = self._conn.execute(
                "SELECT MAX(ts) FROM transcript").fetchone()[0]
        return float(v or 0.0)

    def get_turn(self, turn_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transcript WHERE turn_id=? ORDER BY ts, id",
                (turn_id,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_range(self, t0: float, t1: float,
                  origin: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM transcript WHERE ts>=? AND ts<=?"
        args: list[Any] = [t0, t1]
        if origin:
            sql += " AND origin=?"
            args.append(origin)
        sql += " ORDER BY ts, id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search(self, keyword: str, *, origin: str | None = None,
               since: float | None = None, role: str | None = None,
               limit: int = DEFAULT_LIMIT,
               snippet: int = DEFAULT_SNIPPET) -> list[dict[str, Any]]:
        """按关键词检索逐字片段。

        FTS5 与 LIKE 双通道合并去重：FTS5 负责分词命中，LIKE 负责中文整串
        兜底；再按 origin / since / role 过滤，按时间倒序返回。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return []
        ids: set[int] = set()
        with self._lock:
            # 通道一：FTS5（把关键词拆成二元组/单字，OR 连接）
            try:
                terms = _bigrams(kw) or [kw]
                match = " OR ".join(t.replace('"', '""') for t in terms)
                rows = self._conn.execute(
                    "SELECT rowid FROM transcript_fts WHERE transcript_fts MATCH ?",
                    (match,)).fetchall()
                ids.update(int(r[0]) for r in rows)
            except sqlite3.OperationalError:
                pass
            # 通道二：LIKE 整串兜底
            rows = self._conn.execute(
                "SELECT id FROM transcript WHERE content LIKE ?",
                (f"%{kw}%",)).fetchall()
            ids.update(int(r[0]) for r in rows)

            if not ids:
                return []
            sql = "SELECT * FROM transcript WHERE id IN (%s)" % (
                ",".join("?" * len(ids)))
            args: list[Any] = list(ids)
            if origin:
                sql += " AND origin=?"
                args.append(origin)
            if since is not None:
                sql += " AND ts>=?"
                args.append(since)
            if role:
                sql += " AND role=?"
                args.append(role)
            sql += " ORDER BY ts DESC, id DESC LIMIT ?"
            args.append(max(1, int(limit)))
            rows = self._conn.execute(sql, args).fetchall()

        result = []
        for r in rows:
            d = self._row_to_dict(r)
            if snippet and len(d.get("content", "")) > snippet:
                d["content"] = d["content"][:snippet] + "…"
            result.append(d)
        return result

    # -------------------- 迁移辅助 --------------------

    def append_many(self, items: Iterable[dict[str, Any]]) -> int:
        """批量插入（迁移用），逐条复用 append 的去重逻辑，返回新增条数。"""
        n = 0
        for it in items:
            if self.append(**it) is not None:
                n += 1
        return n
