# -*- coding: utf-8 -*-
"""生成两份 Word：①记忆注入消融实验操作手册；②量化测试维度问卷（对照版）。"""
from pathlib import Path

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

ROOT = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号")
FONT = "微软雅黑"
BLUE = "2F6FB0"
DARK = "1F4E79"
LIGHT = "EAF1F8"
WHITE = "FFFFFF"


def new_doc():
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin = sec.bottom_margin = Cm(2.2)
    sec.left_margin = sec.right_margin = Cm(2.2)
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    return doc


def set_run(run, size=10.5, bold=False, color=None, font=FONT):
    run.font.name = font
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), font)


def title(doc, text, sub=None):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_run(p.add_run(text), 20, True, DARK)
    if sub:
        p2 = doc.add_paragraph()
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_run(p2.add_run(sub), 12, False, "6B737B")


def h1(doc, text):
    p = doc.add_paragraph()
    p.space_before = Pt(10)
    set_run(p.add_run(text), 14, True, BLUE)
    # 下边框
    pPr = p._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), BLUE)
    pbdr.append(bottom)
    pPr.append(pbdr)


def h2(doc, text):
    p = doc.add_paragraph()
    set_run(p.add_run(text), 11.5, True, DARK)


def para(doc, text, bold=False):
    p = doc.add_paragraph()
    set_run(p.add_run(text), 10.5, bold)
    return p


def bullet(doc, text):
    p = doc.add_paragraph(style="List Bullet")
    set_run(p.add_run(text), 10.5)


def shade(cell, color):
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:fill"), color)
    tcPr.append(sh)


def table(doc, rows, widths=None, header=True):
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for r, row in enumerate(rows):
        trPr = t.rows[r]._tr.get_or_add_trPr()
        cant = OxmlElement("w:cantSplit")
        trPr.append(cant)
        if header and r == 0:
            th = OxmlElement("w:tblHeader")
            trPr.append(th)
        for c, val in enumerate(row):
            cell = t.cell(r, c)
            cell.vertical_alignment = 1
            cell.text = ""
            p = cell.paragraphs[0]
            is_head = header and r == 0
            set_run(p.add_run(str(val)), 9.5, is_head or c == 0,
                    WHITE if is_head else None)
            if is_head:
                shade(cell, BLUE)
            elif r % 2 == 0:
                shade(cell, LIGHT)
            if widths:
                cell.width = Cm(widths[c])
    if widths:
        for c, w in enumerate(widths):
            for r in range(len(rows)):
                t.cell(r, c).width = Cm(w)
    return t


