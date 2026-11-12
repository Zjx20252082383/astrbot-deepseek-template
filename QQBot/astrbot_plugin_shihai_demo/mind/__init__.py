"""识海心智核（Core-Mind Runtime）。

论文《识海：融合人格心理学的QQ数字生命耦合动力系统》§13.1：
本包为纯 Python、平台无关的状态动力学实现，不 import 任何 AstrBot 模块。
状态向量 z(t) = [e(t), a(v,t), θ(t), ψ, u(t), g(t)]，六分量按论文五个方程
（情绪场 5.1 / 图谱扩散 5.3 / Hebbian 5.5 / 念头 5.6 / 自适应 5.7）耦合，
形成四个耦合回路（§5.10）。

分层契约（进程间通过 data/plugin_data/shihai/ 下的 JSON 单向流动，目录名解耦于插件名，可重命名）：
    mind_state.json   core → behavior/psyche/dafeiyu/ai_cover   z(t) 全量 + fire_pending
    shihai_graph.json core 内部                                  图谱本体
    psyche_state.json psyche → core                              ψ/效能/依恋（core 每 tick 读取）
    shihai_fired.json behavior → core                            主动消息发送回执
"""

from .state import MindState, EMOTION_DIMS
from .emotion import EmotionField
from .graph import MemoryGraph
from .urge import UrgeEngine
from .adapt import AdaptEngine
from .goals import goal_drift
from .classify import estimate_emotion
from .engine import MindEngine

__all__ = [
    "MindState",
    "EMOTION_DIMS",
    "EmotionField",
    "MemoryGraph",
    "UrgeEngine",
    "AdaptEngine",
    "goal_drift",
    "estimate_emotion",
    "MindEngine",
]
