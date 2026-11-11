# -*- coding: utf-8 -*-
"""《识海》深度项目调研汇报 PPT 生成（python-pptx，本地 .pptx）。

多图表（成本/竞品 + 仿真）、含仿真效果、多预留功能效果图位置。
运行：python build_ppt.py
"""
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt, Emu
from pptx.enum.dml import MSO_LINE_DASH_STYLE

ROOT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号")
MEDIA = ROOT / "AstrBot_snowluma" / "paper" / "media"
ASSETS = ROOT / "ppt_assets"
OUT = ROOT / "识海_深度项目调研汇报.pptx"

# 配色
BLUE = RGBColor(0x2F, 0x6F, 0xB0)
DARK = RGBColor(0x1F, 0x4E, 0x79)
RED = RGBColor(0xC0, 0x39, 0x2B)
LIGHT = RGBColor(0xF4, 0xF6, 0xF8)
TXT = RGBColor(0x26, 0x2B, 0x33)
GRAY = RGBColor(0x6B, 0x73, 0x7B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CARDBG = RGBColor(0xEA, 0xF1, 0xF8)
FONT = "微软雅黑"

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]
PAGE = [0]


def _set_font(run, size, bold=False, color=TXT, name=FONT):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
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
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(6)
        r = p.add_run()
        r.text = text
        _set_font(r, size, bold, color)
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
                [(title, 25, True, WHITE)], anchor=MSO_ANCHOR.MIDDLE)
        if kicker:
            textbox(slide, 10.2, 0.12, 2.7, 0.9,
                    [(kicker, 11, False, RGBColor(0xCF, 0xE0, 0xF2))],
                    anchor=MSO_ANCHOR.MIDDLE)
    PAGE[0] += 1
    if title:
        textbox(slide, 12.4, 7.05, 0.7, 0.35,
                [(str(PAGE[0]), 10, False, GRAY)], )
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


# ---------------- 1. 封面 ----------------
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, DARK)
rect(s, 0, 5.0, 13.333, 0.12, RED)
textbox(s, 1.0, 1.7, 11.3, 1.6,
        [("《识海》全栈智能体", 44, True, WHITE)])
textbox(s, 1.0, 3.1, 11.3, 1.2,
        [("深度项目调研汇报：架构、记忆、情绪、执行与成本工程", 21, False,
          RGBColor(0xCF, 0xE0, 0xF2))])
textbox(s, 1.0, 5.4, 11.3, 1.4,
        [("基于 AstrBot v4.14.6 / OneBot v11 与 DeepSeek 模型", 14, False,
          RGBColor(0xB8, 0xCF, 0xE6)),
         ("汇报人：郑俊鑫　|　2026 年 11 月", 14, False,
          RGBColor(0xB8, 0xCF, 0xE6))])

# ---------------- 2. 目录 ----------------
s = base_slide("目录 / Contents")
toc = [
    ("01　项目概览与行业背景", "定位、趋势、竞品分析"),
    ("02　系统架构与核心机制", "记忆、情绪、执行、多会话"),
    ("03　仿真验证", "长时稳定与数值自洽"),
    ("04　成本工程", "真实账单、缓存诊断、降本路径"),
    ("05　功能效果与结论", "效果图预留、结论与展望"),
]
bullets(s, 1.2, 1.7, 11, 5, toc, size=19, gap=16)

# ---------------- 3. 项目概览 ----------------
s = base_slide("项目概览：一个会记事、有内心戏、能办事的持续陪伴体")
cards = [
    ("记忆", "认知图谱 + 逐字归档", "457 事实 / 467 事件 / 1171 逐字"),
    ("情绪", "ODE 动力学 + 自我调节", "6 情绪场 + 冲动累积与释放"),
    ("执行", "业务状态机 + 多端闭环", "QQ 动作 / 任务 / 主动发起"),
    ("成本", "缓存降本 + 通道对比", "等效输入降 72%，月成本可降 79%"),
]
cw, ch = 2.95, 2.3
for i, (h, m, d) in enumerate(cards):
    x = 0.55 + i * 3.12
    rect(s, x, 1.7, cw, ch, CARDBG)
    rect(s, x, 1.7, cw, 0.12, RED if i == 3 else BLUE)
    textbox(s, x + 0.18, 1.95, cw - 0.36, 1.9,
            [(h, 20, True, DARK), (m, 14, True, TXT), (d, 12, False, GRAY)])