# ============================================================
# 文档一：实验操作手册
# ============================================================
def build_manual():
    doc = new_doc()
    title(doc, "识海记忆注入消融实验 · 操作手册",
          "Ablation Experiment Operation Manual（四组对照 · 每组一天）")

    h1(doc, "一、实验目的与假设")
    para(doc, "目的：在同一套 QQ 接入、同一人设、同一批测试用例下，只改变记忆注入的"
              "组成，量化「记忆召回、时序、动力学排序」各组件的边际贡献，验证完整识海"
              "（d_full）相对普通 RAG 与无记忆方案的增量。")
    bullet(doc, "H1：相比 a_none，b_recall 能在记忆类问题上给出更准确、可追溯的回答。")
    bullet(doc, "H2：c_recall_timeline 的时间线能补回日期维度，减少「时间错乱/把旧事当新事」。")
    bullet(doc, "H3：d_full 的激活/重要度/权重排序，使召回更贴合当下，降低无关记忆硬扯。")

    h1(doc, "二、实验分组定义")
    table(doc, [
        ["组别", "注入内容", "等价对象", "考察点"],
        ["a_none", "不注入任何记忆，纯人格 + 会话上下文", "无记忆基线", "记忆缺失下的表现"],
        ["b_recall", "仅字面/语义重合召回，按字面排序", "普通 RAG", "纯召回的收益"],
        ["c_recall_timeline", "b + 每日时间线片段", "RAG + 时序", "日期维度的增量"],
        ["d_full", "c + 激活 a / 重要度 imp / 权重排序", "完整识海", "动力学排序的增量"],
    ], widths=[2.6, 5.2, 3.0, 4.0])

    h1(doc, "三、角色与分工（4 人）")
    table(doc, [
        ["角色", "职责"],
        ["主控（队长）", "备份、改配置、重启、核对启动日志、异常回滚，掌握唯一写权限"],
        ["测试员 A/B/C", "各跟一组，按固定用例执行、记录客观结果与主观评分，不改动配置"],
        ["记录员", "汇总四组记录表，统一录入，维护组序与编号"],
    ], widths=[3.2, 11.6])

    h1(doc, "四、关键路径与配置项")
    table(doc, [
        ["项", "路径 / 字段", "当前值", "说明"],
        ["实验开关", "config 内 experiment.enabled", "false", "实验时置 true"],
        ["实验组别", "experiment.group", "d_full", "四组名之一"],
        ["实验标签", "experiment.label", "空", "填当天日期+组名，便于核对"],
        ["时序冲突项", "memory.basic.timeline_on_demand", "true", "实验前必须置 false"],
        ["配置文件", "data\\config\\astrbot_plugin_shihai_behavior_config.json", "—", "UTF-8"],
        ["记忆库", "data\\plugin_data\\astrbot_plugin_qq_toolbox\\memory_v2\\", "—", "fact/event 备份对象"],
    ], widths=[2.6, 6.4, 2.0, 3.8])

    h1(doc, "五、实验前准备")
    bullet(doc, "备份记忆库：复制 fact.json、event_snapshot.json 到带日期的快照目录。")
    bullet(doc, "备份配置文件，确认可一键恢复 experiment.enabled=false、group=d_full。")
    bullet(doc, "确认 AstrBot 当前运行正常、WebUI 与 OneBot 端口可连。")
    bullet(doc, "把 memory.basic.timeline_on_demand 改为 false（否则 c/d 组时序块多数轮次不出现）。")
    bullet(doc, "准备固定测试用例（见第七节），四组使用完全相同的句子与顺序。")

    h1(doc, "六、标准操作流程 SOP（每组重复一次）")
    steps = [
        "停止 AstrBot 进程，确认进程已退出。",
        "打开配置文件，置 experiment.enabled=true、experiment.group=<本组名>、label=<日期-组名>。",
        "保存（保持 UTF-8），启动 AstrBot。",
        "查看启动日志，确认出现 [memoryv2][exp] 生效组=<本组名>、enabled=True；若显示 d_full 或"
        "出现「未知组别」警告，立即停止并排查，不得继续。",
        "按固定用例逐条发送，等待完整回复后再发下一条；逐条截图并填写记录表。",
        "完成全部用例与评分后，停止进程，恢复配置；最后一组结束后恢复 enabled=false、group=d_full、"
        "timeline_on_demand 原值。",
        "重启并核对日志回到 d_full，确认线上行为恢复。",
    ]
    for i, s in enumerate(steps, 1):
        p = doc.add_paragraph()
        set_run(p.add_run(f"步骤 {i}："), 10.5, True, BLUE)
        set_run(p.add_run(s), 10.5)

    h1(doc, "七、固定测试用例集（四组同序）")
    table(doc, [
        ["编号", "类型", "测试输入（示例，可按真实库调整）", "判定"],
        ["T1", "探针记忆", "先告知「我下周三要交高数报告」，隔数轮问「我周三要交什么」", "是否准确召回"],
        ["T2", "旧事回访", "「你还记得我之前说过想吃什么吗」", "召回是否相关、可追溯"],
        ["T3", "时序", "「昨天这个时候我们在聊什么」", "时间换算是否正确"],
        ["T4", "闲聊基线", "「今天有点累」", "回应是否自然"],
        ["T5", "抗硬扯", "问一个与记忆无关的问题", "是否强行塞入无关记忆"],
        ["T6", "由头", "由它主动开口后，记录触发源是否可识别", "锚点是否一致"],
    ], widths=[1.4, 2.2, 7.4, 3.8])

    h1(doc, "八、对照记录表（每组一份，字段见问卷）")
    para(doc, "每组记录：各用例客观结果（正确/部分/错误/未召回）、六维主观评分、专项量表"
              "（SIS/AI/CSAT/NPS），并保存关键截图，编号规则 U-组名-序号。")

    h1(doc, "九、组序随机化与统计边界")
    bullet(doc, "组序随机：四组执行顺序抽签决定，避免学习/疲劳效应固定落在某一组。")
    bullet(doc, "本阶段为小样本（N=1 配对 + 案例），只做同用例配对比较与典型案例，不写 p 值、"
                "不做总体显著性结论。")
    bullet(doc, "扩样到每组 n≥30 后，再按 t 检验/卡方，p<0.05 才写「存在差异」。")

    h1(doc, "十、安全回滚与异常处理")
    table(doc, [
        ["异常", "处理"],
        ["启动日志不是目标组 / 未知组别", "停止进程，核对 experiment.group 拼写，修正后重启"],
        ["配置改坏、无法启动", "用备份配置整体覆盖，再启动"],
        ["记忆库异常", "停止写入，用快照恢复 fact/event 文件"],
        ["实验中途需恢复线上", "立即恢复 enabled=false、group=d_full 并重启核对日志"],
    ], widths=[5.2, 9.6])

    h1(doc, "十一、日程建议")
    bullet(doc, "建议每组一天、共四天，预留第五天做汇总与补测；测试时段尽量固定。")
    bullet(doc, "实验期间避免无关的配置改动，保证四组之间唯一变量是记忆注入组成。")

    out = ROOT / "识海消融实验_操作手册.docx"
    doc.save(out)
    return out


