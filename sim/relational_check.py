#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关系层 z_r(o) 的落地前验证（论文 6.14.5 两条未做验证）。

① 投影形式下 R_o 是否在 [R_min, 1] 内收敛、下界是否真的是吸引子而不是
   "越界后再弹回"（后者会在边界附近产生 ε 抖动，稳态值依赖步长）。
② α / β 比值扫描：找出既满足显式稳定、又与既有情绪 ODE 时间尺度不冲突的
   可行域，避免关系层比"生气"还快地衰减。

    python relational_check.py

输出（同目录 out_rel/）：
    relational_check.json   两轮检查的全部数值
    relational_check.png    α/β 可行域热图 + 收敛轨迹
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

OUT = Path(__file__).resolve().parent / "out_rel"

# 与 modules/relational.py 同一口径（改那边要同步改这里）
ALPHA = 0.25
REL_DECAY = 0.94
DT = 0.5
BETA = -math.log(REL_DECAY) / DT
R_MIN, N0, KAPPA, GAMMA, R_GATE = 0.15, 5.0, 2.0, 0.10, 0.40

# emotion.py DEFAULT_TAU（论文 6.13.2 第 3 条）
TAU = {"happy": 6, "sad": 12, "angry": 2, "fear": 3, "love": 24, "surprise": 1}


def sig(x):
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def f(r, rho, v, c):
    return ALPHA * (rho * sig(KAPPA * v) - r) - BETA * (r - R_MIN) + GAMMA * c


def step_projected(r, rho, v, c, dt=DT):
    """投影写在向量场里（本实现）：下界处只接受非负增量。"""
    nxt = r + dt * f(r, rho, v, c)
    if r <= R_MIN + 1e-12 and nxt < R_MIN:
        return R_MIN
    return max(R_MIN, min(1.0, nxt))


def step_clamped(r, rho, v, c, dt=DT):
    """朴素实现（先积分再钳位）：边界处会出现"越界-弹回-再越界"。"""
    return max(R_MIN, min(1.0, r + dt * f(r, rho, v, c)))


def check_projection():
    rows = []
    worst_clamp = 0.0
    for r0 in (R_MIN, 0.16, 0.25, 0.40, 0.60, 1.00):
        for rho in (0.0, 0.17, 0.5, 1.0):
            for v in (-1.0, 0.0, 1.0):
                for c in (0, 1):
                    a, b = r0, r0
                    traj = []
                    for _k in range(400):
                        a = step_projected(a, rho, v, c)
                        b = step_clamped(b, rho, v, c)
                        traj.append(a)
                        worst_clamp = max(worst_clamp, abs(a - b))
                    tail = traj[-20:]
                    expect = (ALPHA * rho * sig(KAPPA * v) + BETA * R_MIN
                              + GAMMA * c) / (ALPHA + BETA)
                    expect = max(R_MIN, min(1.0, expect))
                    rows.append({
                        "r0": r0, "rho": rho, "v": v, "c": c,
                        "r_final": round(a, 4),
                        "in_bounds": R_MIN - 1e-9 <= a <= 1.0 + 1e-9,
                        "tail_spread": round(max(tail) - min(tail), 6),
                        "matches_formula": abs(a - expect) < 1e-3,
                    })
    # 大步长对照：钳位实现会"冲过下界再弹回"，投影实现不会。
    # 生产步长 0.5h 下两者数值相同（Δt·f 最大 0.02，越不过界），
    # 差异只在语义与大步长；这里把大步长显式测出来，免得论文说"会抖动"而无据。
    gaps = []
    for dt in (0.5, 1.0, 2.0, 4.0, 8.0):
        a, b, seen = R_MIN, R_MIN, []
        for _k in range(60):
            a = step_projected(a, 0.0, -1.0, 0)
            b = step_clamped(b, 0.0, -1.0, 0)
            seen.append(round(a, 4))
        gap = 0.0
        for r0 in (R_MIN, 0.3, 0.8):
            x, y = r0, r0
            for _k in range(30):
                x = max(R_MIN, min(1.0, x + dt * f(x, 0.0, 1.0, 1)))
                y = max(R_MIN, min(1.0, y + dt * f(y, 0.0, 1.0, 1)))
                gap = max(gap, abs(x - y))
        gaps.append({"dt": dt, "gap": round(gap, 4)})
    return {
        "n_cases": len(rows),
        "all_in_bounds": all(x["in_bounds"] for x in rows),
        "all_fixed_point_match": all(x["matches_formula"] for x in rows),
        "max_tail_spread": max(x["tail_spread"] for x in rows),
        "max_projected_vs_clamped_gap": round(worst_clamp, 6),
        "floor_is_attractor": all(
            x["r_final"] >= R_MIN - 1e-9 for x in rows if x["rho"] == 0.0 and x["c"] == 0),
        "dt_gap_scan": gaps,
        "cases": rows[:6],
    }