bullets(s, 0.7, 4.4, 12, 2.4, [
    ("定位", "填补「陪伴能力强 × 系统动作能力强」的市场空档，区别于偏科型竞品。"),
    ("方法", "数学建模（ODE/艾宾浩斯）+ 心理学机制（Hebbian/自我调节）+ 工程闭环。"),
], size=15)

# ---------------- 4. 行业背景 ----------------
s = base_slide("行业背景：从单点问答走向持续、主动、具身的智能体")
bullets(s, 0.7, 1.6, 12, 4.5, [
    ("通用助手", "豆包/元宝/DeepSeek 强在问答与工具，弱在长期陪伴与人格连续。"),
    ("陪伴产品", "Replika/Nomi/Kindroid 强在情感陪伴，却难以操作外部系统、完成任务。"),
    ("系统助手", "小布/小爱/YOYO 强在设备控制，弱在情绪与长期记忆。"),
    ("趋势", "智能体正从「被动应答」转向「主动发起、跨会话记忆、能执行闭环」。"),
    ("机会", "把动力学心智与业务状态机结合，做全栈而非偏科的智能体。"),
], size=16)

# ---------------- 5. 竞品矩阵 ----------------
s = base_slide("竞品定位：陪伴能力 × 系统动作能力")
add_pic_fit(s, ASSETS / "competitor_matrix.png", 0.6, 1.4, 8.2, 5.7)
bullets(s, 9.0, 1.8, 3.9, 4.5, [
    ("空档", "右上象限尚无成熟产品。"),
    ("识海", "同时具备强陪伴与强动作。"),
    ("壁垒", "记忆+情绪+执行三位一体。"),
], size=14)

# ---------------- 6. 三维对比 ----------------
s = base_slide("三维对比：记忆 / 情绪 / 执行")
add_pic_fit(s, ASSETS / "three_dim_compare.png", 0.5, 1.5, 12.3, 5.4)

# ---------------- 7. 总体架构 ----------------
s = base_slide("总体架构：分层解耦，数据目录即契约")
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

# ---------------- 8. 记忆：认知图谱 ----------------
s = base_slide("记忆系统（一）：认知图谱而非死硬盘")
bullets(s, 0.7, 1.6, 12, 5, [
    ("分桶存储", "事实 Fact 与事件 Event 分桶，各自带重要度与衰减参数。"),
    ("艾宾浩斯衰减", "保持率 R=e^{-Δt/S}，稳定度 S 随重要度、情绪峰值、复习次数提升。"),
    ("Hebbian 边权", "共现强化语义边，边防错构：低质量、超长降级内容可检索但不进注入。"),
    ("重要度自评", "LLM 对新条目打 0–1 重要度，高重要度事件更慢遗忘。"),
    ("时间线注入", "每日一句补回日期维度，避免只剩几段老话。"),
], size=15)

# ---------------- 9. 记忆：逐字归档 ----------------
s = base_slide("记忆系统（二）：逐字归档与原文锚点（方案 A）")
bullets(s, 0.7, 1.55, 7.4, 5, [
    ("问题", "窗口外只剩摘要，逐字原文在 11-10 后断档。"),
    ("归档", "SQLite + WAL + FTS5，每轮 user/assistant 原文持续落库。"),
    ("锚点", "EventItem 增 transcript_turn_id，指向同轮逐字。"),
    ("按需检索", "recall_transcript 工具只在需要原话时调用，不常驻前缀。"),
    ("零缓存影响", "内容不进固定前缀，不降低缓存命中率。"),
], size=14)
add_pic_fit(s, MEDIA / "fig_07_differentiated_decay.png",
            8.3, 1.6, 4.6, 3.0)
