# -*- coding: utf-8 -*-
"""为识海_合并稿生成彩色图表：艾宾浩斯曲线、三桶对比、复习加固、架构与数据流。"""
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 150

OUT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号\整合分析_20261111\figures")
OUT.mkdir(parents=True, exist_ok=True)

BLUE, GREEN, ORANGE, PURPLE, RED, GRAY = (
    "#2b6cb0", "#2f9e6e", "#dd8a3e", "#7d5ba6", "#c0504d", "#8a94a0")


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUT / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("saved", name)


# ---------- 图1：经典艾宾浩斯遗忘曲线 ----------
def fig_classic():
    t = np.linspace(0, 10, 400)
    S = 2.0
    R = np.exp(-t / S)
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot(t, R, color=BLUE, lw=2.6, label="R(Δt) = exp(−Δt / S)")
    ax.fill_between(t, R, color=BLUE, alpha=0.10)
    ax.axhline(0.5, color=GRAY, ls="--", lw=1.2)
    ax.annotate("半衰期：R = 0.5", xy=(S * np.log(2), 0.5),
                xytext=(S * np.log(2) + 0.6, 0.62),
                arrowprops=dict(arrowstyle="->", color=GRAY), color="#555", fontsize=10)
    ax.annotate("先快后慢：初期陡降，尾部平缓", xy=(1.2, 0.55),
                color=RED, fontsize=10.5)
    ax.set_xlabel("经过时间 Δt（相对单位）")
    ax.set_ylabel("记忆保持率 R")
    ax.set_title("经典艾宾浩斯保持率曲线（函数形式）", fontsize=13, color="#16324f")
    ax.set_ylim(0, 1.02); ax.set_xlim(0, 10)
    ax.grid(alpha=0.25); ax.legend(loc="upper right")
    save(fig, "ebbinghaus_classic.png")


# ---------- 图2：三桶半衰期对比 ----------
def fig_buckets():
    days = np.linspace(0, 120, 400)
    buckets = [("事实桶 T½=15 天", 15, ORANGE),
               ("事件桶 T½=30 天", 30, GREEN),
               ("核心桶 T½=90 天", 90, BLUE)]
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    for label, half, c in buckets:
        R = np.exp(-np.log(2) * days / half)
        ax.plot(days, R, lw=2.4, color=c, label=label)
    ax.axhline(0.5, color=GRAY, ls="--", lw=1.2)
    for half, c in [(15, ORANGE), (30, GREEN), (90, BLUE)]:
        ax.plot(half, 0.5, "o", color=c, ms=7)
    ax.set_xlabel("经过时间 Δt（天）")
    ax.set_ylabel("保持率 R")
    ax.set_title("三桶差异化衰减：半衰期 15 / 30 / 90 天", fontsize=13, color="#16324f")
    ax.set_ylim(0, 1.02); ax.set_xlim(0, 120)
    ax.grid(alpha=0.25); ax.legend(loc="upper right")
    save(fig, "ebbinghaus_buckets.png")


# ---------- 图3：复习加固（间隔重复） ----------
def fig_review():
    fig, ax = plt.subplots(figsize=(7.4, 4.4))
    # 每段衰减，复习时重置并让 S 变大
    S0 = 2.0
    starts = [0, 3, 7, 12]
    Ss = [S0, 3.2, 5.0, 7.5]
    colors = [RED, ORANGE, GREEN, BLUE]
    for k, (x0, S, c) in enumerate(zip(starts, Ss, colors)):
        x = np.linspace(x0, x0 + 6, 120)
        R0 = 1.0 if k == 0 else 0.92
        R = R0 * np.exp(-(x - x0) / S)
        ax.plot(x, R, lw=2.4, color=c,
                label=f"第 {k+1} 次学习，S={S:g}")
        if k < len(starts) - 1:
            ax.annotate("复习", xy=(starts[k+1], 0.92), xytext=(starts[k+1]-0.2, 1.05),
                        color="#555", fontsize=10,
                        arrowprops=dict(arrowstyle="->", color=GRAY))
    ax.set_xlabel("时间（相对单位）")
    ax.set_ylabel("保持率 R")
    ax.set_title("间隔复习的加固效果：S 增大、衰减变慢", fontsize=13, color="#16324f")
    ax.set_ylim(0, 1.12); ax.set_xlim(0, 18)
    ax.grid(alpha=0.25); ax.legend(loc="lower right", fontsize=9)
    save(fig, "ebbinghaus_review.png")


