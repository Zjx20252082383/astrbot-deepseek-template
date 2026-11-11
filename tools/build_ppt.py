# -*- coding: utf-8 -*-
"""《识海》深度项目调研汇报 PPT 生成（python-pptx，本地 .pptx）。

完整调研叙事：Hook → 政策分析 → 项目背景 → 市场与痛点 → 心理学原理与建模实现
→ 系统架构与技术创新 → 仿真验证 → 成本工程 → 应对策略与未来规划。
多图表（市场/竞品/成本 + 仿真）、含仿真效果、多预留功能效果图位置。
运行：python build_ppt.py
"""
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt
from pptx.enum.dml import MSO_LINE_DASH_STYLE

ROOT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号")
MEDIA = ROOT / "AstrBot_snowluma" / "paper" / "media"
ASSETS = ROOT / "ppt_assets"
OUT = ROOT / "AstrBot_snowluma" / "core" / "识海_深度项目调研汇报.pptx"

# 配色
BLUE = RGBColor(0x2F, 0x6F, 0xB0)
DARK = RGBColor(0x1F, 0x4E, 0x79)
RED = RGBColor(0xC0, 0x39, 0x2B)
LIGHT = RGBColor(0xF4, 0xF6, 0xF8)
TXT = RGBColor(0x26, 0x2B, 0x33)
GRAY = RGBColor(0x6B, 0x73, 0x7B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CARDBG = RGBColor(0xEA, 0xF1, 0xF8)
CODEBG = RGBColor(0x1E, 0x27, 0x30)
CODEFG = RGBColor(0xD4, 0xE2, 0xEC)
GREEN = RGBColor(0x3F, 0x9E, 0x57)
FONT = "微软雅黑"
CODEFONT = "Consolas"

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]
PAGE = [0]


def _set_font(run, size, bold=False, color=TXT, name=FONT, italic=False):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:latin", "a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", name)