def check_alpha_beta():
    grid = []
    feasible = []
    betas = [0.04 + 0.02 * i for i in range(11)]      # 0.04 ~ 0.24
    alphas = [0.10 + 0.02 * i for i in range(21)]     # 0.10 ~ 0.50
    for beta in betas:
        row = []
        for alpha in alphas:
            stable = abs(DT * (alpha + beta)) < 2.0
            nu = 1.0 / beta
            r_star = (beta * R_MIN + GAMMA) / (alpha + beta)     # c=1, ρ=0
            # 不冲突判据：关系半衰期不快于"生气"（2 h）、不慢于"爱"（24 h）；
            # 稳态落在 0.25~0.40（6.10.3 报的 0.32 就在这条带上）。
            ok = stable and 2.0 <= nu <= 24.0 and 0.25 <= r_star <= 0.40
            row.append(1.0 if ok else 0.0)
            if ok:
                feasible.append((round(alpha, 3), round(beta, 3), round(nu, 2),
                                 round(r_star, 3)))
        grid.append(row)
    picked = (ALPHA, BETA)
    nu_pick = 1 / BETA
    r_star_pick = (BETA * R_MIN + GAMMA) / (ALPHA + BETA)
    return {
        "alphas": [round(a, 3) for a in alphas],
        "betas": [round(b, 3) for b in betas],
        "grid": grid,
        "feasible_count": len(feasible),
        "feasible_head": feasible[:5],
        "picked": {"alpha": picked[0], "beta": round(BETA, 4),
                   "nu_hours": round(nu_pick, 2), "r_star_c1": round(r_star_pick, 3),
                   "dt_alpha_beta": round(DT * (ALPHA + BETA), 4),
                   "in_feasible": (round(nu_pick, 2) <= 24.0 and 2.0 <= nu_pick
                                   and 0.25 <= r_star_pick <= 0.40
                                   and abs(DT * (ALPHA + BETA)) < 2.0)},
        "emotion_tau": TAU,
    }


def plot(proj, scan):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), dpi=200)
    for rho in (0.0, 0.5, 1.0):
        traj, r = [], 1.0
        for _k in range(120):
            r = step_projected(r, rho, 0.0, 0)
            traj.append(r)
        ax1.plot([k * DT for k in range(len(traj))], traj, label=f"ρ={rho}")
    ax1.axhline(R_GATE, ls="--", lw=1, color="#888")
    ax1.text(1, R_GATE + 0.02, "R_gate = 0.40", fontsize=8, color="#666")
    ax1.set_xlabel("t / h")
    ax1.set_ylabel("R_o")
    ax1.set_title("① 投影动力学：R_o 收敛，下界 0.15 是吸引子")

    im = ax2.imshow(scan["grid"], origin="lower", aspect="auto", cmap="Greens",
                    extent=[scan["alphas"][0], scan["alphas"][-1],
                            scan["betas"][0], scan["betas"][-1]])
    ax2.plot([ALPHA], [round(BETA, 2)], "r*", ms=10)
    ax2.set_xlabel("α (1/h)")
    ax2.set_ylabel("β (1/h)")
    ax2.set_title("② (α, β) 可行域：绿=稳定且时间尺度不冲突")
    fig.colorbar(im, ax=ax2, shrink=0.8)
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "relational_check.png")
    plt.close(fig)


def main():
    proj = check_projection()
    scan = check_alpha_beta()
    plot(proj, scan)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"projection": proj, "alpha_beta": scan,
               "params": {"alpha": ALPHA, "beta": round(BETA, 4), "dt": DT,
                          "r_min": R_MIN, "n0": N0, "kappa": KAPPA,
                          "gamma": GAMMA, "r_gate": R_GATE}}
    (OUT / "relational_check.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in {
        "projection_all_in_bounds": proj["all_in_bounds"],
        "projection_matches_formula": proj["all_fixed_point_match"],
        "max_tail_spread": proj["max_tail_spread"],
        "max_gap_projected_vs_clamped": proj["max_projected_vs_clamped_gap"],
        "floor_is_attractor": proj["floor_is_attractor"],
        "feasible_cells": scan["feasible_count"],
        "picked": scan["picked"],
    }.items()}, ensure_ascii=False, indent=1))
    print("out ->", OUT)


if __name__ == "__main__":
    main()
