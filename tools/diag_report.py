# -*- coding: utf-8 -*-
"""统计 [diag] 日志：回答"钱花在哪、原文全不全、她看了什么"。
用法：  python tools\diag_report.py [日志路径]
"""
import re
import sys
import pathlib
from statistics import median

LOG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                   r"E:\Users\zjx\Desktop\deepseek接QQ小号\data\logs\astrbot.log")

ROW = re.compile(
    r"sys_chars=(\d+) ctx_msgs=(\d+) ctx_chars=(\d+) "
    r"tool_n=(\d+) tool_chars=(\d+) trunc=(\d+) empty=(\d+)"
)


def main():
    if not LOG.exists():
        print(f"日志不存在: {LOG}")
        return
    rows = []
    with LOG.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = ROW.search(line)
            if m:
                rows.append(tuple(int(x) for x in m.groups()))

    if not rows:
        print("没有 [diag] 记录。插件可能还没重载，或这段时间没有请求。")
        return

    n = len(rows)
    sys_c = [r[0] for r in rows]
    msgs = [r[1] for r in rows]
    ctx_c = [r[2] for r in rows]
    tool_n = [r[3] for r in rows]
    tool_c = [r[4] for r in rows]
    trunc = [r[5] for r in rows]
    empty = [r[6] for r in rows]

    print(f"样本请求数: {n}\n")
    print("── 总量 ──")
    print(f"  system_prompt   中位 {median(sys_c):>7,.0f} 字符")
    print(f"  contexts        中位 {median(msgs):>7.0f} 条 / {median(ctx_c):>7,.0f} 字符")
    print(f"  工具定义        中位 {median(tool_n):>7.0f} 个 / {median(tool_c):>7,.0f} 字符")

    tot = median(sys_c) + median(ctx_c) + median(tool_c)
    if tot:
        print("\n── 三个数 ──")
        print(f"  ① 工具定义占比   {median(tool_c)/tot:6.1%}   ← 纯开销，对记忆零贡献")
        print(f"  ② 截断覆盖率     {sum(trunc)/max(1,sum(msgs)):6.1%}   ← 带省略标记的消息 / 总消息")
        print(f"  ③ 空内容占比     {sum(empty)/max(1,sum(msgs)):6.1%}   ← 空白消息占了多少窗口位")

    print("\n── 分布 ──")
    for label, arr, unit in (("ctx 消息条数", msgs, "条"), ("工具个数", tool_n, "个")):
        lo, hi = min(arr), max(arr)
        print(f"  {label}: 最小 {lo}{unit} / 中位 {median(arr):.0f}{unit} / 最大 {hi}{unit}")

    big = sum(1 for m in msgs if m > 100)
    print(f"\n  超过 100 条消息的请求: {big} 次 / {n} 次")


if __name__ == "__main__":
    main()