def textbox(slide, x, y, w, h, lines, anchor=MSO_ANCHOR.TOP):
    """lines: list of (text, size, bold, color, align) 或 (text,size,bold,color)."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for i, spec in enumerate(lines):
        text, size, bold, color = spec[:4]
        align = spec[4] if len(spec) > 4 else PP_ALIGN.LEFT
        italic = spec[5] if len(spec) > 5 else False
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(6)
        r = p.add_run()
        r.text = text
        _set_font(r, size, bold, color, italic=italic)
    return tb


def rect(slide, x, y, w, h, fill, line=None, line_w=None, dash=None):
    sp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y),
                                Inches(w), Inches(h))
    sp.fill.solid()
    sp.fill.fore_color.rgb = fill
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line
        sp.line.width = Pt(line_w or 1)
        if dash:
            sp.line.dash_style = dash
    sp.shadow.inherit = False
    return sp


def base_slide(title=None, kicker=None):
    slide = prs.slides.add_slide(BLANK)
    rect(slide, 0, 0, 13.333, 7.5, WHITE)
    if title:
        rect(slide, 0, 0, 13.333, 1.15, BLUE)
        rect(slide, 0, 1.15, 13.333, 0.06, RED)
        textbox(slide, 0.55, 0.12, 11.5, 0.9,
                [(title, 24, True, WHITE)], anchor=MSO_ANCHOR.MIDDLE)
        if kicker:
            textbox(slide, 10.2, 0.12, 2.7, 0.9,
                    [(kicker, 11, False, RGBColor(0xCF, 0xE0, 0xF2))],
                    anchor=MSO_ANCHOR.MIDDLE)
    PAGE[0] += 1
    if title:
        textbox(slide, 12.4, 7.05, 0.7, 0.35,
                [(str(PAGE[0]), 10, False, GRAY)])
    return slide


def add_pic_fit(slide, path, x, y, max_w, max_h):
    with Image.open(path) as im:
        iw, ih = im.size
    ratio = min(max_w / iw, max_h / ih)
    w = iw * ratio
    h = ih * ratio
    px = x + (max_w - w) / 2
    py = y + (max_h - h) / 2
    return slide.shapes.add_picture(str(path), Inches(px), Inches(py),
                                   Inches(w), Inches(h))


def bullets(slide, x, y, w, h, items, size=15, gap=8):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, (head, body) in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        r1 = p.add_run(); r1.text = "▪ "
        _set_font(r1, size, True, BLUE)
        r2 = p.add_run(); r2.text = head
        _set_font(r2, size, True, DARK)
        if body:
            r3 = p.add_run(); r3.text = "  " + body
            _set_font(r3, size - 1, False, TXT)
    return tb


def codebox(slide, x, y, w, h, code, size=10.5, title=None):
    rect(slide, x, y, w, h, CODEBG)
    if title:
        rect(slide, x, y, w, 0.34, RGBColor(0x2C, 0x38, 0x44))
        textbox(slide, x + 0.12, y + 0.02, w - 0.2, 0.32,
                [(title, 10.5, True, RGBColor(0x8F, 0xB6, 0xDE))],
                anchor=MSO_ANCHOR.MIDDLE)
        ty = y + 0.4
        hh = h - 0.4
    else:
        ty = y + 0.12
        hh = h - 0.2
    tb = slide.shapes.add_textbox(Inches(x + 0.14), Inches(ty),
                                  Inches(w - 0.28), Inches(hh))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, line in enumerate(code.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(0)
        p.space_before = Pt(0)
        r = p.add_run()
        r.text = line if line else " "
        _set_font(r, size, False, CODEFG, name=CODEFONT)
    return tb


def formula(slide, x, y, w, h, lines, size=14):
    rect(slide, x, y, w, h, RGBColor(0xF0, 0xF4, 0xFA), line=BLUE, line_w=1)
    tb = slide.shapes.add_textbox(Inches(x + 0.15), Inches(y + 0.1),
                                  Inches(w - 0.3), Inches(h - 0.2))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run(); r.text = ln
        _set_font(r, size, True, DARK, italic=True)
    return tb


def placeholder(slide, x, y, w, h, label):
    sp = rect(slide, x, y, w, h, LIGHT, line=GRAY, line_w=1.5,
              dash=MSO_LINE_DASH_STYLE.DASH)
    tf = sp.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = "功能效果图预留\n" + label
    _set_font(r, 13, True, GRAY)
    return sp


def table(slide, x, y, w, h, data, col_widths=None, head_size=12,
          body_size=10.5):
    rows, cols = len(data), len(data[0])
    gtbl = slide.shapes.add_table(rows, cols, Inches(x), Inches(y),
                                 Inches(w), Inches(h)).table
    if col_widths:
        total = sum(col_widths)
        for c, cw in enumerate(col_widths):
            gtbl.columns[c].width = Inches(w * cw / total)
    for r in range(rows):
        for c in range(cols):
            cell = gtbl.cell(r, c)
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.08)
            cell.margin_top = Inches(0.03)
            cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            run = p.add_run()
            run.text = data[r][c]
            if r == 0:
                cell.fill.solid()
                cell.fill.fore_color.rgb = BLUE
                _set_font(run, head_size, True, WHITE)
            else:
                cell.fill.solid()
                cell.fill.fore_color.rgb = WHITE if r % 2 else CARDBG
                _set_font(run, body_size, c == 0, DARK if c == 0 else TXT)
    return gtbl


# ================= 1. 封面 =================
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, DARK)
rect(s, 0, 5.0, 13.333, 0.12, RED)
textbox(s, 1.0, 1.7, 11.3, 1.6,
        [("《识海》全栈智能体", 44, True, WHITE)])
textbox(s, 1.0, 3.1, 11.3, 1.2,
        [("深度项目调研汇报：从心理学原理到动力学建模、系统实现与成本工程", 20,
          False, RGBColor(0xCF, 0xE0, 0xF2))])
textbox(s, 1.0, 5.4, 11.3, 1.4,
        [("基于 AstrBot v4.14.6 / OneBot v11 与 DeepSeek 模型", 14, False,
          RGBColor(0xB8, 0xCF, 0xE6)),
         ("汇报人：郑俊鑫　|　2026 年 11 月", 14, False,
          RGBColor(0xB8, 0xCF, 0xE6))])

# ================= 2. Hook =================
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, DARK)
rect(s, 0, 0, 0.18, 7.5, RED)
textbox(s, 0.9, 0.9, 11.6, 1.0,
        [("先问一个问题：", 22, True, RGBColor(0x9F, 0xC2, 0xE4))])
textbox(s, 0.9, 1.7, 11.7, 2.0,
        [("当一个 AI 陪你聊了几千句话之后，", 30, True, WHITE),
         ("它真的「记得你」、真的「在乎你」吗？", 30, True, WHITE)])
hook_cards = [
    ("3 亿人", "愿意为「不孤独」买单，情感陪伴需求真实而庞大"),
    ("337 款", "全球 AI 陪伴 App，却大多只会「扮演」，没有连续记忆"),
    ("595 亿", "2028 年中国市场规模，但「陪伴强 × 能办事」仍是空档"),
]
for i, (big, desc) in enumerate(hook_cards):
    x = 0.9 + i * 4.0
    rect(s, x, 4.25, 3.7, 2.2, RGBColor(0x2A, 0x5A, 0x88))
    textbox(s, x + 0.2, 4.45, 3.3, 0.9, [(big, 30, True, RGBColor(0xF2, 0xC0,
            0x6B))])
    textbox(s, x + 0.2, 5.35, 3.3, 1.0, [(desc, 12.5, False, RGBColor(0xD8,
            0xE6, 0xF2))])
textbox(s, 0.9, 6.75, 11.6, 0.6,
        [("《识海》要回答的，正是这个空档：让 AI 有连续记忆、有内心戏，还能真正办事。",
          14, True, RGBColor(0xCF, 0xE0, 0xF2))])

# ================= 3. 目录 =================
s = base_slide("目录 / Contents")
toc = [
    ("01　政策分析与项目背景", "国家战略、拟人化治理、行业演进"),
    ("02　市场调研与痛点分析", "市场规模、竞品矩阵、用户痛点"),
    ("03　心理学原理与建模实现", "遗忘曲线 / Hebbian / 人格依恋 / 失调防御"),
    ("04　系统架构与技术创新", "记忆、情绪、执行、多会话"),
    ("05　仿真验证与成本工程", "200h 长时稳定、真实账单与降本"),
    ("06　应对策略与未来规划", "技术路线图、分阶段规划"),
]
bullets(s, 1.2, 1.6, 11, 5.4, toc, size=18, gap=15)

# ================= 4. 政策分析 =================
s = base_slide("政策分析：国家战略鼓励发展，拟人化互动走向规范", "Policy")
pol = [
    ["政策 / 文件", "时间", "核心指向"],
    ["国务院《关于深入实施「人工智能+」行动的意见》\n国发〔2025〕11 号", "2025-08",
     "到 2027 年率先实现 AI 与 6 大重点领域深度融合，建设人机协同的智能经济与智能社会"],
    ["国家网信办《人工智能拟人化互动服务\n管理暂行办法（征求意见稿）》", "2025-12",
     "回应情感陪伴、心理疗愈风险，「智能有度、服务有温」，规范拟人化互动边界"],
    ["「十五五」相关规划导向", "2026 起",
     "在发展中规范、在规范中发展，鼓励可信、可控、有温度的 AI 应用"],
]
table(s, 0.6, 1.55, 12.1, 3.4, pol, col_widths=[3.4, 1.3, 6.0],
      body_size=11)
bullets(s, 0.7, 5.35, 12, 1.8, [
    ("机会", "政策同时支持「智能经济」与「有温度的服务」，全栈陪伴智能体正处在鼓励方向。"),
    ("合规要求", "情感陪伴需边界清晰、可审计、防沉迷与误导——本项目的守护层与不变量设计正是为此预留。"),
], size=13.5)

# ================= 5. 项目背景 =================
s = base_slide("项目背景：从单点问答走向持续、主动、具身的智能体", "Background")
bullets(s, 0.7, 1.6, 12, 4.6, [
    ("通用助手", "豆包 / 元宝 / DeepSeek 强在问答与工具，弱在长期陪伴与人格连续。"),
    ("陪伴产品", "Replika / Nomi / Kindroid 强在情感陪伴，却难以操作系统、完成实际任务。"),
    ("系统助手", "小布 / 小爱 / YOYO 强在设备控制，弱在情绪与长期记忆。"),
    ("演进趋势", "智能体正从「被动应答」转向「主动发起、跨会话记忆、执行闭环」。"),
    ("核心命题", "把动力学心智与业务状态机结合，做全栈而非偏科的智能体。"),
], size=15.5)

# ================= 6. 市场规模 =================
s = base_slide("市场调研（一）：AI 情感陪伴高速扩张", "Market")
add_pic_fit(s, ASSETS / "market_size.png", 0.55, 1.5, 8.2, 5.4)
bullets(s, 8.9, 1.8, 4.0, 4.6, [
    ("CAGR", "约 148.7%，4 年约 49 倍。"),
    ("保有量", "全球情感机器人系统超 800 万台。"),
    ("专利", "国内相关专利 1283 项，2025 年申请同比 +142%。"),
    ("数据来源", "中研普华，经新华网、36 氪等交叉验证。"),
], size=13)

# ================= 7. 竞品矩阵 =================
s = base_slide("市场调研（二）：竞品定位矩阵", "Market")
add_pic_fit(s, ASSETS / "competitor_matrix.png", 0.6, 1.4, 8.2, 5.7)
bullets(s, 9.0, 1.8, 3.9, 4.5, [
    ("空档", "右上象限尚无成熟产品。"),
    ("识海", "同时具备强陪伴与强动作。"),
    ("壁垒", "记忆 + 情绪 + 执行三位一体。"),
], size=14)

# ================= 8. 三维对比 =================
s = base_slide("市场调研（三）：记忆 / 情绪 / 执行三维对比", "Market")
add_pic_fit(s, ASSETS / "three_dim_compare.png", 0.5, 1.5, 12.3, 5.4)

# ================= 9. 痛点分析 =================
s = base_slide("痛点分析：现有 AI 陪伴的四个结构性缺口", "Pain Points")
pains = [
    ("记不住", "依赖单次长上下文，截断即丢；窗口外只剩摘要，没有完整原话。"),
    ("不连续", "靠 Prompt 每轮扮演人设，情绪与人格不积累，「风一吹就崩」。"),
    ("办不了事", "只能单步应答，无法操作外部系统、处理多步骤复杂情境。"),
    ("不可信", "易幻觉、易情感依赖与误导，缺乏一致性审计与合规边界。"),
]
for i, (h, b) in enumerate(pains):
    x = 0.6 + (i % 2) * 6.25
    y = 1.6 + (i // 2) * 2.55
    rect(s, x, y, 5.9, 2.3, CARDBG)
    rect(s, x, y, 0.12, 2.3, RED)
    textbox(s, x + 0.28, y + 0.2, 5.4, 0.6, [(h, 19, True, DARK)])
    textbox(s, x + 0.28, y + 0.95, 5.45, 1.2, [(b, 13, False, TXT)])

# ================= 10. 心理学原理总览 =================
s = base_slide("心理学原理与建模：从理论到可计算的映射", "Psychology")
psy = [
    ["心理学 / 神经科学原理", "提出者", "建模实现", "落地模块"],
    ["遗忘曲线（保持率先快后慢）", "艾宾浩斯 Ebbinghaus", "R=e^(−Δt/S) 分桶衰减", "memory_v2"],
    ["赫布学习（共现即连接）", "赫布 Hebb", "Δw∝a_i·a_j 边权更新", "神经图谱"],
    ["大五人格 OCEAN", "McCrae & Costa", "ψ 五维参数调制响应", "psyche"],
    ["依恋理论", "鲍尔比 / 安斯沃斯", "att 依恋状态与距离调节", "psyche"],
    ["认知失调", "费斯廷格 Festinger", "失调量驱动 Ego 调节", "EgoRegulator"],
    ["心理防御机制", "弗洛伊德 / 安娜", "防御四机制 + 分段放行", "EgoRegulator"],
    ["自我效能感", "班杜拉 Bandura", "效能反馈调整 θ 与阈值", "psyche / core"],
]
table(s, 0.5, 1.45, 12.35, 5.4, psy, col_widths=[3.3, 2.4, 3.6, 1.8],
      body_size=10.5, head_size=11.5)

# ---------- 11. 心理学① 遗忘曲线 ----------
s = base_slide("心理学原理（一）：艾宾浩斯遗忘曲线 → 记忆衰减", "Psychology")
bullets(s, 0.6, 1.55, 5.7, 4.8, [
    ("原理简介", "1885 年艾宾浩斯实验发现：遗忘先快后慢，保持率随时间近似指数下降。"),
    ("关键变量", "稳定度 S 越大，遗忘越慢；S 由重要度、情绪峰值、复习次数共同决定。"),
    ("分桶半衰期", "事实 15 天 / 事件 30 天 / 核心 90 天，归档只移不删。"),
    ("复习加固", "每次被重新想起，N_recall 增加，S 上升，符合间隔重复。"),
], size=13.5)
formula(s, 6.5, 1.6, 6.3, 1.5,
        ["R(t) = exp( −Δt / S )",
         "S = (1+β·imp)(1+α·E_v)(1+β_N·N_recall)"], size=14)
codebox(s, 6.5, 3.3, 6.3, 3.3,
        "import math\n"
        "def retention(dt_hours, stability):\n"
        "    return math.exp(-dt_hours / stability)\n"
        "\n"
        "def stability(imp, emo, n_recall):\n"
        "    return ((1+b*imp) * (1+a*emo)\n"
        "            * (1+bn*n_recall))\n"
        "\n"
        "if retention(dt, S) < MIN_SCORE:   # 0.30\n"
        "    move_to_archive(item)           # 只移不删",
        title="memory_v2.py（简化）")

# ---------- 12. 心理学② Hebbian ----------
s = base_slide("心理学原理（二）：赫布学习 → 语义边权", "Psychology")
bullets(s, 0.6, 1.55, 5.7, 4.8, [
    ("原理简介", "赫布 1949 年提出：反复共同激活的神经元之间连接增强（Cells that fire together, wire together）。"),
    ("建模方式", "节点共现一次，边权按激活度乘积增加；不活跃时边权缓慢衰减。"),
    ("边防错构", "低质量、超长降级内容只可检索、不进注入，避免虚假连接污染召回。"),
    ("梦境固化", "凌晨重放高频共现，修剪弱边，巩固语义结构。"),
], size=13.5)
formula(s, 6.5, 1.6, 6.3, 1.4,
        ["Δw_ij = η · a_i · a_j",
         "w_ij(t+1) = w_ij + Δw − λ·w_ij"], size=14)
codebox(s, 6.5, 3.2, 6.3, 3.4,
        "def hebbian_update(graph, nodes, eta=0.05):\n"
        "    for i in nodes:\n"
        "        for j in nodes:\n"
        "            if i == j: continue\n"
        "            dw = eta * act(i) * act(j)\n"
        "            graph[i][j] += dw\n"
        "    decay_edges(graph, lam)     # 双衰减\n"
        "\n"
        "def dream_consolidate(graph):\n"
        "    replay_cooccur(graph)\n"
        "    prune_weak_edges(graph)",
        title="神经图谱（简化）")

# ---------- 13. 心理学③ 人格与依恋 ----------
s = base_slide("心理学原理（三）：大五人格与依恋理论", "Psychology")
bullets(s, 0.6, 1.5, 6.0, 3.0, [
    ("大五人格 OCEAN", "开放性 / 尽责性 / 外倾性 / 宜人性 / 神经质，五维 ψ 连续调制响应模式。"),
    ("依恋理论", "鲍尔比、安斯沃斯：依恋对象的亲近与分离影响安全感；建模为 att 依恋状态。"),
], size=13.5)
formula(s, 0.6, 4.3, 6.0, 1.3,
        ["ψ = [O, C, E, A, N]",
         "d(att)/dt = k_close·proximity − k_sep·distance"], size=13)
codebox(s, 6.8, 1.5, 6.0, 5.1,
        "def render_expression(psi, base):\n"
        "    O,C,E,A,N = psi\n"
        "    tone = base.copy()\n"
        "    tone['verbosity'] *= 0.8+0.4*O\n"
        "    tone['warmth']    *= 0.8+0.4*A\n"
        "    tone['anxiety']   += 0.6*N\n"
        "    return tone\n"
        "\n"
        "def attachment(att, proximity, dist):\n"
        "    return att + dt*(k_close*proximity\n"
        "                    - k_sep*dist)",
        title="psyche（简化）")

# ---------- 14. 心理学④ 失调、防御、自我效能 ----------
s = base_slide("心理学原理（四）：认知失调、防御机制与自我效能", "Psychology")
psy4 = [
    ["原理", "提出者", "核心机制", "建模与作用"],
    ["认知失调", "费斯廷格", "信念与行为冲突产生紧张", "失调量驱动 Ego 调节，产生「内耗」"],
    ["防御机制", "弗洛伊德 / 安娜", "压抑、转移、合理化等缓解焦虑", "防御四机制 + 分段放行表"],
    ["自我效能", "班杜拉", "对「能否做到」的信念", "成功/失败反馈调整 θ 与触发阈值"],
]
table(s, 0.55, 1.5, 12.25, 2.6, psy4, col_widths=[2.0, 2.2, 3.6, 4.2],
      body_size=11)
codebox(s, 0.6, 4.4, 12.2, 2.5,
        "def ego_regulate(z, impulse):\n"
        "    dissonance = abs(z.belief - z.action)          # 认知失调\n"
        "    if dissonance > TH or z.anxiety > TH:         # 触发防御\n"
        "        return defense_mechanism(z, impulse)      # 压抑/转移/合理化\n"
        "    z.theta += lr * outcome_feedback             # 自我效能更新\n"
        "    return staged_release(z, impulse)            # §5.10 分段放行",
        title="EgoRegulator（简化）")

# ================= 15. 总体架构 =================
s = base_slide("系统架构：分层解耦，数据目录即契约", "Architecture")
layers = [
    ("接入层", "OneBot v11 / QQ 消息、语音、图片、事件"),
    ("心智层", "ODE 情绪场 · 冲动累积 · EgoRegulator 自我调节"),
    ("记忆层", "事实/事件分桶 · 艾宾浩斯衰减 · Hebbian 边权 · 逐字归档"),
    ("能力层", "业务状态机 · 工具集 · 主动行为 · 任务情境"),
    ("模型层", "DeepSeek（LLM 工具调用）· 多模态 · 语音合成/翻唱"),
]
for i, (h, b) in enumerate(layers):
    y = 1.55 + i * 1.02
    rect(s, 0.8, y, 2.6, 0.82, BLUE)
    textbox(s, 0.8, y, 2.6, 0.82, [(h, 16, True, WHITE)],
            anchor=MSO_ANCHOR.MIDDLE)
    rect(s, 3.5, y, 7.0, 0.82, CARDBG)
    textbox(s, 3.7, y, 6.7, 0.82, [(b, 13.5, False, TXT)],
            anchor=MSO_ANCHOR.MIDDLE)
placeholder(s, 10.75, 1.55, 2.0, 5.0, "总体架构图\n（可替换为\n正式架构图）")

# ================= 16. 记忆：认知图谱 =================
s = base_slide("技术创新（一）：认知图谱而非死硬盘", "Innovation")
bullets(s, 0.7, 1.6, 12, 5, [
    ("分桶存储", "事实 Fact 与事件 Event 分桶，各自带重要度与衰减参数。"),
    ("艾宾浩斯衰减", "保持率 R=e^{−Δt/S}，稳定度随重要度、情绪峰值、复习次数提升。"),
    ("Hebbian 边权", "共现强化语义边，边防错构：降级内容可检索但不进注入。"),
    ("重要度自评", "LLM 对新条目打 0–1 重要度，高重要度事件更慢遗忘。"),
    ("时间线注入", "每日一句补回日期维度，避免只剩几段老话。"),
], size=15)

# ================= 17. 记忆：逐字归档 =================
s = base_slide("技术创新（二）：逐字归档与原文锚点", "Innovation")
bullets(s, 0.7, 1.55, 7.4, 5, [
    ("问题", "窗口外只剩摘要，逐字原文在 11-10 后断档。"),
    ("归档", "SQLite + WAL + FTS5，每轮 user/assistant 原文持续落库。"),
    ("锚点", "EventItem 增 transcript_turn_id，指向同轮逐字。"),
    ("按需检索", "recall_transcript 只在需要原话时调用，不常驻前缀。"),
    ("零缓存影响", "内容不进固定前缀，不降低缓存命中率。"),
], size=14)
add_pic_fit(s, MEDIA / "fig_07_differentiated_decay.png",
            8.3, 1.6, 4.6, 3.0)
placeholder(s, 8.3, 4.8, 4.6, 2.2, "逐字检索工具\n调用效果截图")

# ================= 18. 情绪 ODE =================
s = base_slide("技术创新（三）：ODE 动力学心智，而非 AI 演员", "Innovation")
add_pic_fit(s, MEDIA / "fig_01_relaxation.png", 0.6, 1.5, 7.6, 5.4)
bullets(s, 8.4, 1.8, 4.5, 4.5, [
    ("情绪场", "多情绪连续 ODE，受刺激后自然松弛回落。"),
    ("冲动累积", "du/dt 累积，达阈值后释放并回落。"),
    ("自我调节", "EgoRegulator 会克制、会防御，产生真实内耗。"),
    ("人格参数", "神经质等参数改变响应模式。"),
], size=14)

# ================= 19. 执行：状态机 =================
s = base_slide("技术创新（四）：业务状态机与多端回执闭环", "Innovation")
bullets(s, 0.7, 1.6, 12, 5, [
    ("QQ 动作", "改昵称/头像/签名、发空间、点赞评论、传话等真实平台操作。"),
    ("任务情境", "课表、驿站、步行分钟等情境驱动，处理出游突变等复杂流程。"),
    ("主动发起", "行为由情绪/目标阈值涌现，满足条件即主动执行，无需改代码。"),
    ("回执闭环", "发送回执落盘，下一 tick 消费，确认动作真正完成。"),
    ("工具调用", "LLM 通过 @llm_tool 决策，最大 30 步、超时 600s。"),
], size=15)

# ================= 20. 多会话 =================
s = base_slide("多会话机制：会话隔离，作用域不串扰", "Architecture")
bullets(s, 0.7, 1.6, 12, 5, [
    ("并发回复", "按 unified_msg_origin 独立管理回复任务，打断不影响其他会话。"),
    ("焦点落盘", "每个会话的回复焦点独立保存，互不覆盖。"),
    ("归档隔离", "逐字归档按 origin 区分；长期记忆按 sender 归并同一人。"),
    ("锁机制", "框架按会话加锁，避免同一会话并发竞争。"),
], size=16)

# ================= 21. 仿真设置 =================
s = base_slide("仿真验证（一）：设置与方法", "Simulation")
bullets(s, 0.7, 1.6, 12, 5, [
    ("求解器", "RK4 积分，步长 0.25h，保证数值精度。"),
    ("刺激", "随机消息刺激，模拟真实对话节奏。"),
    ("时长", "200h 长时运行，检验稳定性与边界。"),
    ("五道防线", "饱和、阈值、门控等保护机制全部开启。"),
    ("检查项", "松弛、耦合、Hebbian、衰减、扰动、极端饱和等 15 项。"),
], size=16)

# ================= 22. 仿真长时 =================
s = base_slide("仿真验证（二）：200h 长时稳定", "Simulation")
add_pic_fit(s, MEDIA / "fig_05_long_run_200h.png", 0.6, 1.4, 12.1, 5.7)

# ================= 23. 仿真多图 =================
s = base_slide("仿真验证（三）：耦合、衰减与防错构", "Simulation")
add_pic_fit(s, MEDIA / "fig_02_emotion_memory_coupling.png",
            0.5, 1.5, 6.2, 2.8)
add_pic_fit(s, MEDIA / "fig_12_fault_rate.png", 6.9, 1.5, 6.0, 2.8)
add_pic_fit(s, MEDIA / "fig_11_ego_release.png", 0.5, 4.4, 6.2, 2.7)
add_pic_fit(s, MEDIA / "fig_03_hebbian.png", 6.9, 4.4, 6.0, 2.7)

# ================= 24. 成本：账单 =================
s = base_slide("成本工程（一）：真实账单画像（11 天 4,231 笔）", "Cost")
add_pic_fit(s, ASSETS / "billing_tiers.png", 0.55, 1.5, 8.0, 5.4)
bullets(s, 8.8, 1.8, 4.2, 4.5, [
    ("总支出", "56.86 元，日均约 5.17。"),
    ("长尾", "55% 小额请求仅占 7.5%。"),
    ("集中", "8% 大额请求占一半成本。"),
    ("后台", "约 352 次/天小请求，占约 60%。"),
], size=13.5)

# ================= 25. 成本：缓存 =================
s = base_slide("成本工程（二）：前缀缓存诊断与分段丢弃", "Cost")
add_pic_fit(s, ASSETS / "cache_hit_compare.png", 0.5, 1.5, 6.2, 3.0)
add_pic_fit(s, ASSETS / "equiv_input_compare.png", 6.85, 1.5, 6.1, 3.0)
bullets(s, 0.7, 4.7, 12, 2.4, [
    ("根因", "滑动窗口逐轮位移，固定前缀仅命中 system+schema（约 17.4k）。"),
    ("修复", "dequeue 1→10，一次丢 10 轮，后续 9 轮前缀稳定，命中升至 98.6%。"),
    ("效果", "单次等效输入 29,171→8,127，降 72.1%；10 轮平均降约 67%。"),
], size=13.5)

# ================= 26. 成本：通道 =================
s = base_slide("成本工程（三）：通道对比与峰谷映射", "Cost")
add_pic_fit(s, ASSETS / "monthly_cost_channel.png", 0.5, 1.5, 6.3, 3.1)
add_pic_fit(s, ASSETS / "peak_valley_share.png", 6.95, 1.6, 6.0, 2.9)
bullets(s, 0.7, 4.8, 12, 2.2, [
    ("月成本", "百炼 127.6 元 vs 官方约 26–28 元，渠道倍数约 4.8。"),
    ("峰谷", "真实闲时约 53%–66%，中值约 60%；闲时输出单价减半。"),
], size=13.5)

# ================= 27. 成本：构成 =================
s = base_slide("成本工程（四）：成本构成与降本路径", "Cost")
add_pic_fit(s, ASSETS / "cost_composition.png", 0.6, 1.5, 5.4, 5.4)
bullets(s, 6.3, 1.6, 6.6, 5, [
    ("已落地", "缓存分段丢弃，等效输入降 72%。"),
    ("可选项", "切换 DeepSeek 官方通道，月成本降约 79%。"),
    ("待优化", "后台小请求占比高，需埋点拆分成分后再治理。"),
    ("调度", "可将 cron 类任务挪到闲时，利用峰谷价差。"),
    ("原则", "先测量、后优化，避免以牺牲响应方式换成本。"),
], size=14)

# ================= 28. 应对策略与技术创新 =================
s = base_slide("应对策略：以「可计算的心理机制」系统回应四大痛点", "Strategy")
strat = [
    ["痛点", "应对策略", "技术创新"],
    ["记不住", "认知图谱 + 逐字归档 + 原文锚点", "艾宾浩斯衰减、FTS5、按需检索"],
    ["不连续", "状态动力学连续演化，不依赖 Prompt 扮演", "ODE 情绪场、冲动累积、人格 ψ"],
    ["办不了事", "业务状态机 + 多端回执闭环", "意图包、主动行为、工具调用"],
    ["不可信", "守护层四不变量 + 合规审计", "一致性校验、防错构、拟人化合规"],
]
table(s, 0.55, 1.5, 12.25, 3.6, strat, col_widths=[2.0, 4.6, 4.6],
      body_size=11.5)
bullets(s, 0.7, 5.5, 12, 1.6, [
    ("差异化", "不是再加一个聊天 App，而是把心理学原理变成可运行、可验证、可治理的工程系统。"),
], size=13.5)

# ================= 29. 未来规划路线图 =================
s = base_slide("未来规划：分阶段技术路线图", "Roadmap")
phases = [
    ("阶段一 · 已完成", "记忆/情绪/执行主干、逐字归档、缓存降本、仿真与论文", GREEN),
    ("阶段二 · 近期", "端到端验证、后台小请求拆分、补全功能效果图、合规对齐", BLUE),
    ("阶段三 · 中期", "Flutter 多端、3D 全息前端、通道切换与闲时调度", RGBColor(0x7D, 0x5B, 0xA6)),
    ("阶段四 · 远期", "多模态具身协同、个性化长期演化、可迁移心智框架", RED),
]
for i, (h, b, c) in enumerate(phases):
    y = 1.6 + i * 1.28
    rect(s, 0.8, y, 3.0, 1.05, c)
    textbox(s, 0.8, y, 3.0, 1.05, [(h, 15, True, WHITE)],
            anchor=MSO_ANCHOR.MIDDLE)
    rect(s, 3.9, y, 8.9, 1.05, CARDBG)
    textbox(s, 4.1, y, 8.6, 1.05, [(b, 13.5, False, TXT)],
            anchor=MSO_ANCHOR.MIDDLE)

# ================= 30. 结论 =================
s = base_slide("结论", "Conclusion")
bullets(s, 0.7, 1.6, 12, 4.6, [
    ("结论一", "全栈智能体可行：记忆、情绪、执行在同一动力学底座上协同。"),
    ("结论二", "心理学原理可计算：遗忘、Hebbian、人格依恋、失调防御均落到具体公式与模块。"),
    ("结论三", "仿真 200h 稳定，防线有效，防错构机制抑制幻觉。"),
    ("结论四", "成本可量化、可治理：缓存降本已落地，通道切换可再降 79%。"),
    ("定位", "占据「陪伴强 × 动作强」市场空档，顺应政策与市场双重红利。"),
], size=15.5)

# ================= 31-33. 功能效果图预留 =================
s = base_slide("功能效果（一）：对话与记忆效果")
placeholder(s, 0.6, 1.6, 5.9, 2.5, "QQ 对话效果截图")
placeholder(s, 6.85, 1.6, 5.9, 2.5, "记忆召回 / 原话引用截图")
placeholder(s, 0.6, 4.35, 5.9, 2.5, "语音听说唱效果截图")
placeholder(s, 6.85, 4.35, 5.9, 2.5, "情绪表达 / 主动发起截图")

s = base_slide("功能效果（二）：多端与前端")
placeholder(s, 0.6, 1.6, 5.9, 2.5, "Flutter 多端界面")
placeholder(s, 6.85, 1.6, 5.9, 2.5, "3D 全息前端效果")
placeholder(s, 0.6, 4.35, 5.9, 2.5, "WebUI 控制台界面")
placeholder(s, 6.85, 4.35, 5.9, 2.5, "RVC 翻唱 / 画图效果")

s = base_slide("功能效果（三）：运行与观测")
for i, lab in enumerate(["观测台 / 数据看板", "成本与缓存诊断日志",
                         "多会话并发场景", "任务状态机回执"]):
    x = 0.6 + (i % 2) * 6.25
    y = 1.6 + (i // 2) * 2.75
    placeholder(s, x, y, 5.9, 2.5, lab)

# ================= 34. 封底 =================
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, DARK)
rect(s, 0, 3.9, 13.333, 0.1, RED)
textbox(s, 0, 2.5, 13.333, 1.2,
        [("感谢聆听，敬请指正", 36, True, WHITE)])
textbox(s, 0, 4.3, 13.333, 1.0,
        [("《识海》全栈智能体 · 深度项目调研", 16, False,
          RGBColor(0xCF, 0xE0, 0xF2))])

prs.save(str(OUT))
print("PPT 已生成：", OUT)
print("总页数：", len(prs.slides._sldIdLst))
