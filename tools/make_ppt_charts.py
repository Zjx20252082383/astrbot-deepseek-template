# -*- coding: utf-8 -*-
"""生成 PPT 所需的成本与竞品图表（数据均来自实测/账单，非编造）。

输出到 ppt_assets/。运行：python make_ppt_charts.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import numpy as np

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 150
plt.rcParams["savefig.dpi"] = 150

BLUE, ORANGE, GREEN, RED, PURPLE, GRAY = (
    "#2f6fb0", "#e8862e", "#3f9e57", "#c0392b", "#7d5ba6", "#9aa5b1")
OUT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号\ppt_assets")
OUT.mkdir(parents=True, exist_ok=True)


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUT / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("saved", name)


# 1. 竞品定位矩阵 2x2
def competitor_matrix():
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.axhline(0.5, color=GRAY, lw=1.2)
    ax.axvline(0.5, color=GRAY, lw=1.2)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.set_xlabel("系统动作能力 →", fontsize=13)
    ax.set_ylabel("陪伴能力 →", fontsize=13)
    pts = [
        (0.22, 0.78, "Replika / Nomi / Kindroid", BLUE),
        (0.22, 0.24, "豆包 / 元宝 / DeepSeek", GRAY),
        (0.78, 0.24, "OPPO 小布 / 小米小爱 / 荣耀 YOYO", ORANGE),
        (0.78, 0.80, "《识海》（市场空档）", RED),
    ]
    for x, y, t, c in pts:
        ax.scatter([x], [y], s=260, color=c, zorder=3,
                   edgecolors="white", linewidths=1.5)
        dy = 0.07 if y > 0.5 else -0.09
        ax.annotate(t, (x, y), xytext=(x, y + dy), ha="center",
                    fontsize=11, color="#222",
                    fontweight="bold" if "识海" in t else "normal")
    ax.text(0.78, 0.62, "陪伴强 + 动作强\n= 全栈智能体空档", ha="center",
            color=RED, fontsize=10.5, alpha=0.85)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("竞品定位：陪伴能力 × 系统动作能力", fontsize=14, pad=10)
    for s in ["top", "right", "left", "bottom"]:
        ax.spines[s].set_visible(False)
    save(fig, "competitor_matrix.png")


# 2. 三维对比（记忆/情绪/执行）
def three_dim():
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    ax.axis("off")
    rows = [
        ("记忆 Memory", "死硬盘：单次长上下文 + 简单向量检索\n（易遗忘、无语义衰减）",
         "认知图谱：艾宾浩斯分桶衰减 + Hebbian 边权\n+ 重要度自评 + 按需原话检索"),
        ("情绪 Emotion", "AI 演员：靠 Prompt 伪装人设\n风一吹就崩，没有内心戏",
         "动力学心智：ODE 情绪场 + EgoRegulator\n（会克制、会防御、有真实内耗）"),
        ("执行 Execution", "单步机器人：只能做单步指令\n通用 App 无法操作外部",
         "业务状态机：多端回执闭环\n能处理出游突变、私密投票等复杂情境"),
    ]
    ax.set_xlim(0, 10); ax.set_ylim(0, 7)
    headers = ["核心维度", "市面竞品（偏科生）", "《识海》（全栈智能体）"]
    xs = [0.2, 3.4, 6.9]
    for x, h in zip(xs, headers):
        ax.text(x, 6.5, h, fontsize=12.5, fontweight="bold",
                color="white",
                bbox=dict(boxstyle="round,pad=0.4", fc=BLUE, ec="none"))
    for i, (dim, comp, ours) in enumerate(rows):
        y = 4.7 - i * 2.05
        ax.text(xs[0], y, dim, fontsize=11.5, fontweight="bold", color=BLUE)
        ax.text(xs[1], y, comp, fontsize=10, color="#555", va="center")
        ax.text(xs[2], y, ours, fontsize=10, color=RED, va="center")
    save(fig, "three_dim_compare.png")


# 3. 账单长尾：笔数占比 vs 金额占比
def billing_tiers():
    fig, ax = plt.subplots(figsize=(8.5, 5))
    labels = ["小额 <0.005 元", "中间档", "大额 ≥0.05 元"]
    cnt_share = [55.4, 36.2, 8.4]
    amt_share = [7.5, 42.3, 50.2]
    x = np.arange(len(labels)); w = 0.36
    b1 = ax.bar(x - w/2, cnt_share, w, label="笔数占比 %", color=GRAY)
    b2 = ax.bar(x + w/2, amt_share, w, label="金额占比 %", color=BLUE)
    for b in list(b1) + list(b2):
        ax.text(b.get_x() + b.get_width()/2, b.get_height() + 0.8,
                f"{b.get_height():.1f}", ha="center", fontsize=10)
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylabel("占比（%）"); ax.set_ylim(0, 60)
    ax.set_title("账单长尾：55% 的小额请求只占 7.5% 成本，8% 的大额占一半",
                 fontsize=12.5)
    ax.legend()
    save(fig, "billing_tiers.png")


# 4. 缓存命中率前后对比
def cache_hit():
    fig, ax = plt.subplots(figsize=(7.5, 5))
    labels = ["改前\n（dequeue=1）", "改后稳定态\n（dequeue=10）"]
    vals = [40.0, 98.6]
    colors = [GRAY, GREEN]
    bars = ax.bar(labels, vals, color=colors, width=0.55)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width()/2, v + 1.5, f"{v:.1f}%",
                ha="center", fontsize=13, fontweight="bold")
    ax.set_ylabel("前缀缓存命中率（%）"); ax.set_ylim(0, 110)
    ax.set_title("分段丢弃修复：缓存命中率 40% → 98.6%", fontsize=13)
    save(fig, "cache_hit_compare.png")


# 5. 等效输入 token 对比
def equiv_input():
    fig, ax = plt.subplots(figsize=(7.5, 5))
    labels = ["改前", "改后"]
    vals = [29171, 8127]
    bars = ax.bar(labels, vals, color=[GRAY, BLUE], width=0.55)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width()/2, v + 600, f"{v:,}",
                ha="center", fontsize=12, fontweight="bold")
    ax.set_ylabel("单次等效输入 token")
    ax.set_title("主对话单次等效输入 29,171 → 8,127（降 72.1%）", fontsize=13)
    ax.set_ylim(0, 33000)
    save(fig, "equiv_input_compare.png")


# 6. 月度成本通道对比
def channel_cost():
    fig, ax = plt.subplots(figsize=(8.5, 5))
    labels = ["百炼 PAI\n（现状）", "官方口径A", "官方中值", "官方口径B"]
    vals = [127.6, 28.03, 26.75, 25.59]
    colors = [RED, ORANGE, ORANGE, ORANGE]
    bars = ax.bar(labels, vals, color=colors, width=0.6)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width()/2, v + 2, f"{v:.1f}",
                ha="center", fontsize=11, fontweight="bold")
    ax.set_ylabel("估算月度成本（元）")
    ax.set_title("同口径月成本：百炼 127.6 元 vs DeepSeek 官方约 26–28 元",
                 fontsize=12.5)
    ax.set_ylim(0, 145)
    save(fig, "monthly_cost_channel.png")


# 7. 峰谷占比
def peak_valley():
    fig, ax = plt.subplots(figsize=(8, 4.5))
    cats = ["口径A（不扣周末）", "口径B（周末折减）"]
    peak = [46.7, 33.9]; off = [53.3, 66.1]
    ax.barh(cats, peak, color=RED, label="高峰占比 %")
    ax.barh(cats, off, left=peak, color=GREEN, label="闲时占比 %")
    for i, (p, o) in enumerate(zip(peak, off)):
        ax.text(p/2, i, f"{p:.1f}", ha="center", va="center",
                color="white", fontsize=11)
        ax.text(p + o/2, i, f"{o:.1f}", ha="center", va="center",
                color="white", fontsize=11)
    ax.set_xlim(0, 100); ax.set_xlabel("时间占比（%）")
    ax.set_title("真实峰谷映射：闲时约 53%–66%，中值约 60%", fontsize=12.5)
    ax.legend(loc="lower right", bbox_to_anchor=(1, -0.42), ncol=2)
    save(fig, "peak_valley_share.png")


# 8. 官网月成本构成
def cost_composition():
    fig, ax = plt.subplots(figsize=(6.5, 6))
    sizes = [44, 48, 8]
    labels = ["后台未命中输入 44%", "输出 48%", "主对话及其他 8%"]
    colors = [ORANGE, BLUE, GRAY]
    ax.pie(sizes, labels=labels, colors=colors, autopct="%1.0f%%",
           startangle=90, textprops={"fontsize": 11},
           wedgeprops=dict(width=0.45, edgecolor="white"))
    ax.set_title("官网月成本构成（约 26.8 元）", fontsize=13)
    save(fig, "cost_composition.png")


# 9. 中国 AI 情感陪伴市场规模（中研普华，多来源交叉验证）
def market_size():
    fig, ax = plt.subplots(figsize=(8.5, 5))
    years = ["2024", "2025", "2026", "2027", "2028"]
    vals = [12.11, 38.66, 102.64, 241.02, 595.06]
    colors = [GRAY, BLUE, ORANGE, ORANGE, RED]
    bars = ax.bar(years, vals, color=colors, width=0.62)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width()/2, v + 8, f"{v:.1f}",
                ha="center", fontsize=11, fontweight="bold")
    ax.set_ylabel("市场规模（亿元）")
    ax.set_ylim(0, 660)
    ax.set_title("中国 AI 情感陪伴市场：2024→2028，CAGR 约 148.7%",
                 fontsize=13)
    ax.annotate("4 年约 49 倍", xy=(4, 595), xytext=(2.6, 470),
                fontsize=12, color=RED, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))
    save(fig, "market_size.png")


if __name__ == "__main__":
    competitor_matrix()
    three_dim()
    billing_tiers()
    cache_hit()
    equiv_input()
    channel_cost()
    peak_valley()
    cost_composition()
    market_size()
    print("全部图表生成完毕 ->", OUT)
