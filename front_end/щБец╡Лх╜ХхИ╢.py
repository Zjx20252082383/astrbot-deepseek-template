# -*- coding: utf-8 -*-
"""
遥测录制（独立脚本，不依赖、不修改 AstrBot 插件本体）
每秒读一次 http://127.0.0.1:6191/api/state，把关键输入/输出参数追加到
assets/history.jsonl，供观测台「时间曲线」视图按多种时间刻度回放。

用法：  python 遥测录制.py    （建议用隐藏窗口后台运行）
"""
import json
import math
import os
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "history.jsonl")


def _read_token():
    """从 serve_config.json 取访问令牌，保持令牌只在一处维护。"""
    try:
        with open(os.path.join(HERE, "serve_config.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("token", ""))
    except (OSError, ValueError):
        return ""


TOKEN = _read_token()
STATE_URL = "http://127.0.0.1:6191/api/state" + (("?token=" + TOKEN) if TOKEN else "")
KEEP_SECONDS = 24 * 3600
MAX_BYTES = 32 * 1024 * 1024


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def extract(state):
    e = state.get("e", {}) or {}
    enorm = math.sqrt(sum(_num(v) ** 2 for v in e.values()))
    theta = state.get("theta", {}) or {}
    psi = state.get("psi", {}) or {}
    graph = state.get("graph", {}) or {}
    return {
        "t": int(time.time()),
        "urge": round(_num(state.get("urge")), 4),
        "theta": round(_num(theta.get("eff")), 4),
        "r": round(_num(state.get("r_feedback")), 4),
        "enorm": round(enorm, 4),
        "edom": round(_num(state.get("e_dom_value")), 4),
        "psiE": round(_num(psi.get("E")), 4),
        "edges": int(_num(graph.get("edges"))),
        "nodes": int(_num(graph.get("nodes"))),
        "suppress": int(_num(state.get("suppress_until"))),
        "streak": int(_num(state.get("streak_count"))),
    }


def trim_if_needed():
    try:
        if os.path.getsize(OUT) < MAX_BYTES:
            return
        cutoff = time.time() - KEEP_SECONDS
        kept = []
        with open(OUT, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if _num(row.get("t")) >= cutoff:
                    kept.append(line)
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(kept)
        os.replace(tmp, OUT)
    except OSError:
        pass


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    while True:
        try:
            with urllib.request.urlopen(STATE_URL, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("status") == "ok":
                inner = payload.get("payload", {}) or {}
                state = inner.get("state")
                if isinstance(state, dict) and inner.get("status") == "ok":
                    row = extract(state)
                    with open(OUT, "a", encoding="utf-8", newline="") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    trim_if_needed()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
            # 服务重启期间静默等待，不退出
            pass
        time.sleep(1.0)


if __name__ == "__main__":
    main()
