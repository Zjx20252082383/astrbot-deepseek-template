"""§5.4 Hebbian 两道门的消融对照（非系统运行，按实现规则做抽样推演）。

规则与 shihai_sim.py 的 c13_hebbian_gates 一致：节点各自带 sender 与 created，
随机抽取候选对，按实现规则算实际 Δw。结论只在该规则与参数下成立。
"""
import argparse
import json
from pathlib import Path

import numpy as np

GAMMA = 0.06          # graph.py gamma_hebbian
NEAR_WINDOW = 24.0    # 时间邻近窗口（小时）
NEAR_SCALE = 0.2      # 邻近降权系数
N_OWNER, N_BOT = 30, 10
TRIALS = 4000
SEED = 20261110
OUT = Path(__file__).resolve().parent / "out"


def build_nodes(seed: int):
    rng = np.random.default_rng(seed)
    return ([("owner", float(rng.uniform(0, 240))) for _ in range(N_OWNER)]
            + [("bot", float(rng.uniform(0, 240))) for _ in range(N_BOT)])


def run(seed: int, cross_gate: bool, temporal_gate: bool) -> dict:
    rng = np.random.default_rng(seed)
    nodes = build_nodes(seed)
    cross_n = cross_e = near_n = 0
    near_w, far_w = [], []
    for _ in range(TRIALS):
        i, j = rng.integers(0, len(nodes), 2)
        if i == j:
            continue
        (su, tu), (sv, tv) = nodes[i], nodes[j]
        if su != sv:
            cross_n += 1
            if not cross_gate:
                cross_e += 1          # 跨主体连边 = 来源混淆
            continue
        if abs(tu - tv) < NEAR_WINDOW:
            near_n += 1
            near_w.append(GAMMA * (NEAR_SCALE if temporal_gate else 1.0))
        else:
            far_w.append(GAMMA)
    return {
        "cross_subject_candidates": cross_n,
        "cross_subject_edges": cross_e,
        "source_confusion_rate": round(cross_e / cross_n, 4) if cross_n else 0.0,
        "near_pair_candidates": near_n,
        "near_pair_mean_delta": round(float(np.mean(near_w)), 4) if near_w else 0.0,
        "far_pair_mean_delta": round(float(np.mean(far_w)), 4) if far_w else 0.0,
    }


GROUPS = [
    ("Full", True, True),
    ("w/o cross-subject gate", False, True),
    ("w/o temporal penalty", True, False),
    ("w/o both gates", False, False),
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Hebbian 两道门消融")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    rows = {}
    for name, cg, tg in GROUPS:
        rows[name] = run(args.seed, cg, tg)
    print(f"抽样 {TRIALS} 次 / seed={args.seed} / GAMMA={GAMMA}")
    print(f"{'配置':<24}{'跨主体边':>10}{'混淆率':>10}{'邻近Δw':>12}{'远端Δw':>10}")
    for name, _, _ in GROUPS:
        r = rows[name]
        print(f"{name:<24}{r['cross_subject_edges']:>10}"
              f"{r['source_confusion_rate']:>10.4f}"
              f"{r['near_pair_mean_delta']:>12.4f}{r['far_pair_mean_delta']:>10.4f}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ablation_hebbian.json").write_text(
        json.dumps({"trials": TRIALS, "seed": args.seed, "gamma": GAMMA,
                    "near_window_h": NEAR_WINDOW, "near_scale": NEAR_SCALE,
                    "groups": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写入", OUT / "ablation_hebbian.json")


if __name__ == "__main__":
    main()