placeholder(s, 8.3, 4.8, 4.6, 2.2, "逐字检索工具\n调用效果截图")

# ---------------- 10. 情绪：动力学心智 ----------------
s = base_slide("情绪系统：ODE 动力学心智，而非 AI 演员")
add_pic_fit(s, MEDIA / "fig_01_relaxation.png", 0.6, 1.5, 7.6, 5.4)
bullets(s, 8.4, 1.8, 4.5, 4.5, [
    ("情绪场", "多情绪连续 ODE，受刺激后自然松弛回落。"),
    ("冲动累积", "du/dt 累积，达阈值后释放并回落。"),
    ("自我调节", "EgoRegulator 会克制、会防御，产生真实内耗。"),
    ("人格参数", "神经质等参数改变响应模式。"),
], size=14)

# ---------------- 11. 执行：业务状态机 ----------------
s = base_slide("执行系统：业务状态机与多端回执闭环")
bullets(s, 0.7, 1.6, 12, 5, [
    ("QQ 动作", "改昵称/头像/签名、发空间、点赞评论、传话等真实平台操作。"),
    ("任务情境", "课表、驿站、步行分钟等情境驱动，处理出游突变等复杂流程。"),
    ("主动发起", "行为由情绪/目标阈值涌现，满足条件即主动执行，无需改代码。"),
    ("回执闭环", "发送回执落盘，下一 tick 消费，确认动作真正完成。"),
    ("工具调用", "LLM 通过 @llm_tool 决策，最大 30 步、超时 600s。"),
], size=15)

# ---------------- 12. 多会话机制 ----------------
s = base_slide("多会话机制：会话隔离，作用域不串扰")
bullets(s, 0.7, 1.6, 12, 5, [
    ("并发回复", "按 unified_msg_origin 独立管理回复任务，打断不影响其他会话。"),
    ("焦点落盘", "每个会话的回复焦点独立保存，互不覆盖。"),
    ("归档隔离", "逐字归档按 origin 区分；长期记忆按 sender 归并同一人。"),
    ("锁机制", "框架按会话加锁，避免同一会话并发竞争。"),
], size=16)

# ---------------- 13. 仿真设置 ----------------
s = base_slide("仿真验证（一）：设置与方法")
bullets(s, 0.7, 1.6, 12, 5, [
    ("求解器", "RK4 积分，步长 0.25h，保证数值精度。"),
    ("刺激", "随机消息刺激，模拟真实对话节奏。"),
    ("时长", "200h 长时运行，检验稳定性与边界。"),
    ("五道防线", "饱和、阈值、门控等保护机制全部开启。"),
    ("检查项", "松弛、耦合、Hebbian、衰减、扰动、极端饱和等 15 项。"),
], size=16)

# ---------------- 14. 长时稳定 ----------------
s = base_slide("仿真验证（二）：200h 长时稳定")
add_pic_fit(s, MEDIA / "fig_05_long_run_200h.png", 0.6, 1.4, 12.1, 5.7)

# ---------------- 15. 仿真多图 ----------------
s = base_slide("仿真验证（三）：耦合、衰减与防错构")
add_pic_fit(s, MEDIA / "fig_02_emotion_memory_coupling.png",
            0.5, 1.5, 6.2, 2.8)
add_pic_fit(s, MEDIA / "fig_12_fault_rate.png", 6.9, 1.5, 6.0, 2.8)
add_pic_fit(s, MEDIA / "fig_11_ego_release.png", 0.5, 4.4, 6.2, 2.7)
add_pic_fit(s, MEDIA / "fig_03_hebbian.png", 6.9, 4.4, 6.0, 2.7)

