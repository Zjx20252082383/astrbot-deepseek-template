"""心理模块（论文 §8 人格心理学建模）。

六大心理学模型 → 可观测/可调参的心理状态：
    大五人格 ψ   : (O,C,E,A,N)，"性格基底"，月级慢漂移，初始化时设定（§4）
    依恋理论     : att（主人回复一致性 → send_threshold → 主动开口频率）
    认知失调     : O 决定新记忆与旧信念冲突时"更新"还是"合理化"
    条件反射     : 中性刺激反复配对后直接注入 s_int（ psyche 记录配对计数）
    防御机制     : 失败时按 N/A 选 合理化/压抑/投射 的风格
    自我效能 eff : 成功 +0.05 / 失败 -0.1，调制回复自信度

本插件拥有 psyche_state.json 的写权；核心每 tick 读取并应用到 z(t)。
数据目录 data/plugin_data/shihai/（契约目录名，不随插件改名变化）。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict

# 防御机制风格（论文 §8 表：N/A 决定失败时选合理化/压抑/投射）
DEFENSE_STYLES = {
    "auto": "由 N/A 自动选择：A 高→合理化（为自己开脱），N 高→投射（怪环境），否则压抑（不提）",
    "rationalize": "合理化：失败总能给自己找到一个说得过去的理由",
    "suppress": "压抑：很少主动提失败，翻篇很快",
    "project": "投射：容易把挫折归因到外部（天气/运气/别人）",
}

# 大五 → 心理解读短语
PSI_TRAITS = {
    "O": ("开放性", "对新事物好奇、想法多" , "循规蹈矩、喜欢熟悉的东西"),
    "C": ("尽责性", "答应的事记很牢、做事有谱", "随性、有时忘事"),
    "E": ("外向性", "爱主动找人说话、热闹充电", "安静、被动等待为主"),
    "A": ("宜人性", "脾气好、不易发火、爱迁就", "有棱角、容易呛声"),
    "N": ("神经质", "敏感、容易担心、情绪起伏大", "心大、情绪稳"),
}

ATTACHMENT_STYLES = (
    (0.8, "安全型依恋：相信主人会回应，主动联系得很自然"),
    (0.5, "倾向安全型：大部分时候敢主动，偶尔患得患失"),
    (0.3, "矛盾型依恋：想主动又怕打扰，主动前会反复掂量"),
    (0.0, "回避倾向：被冷落过太多次，主动联系变得谨慎"),
)


def attachment_style(att: float) -> str:
    for th, desc in ATTACHMENT_STYLES:
        if att >= th:
            return desc
    return ATTACHMENT_STYLES[-1][1]


def defense_style_desc(style: str, psi: Dict[str, float]) -> str:
    if style not in DEFENSE_STYLES:
        style = "auto"
    if style == "auto":
        if psi.get("A", 0.5) >= 0.6:
            return "失败时倾向合理化（宜人性高，不迁怒）"
        if psi.get("N", 0.5) >= 0.6:
            return "失败时倾向投射（神经质高，容易怪外部）"
        return "失败时倾向压抑（不多说，翻篇）"
    return DEFENSE_STYLES[style]


def default_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """psyche_state.json 的初始内容（也用于损坏兜底）。"""
    return {
        "psi": {
            "O": float(cfg.get("O_init", 0.55)),
            "C": float(cfg.get("C_init", 0.45)),
            "E": float(cfg.get("E_init", 0.60)),
            "A": float(cfg.get("A_init", 0.70)),
            "N": float(cfg.get("N_init", 0.35)),
        },
        "theta": {
            "eff": float(cfg.get("eff_init", 0.60)),
            "send_threshold": float(cfg.get("att_init", 1.0)),
            "tantrum_threshold": float(cfg.get("tantrum_init", 0.60)),
        },
        "defense_style": str(cfg.get("defense_style", "auto")),
        "conditioning": {},  # 条件反射配对计数：刺激词 → 配对次数
        "feedback_counts": {"positive": 0, "negative": 0, "short": 0, "strong_neg": 0},
        "extra_lexicon": {
            "positive": [w for w in str(cfg.get("lexicon_positive", "")).replace("，", ",").split(",") if w.strip()],
            "negative": [w for w in str(cfg.get("lexicon_negative", "")).replace("，", ",").split(",") if w.strip()],
        },
    }


def load_state(path: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    data = default_state(cfg)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                saved = json.load(fh)
            for section in ("psi", "theta"):
                if isinstance(saved.get(section), dict):
                    for k in list(data[section]):
                        try:
                            data[section][k] = float(saved[section].get(k, data[section][k]))
                        except (TypeError, ValueError):
                            pass
            if saved.get("defense_style") in DEFENSE_STYLES:
                data["defense_style"] = saved["defense_style"]
            if isinstance(saved.get("conditioning"), dict):
                data["conditioning"] = {str(k): int(v) for k, v in
                                        saved["conditioning"].items()
                                        if isinstance(v, (int, float))}
            if isinstance(saved.get("feedback_counts"), dict):
                for k in data["feedback_counts"]:
                    data["feedback_counts"][k] = int(saved["feedback_counts"].get(k, 0))
            if isinstance(saved.get("extra_lexicon"), dict):
                for k in ("positive", "negative"):
                    words = saved["extra_lexicon"].get(k, [])
                    if isinstance(words, list):
                        data["extra_lexicon"][k] = [str(w) for w in words if str(w).strip()]
        except (OSError, ValueError, TypeError):
            pass
    return data


def save_state(path: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def profile_text(data: Dict[str, Any]) -> str:
    """心理侧写：把 psyche_state 翻译成一段自然语言报告（psyche_report 工具用）。"""
    psi = data["psi"]
    rows = []
    for key, (cn, hi, lo) in PSI_TRAITS.items():
        v = psi.get(key, 0.5)
        rows.append(f"{cn} {int(v*100)}%（{'偏'+hi if v >= 0.55 else '偏'+lo if v <= 0.35 else '中间'}）")
    theta = data["theta"]
    fc = data["feedback_counts"]
    lines = [
        "【识海·心理侧写】",
        "人格：" + "；".join(rows),
        f"依恋：{attachment_style(theta.get('send_threshold', 1.0))}",
        f"自我效能：{int(theta.get('eff', 0.6)*100)}%（"
        + ("挺自信" if theta.get("eff", 0.6) >= 0.7 else
           "有点不自信" if theta.get("eff", 0.6) <= 0.35 else "一般") + "）",
        f"防御机制：{defense_style_desc(data.get('defense_style', 'auto'), psi)}",
        "历史反馈：好评{}次 / 冷淡{}次 / 强负{}次".format(
            fc.get("positive", 0), fc.get("short", 0), fc.get("strong_neg", 0)),
    ]
    cond = data.get("conditioning", {})
    if cond:
        tops = sorted(cond.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lines.append("条件反射配对：" + "、".join(f"{k}×{v}" for k, v in tops))
    return "\n".join(lines)
