#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性隔离脚本 —— 断开"编造写回图谱 → 下轮又被召回 → 继续编"的闭环

做什么：
  1. 把 2026-11-11 11:58 那条编造事件（event:1794369495590）从识海图谱里移出，
     并一并清掉它的 16 条关联边（不清理会留下悬空边，占 max_edges 名额）。
  2. 把"语音转写可能残缺、不要补全推测"的约束写进插件配置 voice.listen.listen_hint。

不做什么：
  · 不启动、不停止任何进程（启停由你自己来）。
  · 不动 data_v4.db 里的会话历史（420 KB JSON 结构体，改错一个字符整段历史就没了；
    上下文是滑动窗口，那几句会自然滚出去）。
  · 不动 event_snapshot.json（那是 memory_v2 的 EventBank，467 条里并没有这条，
    归档机制 event_archive.json 对它不适用）。

安全设计：
  · 运行前校验图谱文件的最后写入时间：若 120 秒内刚被写过，说明机器人还在跑，
    直接拒绝执行（因为进程内存里的 nodes 会在下次 save 时把它写回来）。
  · 改动前把图谱与配置各备份一份到 backups/shihai_graph_pre_voice_fix/，
    备份目录不带日期；若备份已存在则拒绝重复执行，避免覆盖第一次的原始状态。
  · 图谱写回使用与原文件一致的紧凑序列化（json.dumps(..., ensure_ascii=False)），
    与 mind/graph.py 的 save() 保持同一格式；其余 952 个节点字段原样保留。
  · 配置以 utf-8-sig 读写，保留原有 UTF-8 BOM 不被去掉。

回滚：把 backups/shihai_graph_pre_voice_fix/ 下的两个 .before.json 覆盖回原路径即可。
"""

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号")
GRAPH = ROOT / "data" / "plugin_data" / "shihai" / "shihai_graph.json"
CONF = ROOT / "data" / "config" / "astrbot_plugin_shihai_behavior_config.json"
BAK_DIR = ROOT / "backups" / "shihai_graph_pre_voice_fix"

TARGET = "event:1794369495590"
STALE_SECONDS = 120

HINT = (
    "（以下是用户语音的转写，可能不完整或有误）"
    "请只依据上述转写内容回应；若信息不足以确定用户意思，直接问用户确认。"
    "不要推测、不要补全，也不要添加用户未提及的任何事实——"
    "时间、数字、报告、约定这类具体事项尤其不要凭空说。"
    "转写里出现「[此处转写缺失]」标记时，表示那一段语音没有被识别出来："
    "就那一段向用户确认，不要猜它说了什么，也不要把它当作用户真说过的话。"
    "如果无法确认，宁可只回应你确实听到的部分，并向用户确认。"
)


def fail(msg: str) -> None:
    print(f"[中止] {msg}")
    sys.exit(1)


def main() -> None:
    print("=" * 62)
    print("识海 · 编造事件隔离 + 语音约束写入")
    print("=" * 62)

    if not GRAPH.exists():
        fail(f"找不到图谱文件：{GRAPH}")
    if not CONF.exists():
        fail(f"找不到配置文件：{CONF}")

    # ---- 1) 进程存活检查 ----
    idle = time.time() - GRAPH.stat().st_mtime
    print(f"图谱最后写入：{time.strftime('%H:%M:%S', time.localtime(GRAPH.stat().st_mtime))}"
          f"（{int(idle)} 秒前）")
    if idle < STALE_SECONDS:
        fail(f"图谱 {STALE_SECONDS} 秒内刚被写过，机器人很可能还在运行。\n"
             f"       请先停止机器人，等它完全退出后再跑本脚本。")

    # ---- 2) 备份（不覆盖已有备份）----
    if (BAK_DIR / "shihai_graph.before.json").exists():
        fail(f"备份已存在：{BAK_DIR}\n"
             f"       说明本脚本可能已经跑过一次。确认要重跑的话，"
             f"先把该备份目录挪走（它是唯一可回滚的原始状态）。")
    BAK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GRAPH, BAK_DIR / "shihai_graph.before.json")
    shutil.copy2(CONF, BAK_DIR / "config.before.json")
    print(f"[备份] {BAK_DIR}")
    print(f"        shihai_graph.before.json  {GRAPH.stat().st_size:,} 字节")
    print(f"        config.before.json        {CONF.stat().st_size:,} 字节")

    # ---- 3) 从图谱移除节点 + 关联边 ----
    raw = json.loads(GRAPH.read_text(encoding="utf-8"))
    nodes = raw.get("nodes", {}) or {}
    edges = raw.get("edges", []) or []

    node = nodes.get(TARGET)
    if node is None:
        fail(f"图谱里没有 {TARGET}，可能已处理过，未做任何修改（备份保留在原地）。")

    print("-" * 62)
    print("将移除的节点：")
    print(f"  id      : {TARGET}")
    print(f"  content : {str(node.get('content', ''))[:110]}")
    print(f"  created : {time.strftime('%m-%d %H:%M', time.localtime(float(node.get('created', 0))))}")

    edges_before = len(edges)
    dropped = [e for e in edges if TARGET in (e[0], e[1])]
    edges_after = [e for e in edges if TARGET not in (e[0], e[1])]

    nodes.pop(TARGET, None)
    anchors = raw.get("used_anchors") or {}
    anchors.pop(TARGET, None)
    raw["nodes"] = nodes
    raw["edges"] = edges_after
    raw["used_anchors"] = anchors

    # 与 mind/graph.py save() 同格式：紧凑、无 indent、关 ASCII 转义
    tmp = GRAPH.with_suffix(GRAPH.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    tmp.replace(GRAPH)

    print("-" * 62)
    print(f"[图谱] 节点 953 → {len(nodes)}")
    print(f"[图谱] 边 {edges_before} → {len(edges_after)}（移除与目标相关的 {len(dropped)} 条）")
    print(f"[图谱] 文件 {GRAPH.stat().st_size:,} 字节")

    # ---- 4) 写入语音约束（保留 BOM）----
    raw_cfg = CONF.read_bytes()
    had_bom = raw_cfg[:3] == b"\xef\xbb\xbf"
    cfg = json.loads(raw_cfg.decode("utf-8-sig"))
    voice = cfg.setdefault("voice", {})
    listen = voice.setdefault("listen", {})
    old = listen.get("listen_hint", "")
    listen["listen_hint"] = HINT

    CONF.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8-sig" if had_bom else "utf-8",
    )
    print("-" * 62)
    print(f"[配置] voice.listen.listen_hint")
    print(f"        原值：{old!r}")
    print(f"        新值：{HINT[:40]}…（共 {len(HINT)} 字）")
    print(f"[配置] BOM {'保留' if had_bom else '原本就没有'}；文件 {CONF.stat().st_size:,} 字节")

    # ---- 5) 收尾提示 ----
    print("=" * 62)
    print("完成。接下来由你操作：")
    print("  1. 启动机器人（启动日志里应能看到 [memoryv2][exp] 生效组=… 这行）；")
    print("  2. 用同一段对话重放一遍（先说一半、让语音转写残缺），确认她不再编；")
    print("  3. 若要回滚，把下面两个文件覆盖回原路径：")
    print(f"       {BAK_DIR / 'shihai_graph.before.json'}  ->  {GRAPH}")
    print(f"       {BAK_DIR / 'config.before.json'}        ->  {CONF}")
    print("=" * 62)


if __name__ == "__main__":
    main()