# ---------------- 16. 账单画像 ----------------
s = base_slide("成本工程（一）：真实账单画像（11 天 4,231 笔）")
add_pic_fit(s, ASSETS / "billing_tiers.png", 0.55, 1.5, 8.0, 5.4)
bullets(s, 8.8, 1.8, 4.2, 4.5, [
    ("总支出", "56.86 元，日均约 5.17。"),
    ("长尾", "55% 小额请求仅占 7.5%。"),
    ("集中", "8% 大额请求占一半成本。"),
    ("后台", "约 352 次/天小请求，占约 60%。"),
], size=13.5)

# ---------------- 17. 缓存诊断 ----------------
s = base_slide("成本工程（二）：前缀缓存诊断与分段丢弃")
add_pic_fit(s, ASSETS / "cache_hit_compare.png", 0.5, 1.5, 6.2, 3.0)
add_pic_fit(s, ASSETS / "equiv_input_compare.png", 6.85, 1.5, 6.1, 3.0)
bullets(s, 0.7, 4.7, 12, 2.4, [
    ("根因", "滑动窗口逐轮位移，固定前缀仅命中 system+schema（约 17.4k）。"),
    ("修复", "dequeue 1→10，一次丢 10 轮，后续 9 轮前缀稳定，缓存命中升至 98.6%。"),
    ("效果", "单次等效输入 29,171→8,127，降 72.1%；10 轮平均降约 67%。"),
], size=13.5)

# ---------------- 18. 通道对比 ----------------
s = base_slide("成本工程（三）：通道对比与峰谷映射")
add_pic_fit(s, ASSETS / "monthly_cost_channel.png", 0.5, 1.5, 6.3, 3.1)
add_pic_fit(s, ASSETS / "peak_valley_share.png", 6.95, 1.6, 6.0, 2.9)
bullets(s, 0.7, 4.8, 12, 2.2, [
    ("月成本", "百炼 127.6 元 vs 官方约 26–28 元，渠道倍数约 4.8。"),
    ("峰谷", "真实闲时约 53%–66%，中值约 60%；闲时输出单价减半。"),
], size=13.5)

# ---------------- 19. 成本构成与降本 ----------------
s = base_slide("成本工程（四）：成本构成与降本路径")
add_pic_fit(s, ASSETS / "cost_composition.png", 0.6, 1.5, 5.4, 5.4)
bullets(s, 6.3, 1.6, 6.6, 5, [
    ("已落地", "缓存分段丢弃，等效输入降 72%。"),
    ("可选项", "切换 DeepSeek 官方通道，月成本降约 79%。"),
    ("待优化", "后台小请求占比高，需埋点拆分成分后再治理。"),
    ("调度", "可将 cron 类任务挪到闲时，利用峰谷价差。"),
    ("原则", "先测量、后优化，避免以牺牲响应方式换成本。"),
], size=14)

# ---------------- 20-22. 功能效果图预留 ----------------
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

# ---------------- 23. 结论与展望 ----------------
s = base_slide("结论与展望")
bullets(s, 0.7, 1.6, 12, 4.6, [
    ("结论一", "全栈智能体可行：记忆、情绪、执行在同一动力学底座上协同。"),
    ("结论二", "仿真 200h 稳定，防线有效，防错构机制抑制幻觉。"),
    ("结论三", "成本可量化、可治理：缓存降本已落地，通道切换可再降 79%。"),
    ("展望一", "拆分后台小请求成分，进一步精细化降本。"),
    ("展望二", "完善记忆权重与复习机制，补全功能效果图与端到端验证。"),
], size=16)

# ---------------- 24. 封底 ----------------
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, DARK)
rect(s, 0, 3.9, 13.333, 0.1, RED)
textbox(s, 0, 2.5, 13.333, 1.2,
        [("感谢聆听，敬请指正", 36, True, WHITE)], )
textbox(s, 0, 4.3, 13.333, 1.0,
        [("《识海》全栈智能体 · 深度项目调研", 16, False,
          RGBColor(0xCF, 0xE0, 0xF2))])

prs.save(str(OUT))
print("PPT 已生成：", OUT)
print("总页数：", len(prs.slides._sldIdLst))
