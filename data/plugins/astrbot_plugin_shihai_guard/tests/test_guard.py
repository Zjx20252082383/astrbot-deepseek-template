"""守护层单测（不依赖 AstrBot，直接 pytest）。

覆盖三类故障的复现与拦截：
  test_case_completed_exam      记忆错乱：已完成事项被当作待办
  test_quote_isolation          上下文割裂：自己说过的话被当作事实复述
  test_persona_state_assertion  人格分裂：静态断言与 ψ 抢解释权
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guard import channels, persona, postcheck, timeline  # noqa: E402


def _node(nid, content, hours_ago, ntype="event", a=0.5):
    return {"id": nid, "content": content, "type": ntype,
            "created": time.time() - hours_ago * 3600.0, "a": a}


def test_when_label_boundaries():
    assert timeline.when_label(0.2 * 3600) == "刚刚聊过"
    assert timeline.when_label(4 * 3600) == "4 小时前聊过"
    assert timeline.when_label(26 * 3600) == "昨天聊过"
    assert timeline.when_label(3 * 86400) == "3 天前聊过"
    assert timeline.when_label(14 * 86400) == "2 周前聊过"
    assert timeline.when_label(90 * 86400) == "3 个月前聊过"


def test_case_completed_exam():
    """R_t 必须把"已考完"排在"明天要考"之前，时序纪律才可能被遵守。"""
    nodes = [
        _node("event:old", "聊到：明天上午考试呀", 17),
        _node("event:new", "聊到：已经考完了呀", 4),
    ]
    r_t = timeline.build_r_t(nodes, now=time.time(), limit=4)
    assert r_t[0]["id"] == "event:new"
    assert "4 小时前" in r_t[0]["when"]
    assert timeline.check_temporal_order(r_t)
    rendered = timeline.render_r_t(r_t)
    assert rendered.index("已经考完了") < rendered.index("明天上午考试")


def test_quote_isolation():
    nodes = [
        _node("event:1", "聊到：今天下雨了", 2),
        _node("quote:1", "呜，今天好想窝着不动", 2, ntype="quote", a=0.9),
    ]
    g_active = channels.filter_facts(nodes)
    r_t = timeline.build_r_t(nodes, now=time.time())
    quotes = channels.filter_quotes(nodes)
    audit = channels.audit_channels(g_active, [], r_t)
    assert all(v == 0 for v in audit.values())
    assert [q["id"] for q in quotes] == ["quote:1"]
    assert "不要照抄" in channels.render_bot_quotes(quotes)


def test_persona_state_assertion():
    bad = "你总是元气满满，绝不当冷淡。"
    assert persona.scan_persona(bad)["violations"] >= 2
    ok = "说话短一点，多用语气词。"
    assert persona.scan_persona(ok)["pass"]


def test_style_params_bounds_and_monotonic():
    lo = persona.style_params({"E": 0.0, "N": 0.0, "A": 0.0, "C": 0.0, "O": 0.0})
    hi = persona.style_params({"E": 1.0, "N": 1.0, "A": 1.0, "C": 1.0, "O": 1.0})
    for k in lo:
        assert 0.0 <= lo[k] <= 1.0 and 0.0 <= hi[k] <= 1.0
    assert hi["initiative"] > lo["initiative"]
    assert hi["sentence_len"] < lo["sentence_len"]


def test_arbitrate_rejects_overlap():
    """I4：状态字段与风格字段键空间必须不相交。"""
    try:
        persona.arbitrate({"emotion": 0.3, "style_params": {}},
                          {"style_params": {}})
    except ValueError:
        return
    raise AssertionError("字段重叠未被拒绝，I4 失效")


def test_postcheck_flags_over_expression():
    chk = postcheck.check_against_state("超级超级难过！！救命", "sad", 0.2, 0.2)
    assert chk["action"] == "downgrade"
    assert chk["deviation"] > 0.35
    assert "超级" not in postcheck.downgrade("超级难过！！")


def test_temporal_claim_detector():
    assert postcheck.check_temporal_claim("考完了没，抓紧做题")["nudge"]
    assert not postcheck.check_temporal_claim("上次说考完了，辛苦啦")["nudge"]
