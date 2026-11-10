"""识海·一致性守护层（Consistency Guard）。

对论文 §7.5 / §7.6 / §18 的工程落地：把"记忆错乱、上下文割裂、人格分裂"
三类故障分别钉在三个拦截点上，且每个拦截点都只读 z(t)、不写 z(t)。

    P1 写入侧  channels   —— quote 与事实分流（I2）
    P2 检索侧  timeline   —— when 标签 + R_t（I1）
    P3 生成侧  persona    —— S/M 归界与 ψ 渲染风格（I3/I4）
           postcheck  —— 生成后强度与时态校验（闭环）

纯函数优先：core 不依赖 AstrBot，可单测；main.py 仅做框架适配。
"""

from . import channels, persona, postcheck, timeline  # noqa: F401

__all__ = ["channels", "persona", "postcheck", "timeline"]