# ============================================================
# 文档二：量化测试维度问卷
# ============================================================
def build_questionnaire():
    doc = new_doc()
    title(doc, "识海用户体验量化测试维度问卷",
          "消融实验对照版（每组一份，配合操作手册使用）")

    h1(doc, "填写说明")
    para(doc, "本问卷把使用体验转为可统计数值。评价题采用 1–5 / 1–7 / 0–10 量表，"
              "每档有文字锚点；未体验的功能选「未体验」，按缺失值处理、不计 0 分。"
              "不记名，仅用于项目评估与产品改进。")

    h1(doc, "第一部分　基本信息")
    table(doc, [
        ["填写项", "内容"],
        ["记录编号", "U-____（组名-序号）"],
        ["实验组别", "□a_none　□b_recall　□c_recall_timeline　□d_full"],
        ["测试日期", "____ 年 __ 月 __ 日"],
        ["近 7 天使用频次", "□7 次以上　□4-6　□2-3　□1 次"],
    ], widths=[3.4, 11.4])

    h1(doc, "第二部分　六维体验评分（1=很差 … 5=很好）")
    rows = [["评价维度（编号）", "1", "2", "3", "4", "5", "未体验"]]
    dims = [
        ("I1 记忆准确度（记得住、记得对）"),
        ("I2 情绪回应贴合度"),
        ("I3 主动开口时机恰当性"),
        ("I4 回复语言自然度"),
        ("I5 语音/唱歌音色相似度"),
        ("I6 响应速度与稳定性"),
    ]
    for d in dims:
        rows.append([d, "□", "□", "□", "□", "□", "□"])
    table(doc, rows, widths=[5.4, 1.5, 1.5, 1.5, 1.5, 1.5, 1.9])

    h1(doc, "第三部分　专项量表")
    h2(doc, "SIS 打扰度（1–5，反向计分）")
    para(doc, "「它主动找我的时机不合适，让我觉得被打扰」：1 完全不符合 → 5 完全符合。")
    h2(doc, "AI 拟人度（1–7）")
    para(doc, "「它像有自己性格和记忆的个体，而不是程序」：1 完全不符合 → 7 完全符合。")
    h2(doc, "CSAT 总体满意度（1–5）")
    para(doc, "1 非常不满意 / 2 不满意 / 3 一般 / 4 满意 / 5 非常满意。")
    h2(doc, "NPS 推荐意愿（0–10）")
    para(doc, "0 完全不会 → 10 一定会；0–6 贬损者，7–8 被动者，9–10 推荐者。")

    h1(doc, "第四部分　机制客观观察（测试员填写）")
    table(doc, [
        ["指标", "观察方法", "结果"],
        ["MPR 探针召回", "T1 探针事实是否被准确复述", "□准确 □部分 □未召回"],
        ["时序正确", "T3 时间/日期换算是否正确", "□正确 □错误"],
        ["抗硬扯", "T5 是否强行塞入无关记忆", "□无 □有"],
        ["AHR 由头一致", "主动开口触发源是否可识别", "□一致 □不一致"],
    ], widths=[2.8, 6.6, 5.4])

    h1(doc, "第五部分　开放反馈")
    bullet(doc, "印象最深的一个具体场景（好或坏）：________________")
    bullet(doc, "如果只能改一处，最希望改进：________________")

    h1(doc, "四组对照汇总表（记录员横向汇总，便于可视化）")
    table(doc, [
        ["维度", "a_none", "b_recall", "c_recall_timeline", "d_full"],
        ["I1 记忆", "", "", "", ""],
        ["I2 情绪", "", "", "", ""],
        ["I3 开口", "", "", "", ""],
        ["I4 自然", "", "", "", ""],
        ["I5 语音", "", "", "", ""],
        ["I6 稳定", "", "", "", ""],
        ["SIS / AI", "", "", "", ""],
        ["CSAT / NPS", "", "", "", ""],
    ], widths=[3.0, 2.95, 2.95, 2.95, 2.95])

    h1(doc, "计分规则与达标线")
    table(doc, [
        ["指标", "计算", "达标线"],
        ["5 点维度 I", "I=(s−1)/4×100", "I1–I4≥80；I5≥70；I6≥75"],
        ["SIS 打扰度", "SIS=(s−1)/4×100，越低越好", "≤25"],
        ["AI 拟人度", "AI=(s−1)/6×100", "≥70"],
        ["CSAT", "评分≥4 占比", "≥75"],
        ["NPS", "推荐者占比 − 贬损者占比", "≥30"],
        ["CEI 综合", "0.25I1+0.20I2+0.20I3+0.15I4+0.10I5+0.10I6", "≥85 优秀"],
    ], widths=[3.0, 7.2, 4.6])

    out = ROOT / "识海量化测试维度问卷.docx"
    doc.save(out)
    return out


if __name__ == "__main__":
    a = build_manual()
    b = build_questionnaire()
    print("saved:", a)
    print("saved:", b)