# ---------- 图4：六插件架构 ----------
def box(ax, x, y, w, h, text, fc, fs=10, tc="white"):
    p = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                       fc=fc, ec="#33475b", lw=1.1)
    ax.add_patch(p)
    ax.text(x + w/2, y + h/2, text, ha="center", va="center",
            color=tc, fontsize=fs, weight="bold")


def arrow(ax, p1, p2, color="#445566", style="-|>"):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=14,
                                 lw=1.3, color=color))


def fig_architecture():
    fig, ax = plt.subplots(figsize=(8.2, 5.4))
    ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")
    # 顶层：平台接入
    box(ax, 0.6, 8.6, 8.8, 1.0, "QQ / OneBot 接入层 · 多端消息与语音", "#4a6fa5", 11)
    # 中层六个插件
    plugins = [
        (0.4, 6.3, "核心插件\n动力学内核 z(t)", BLUE),
        (3.6, 6.3, "行为插件\n意图包·RVC·工具", GREEN),
        (6.8, 6.3, "心理插件\n六类模型 profile", PURPLE),
        (0.4, 4.0, "守护插件\n不变量·只读审计", RED),
        (3.6, 4.0, "信使插件\n短信·多端回执", ORANGE),
        (6.8, 4.0, "工具件\n52 个 LLM 工具", "#5b8c5a"),
    ]
    for x, y, t, c in plugins:
        box(ax, x, y, 2.8, 1.5, t, c, 9.5)
    # 数据契约总线
    box(ax, 0.6, 2.2, 8.8, 0.9, "数据契约总线（共享状态快照 · 契约目录）", "#6b7a8f", 10.5)
    # 底层存储
    box(ax, 0.6, 0.5, 4.2, 1.0, "记忆库 memory_v2\n图谱 / 分桶 / 逐字", "#355c7d", 9.5)
    box(ax, 5.2, 0.5, 4.2, 1.0, "任务库 · 观测台 · 日志", "#355c7d", 9.5)
    # 箭头
    arrow(ax, (5, 8.6), (5, 7.8))
    for x in [1.8, 5.0, 8.2]:
        arrow(ax, (x, 6.3), (x, 3.1))
        arrow(ax, (x, 4.0), (x, 3.1))
    arrow(ax, (2.7, 2.2), (2.7, 1.5))
    arrow(ax, (7.3, 2.2), (7.3, 1.5))
    ax.set_title("六插件仿生认知架构与数据契约", fontsize=13, color="#16324f")
    save(fig, "architecture_plugins.png")


# ---------- 图5：记忆分层数据流 ----------
def fig_memory_flow():
    fig, ax = plt.subplots(figsize=(8.2, 3.6))
    ax.set_xlim(0, 12); ax.set_ylim(0, 4); ax.axis("off")
    stages = [
        (0.2, "用户消息\n写入侧净化", BLUE),
        (2.6, "神经图谱\n节点+边", GREEN),
        (5.0, "三桶分桶\n保持率衰减", ORANGE),
        (7.4, "扩散召回\n动力学排序", PURPLE),
        (9.8, "意图包注入\n语言生成", RED),
    ]
    centers = []
    for x, t, c in stages:
        box(ax, x, 1.2, 2.0, 1.5, t, c, 9.2)
        centers.append(x + 2.0)
    for k in range(len(stages) - 1):
        arrow(ax, (centers[k], 1.95), (centers[k+1] - 2.0, 1.95))
    ax.text(6, 0.4, "全程检索与衰减不调用大模型（记忆零 LLM 成本）",
            ha="center", color="#555", fontsize=10)
    ax.set_title("记忆系统分层数据流", fontsize=13, color="#16324f")
    save(fig, "memory_dataflow.png")


if __name__ == "__main__":
    fig_classic()
    fig_buckets()
    fig_review()
    fig_architecture()
    fig_memory_flow()
    print("all figures done")
