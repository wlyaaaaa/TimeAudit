# -*- coding: utf-8 -*-
"""审计: σ 敏感性 / 基准名次敏感性 / 多种子稳定性"""
import statistics, time, sys
import field_sim as F
from analyze import analyze, decomp, rank_index, summarize

def quick(res):
    out=analyze(res)
    d=out["decomp"]
    return dict(
        gold5_luck=d["金头-第5名"]["luck"]*100, gold5_tech=d["金头-第5名"]["tech"]*100,
        gold10_luck=d["金头门槛-第10名"]["luck"]*100,
        cop5000_luck=d["中位铜头-第5000名"]["luck"]*100, cop5000_tech=d["中位铜头-第5000名"]["tech"]*100,
        cop10000_luck=d["铜头门槛-第10000名"]["luck"]*100,
        cop_med_games=out["copper_median_rank5000_games"], cop_games_all=out["copper_games"]["p50"],
        probe_mean=out["probe_dp200"]["mean"], probe_sd=out["probe_dp200"]["sd"],
        gold5_games=d["金头-第5名"]["games"], gold5_wr=d["金头-第5名"]["total_wr"]*100,
    )

print("="*70)
print("A) σ 敏感性 (base_rank=8000, seed=固定)")
print("-"*70)
print("{:>5} | 金头5 运/技 | 金头10运 | 铜5000 运/技 | 铜10000运 | 铜场次 | 探针200(均/SD) | 金头WR/场次".format("σ"))
for sg in [0.8, 0.9, 1.0, 1.1, 1.2]:
    res=F.run_field(sg); q=quick(res)
    print("{:>5.2f} | {:4.1f}/{:4.1f}  | {:5.1f}   | {:4.1f}/{:4.1f}   | {:5.1f}    | {:3.0f}/{:3.0f} | {:5.0f}/{:4.0f}    | {:.0f}%/{}".format(
        sg, q["gold5_luck"],q["gold5_tech"], q["gold10_luck"],
        q["cop5000_luck"],q["cop5000_tech"], q["cop10000_luck"],
        q["cop_med_games"],q["cop_games_all"], q["probe_mean"],q["probe_sd"],
        q["gold5_wr"],q["gold5_games"]))

print("\n"+"="*70)
print("B) 基准名次敏感性 (σ=1.0) — 运气局判定门槛=第 base_rank 名")
print("-"*70)
print("{:>9} | 金头5 运/技 | 铜5000 运/技".format("base_rank"))
for br in [5000, 6000, 8000, 10000, 12000]:
    res=F.run_field(1.0, base_rank=br); q=quick(res)
    print("{:>9} | {:4.1f}/{:4.1f}  | {:4.1f}/{:4.1f}".format(
        br, q["gold5_luck"],q["gold5_tech"], q["cop5000_luck"],q["cop5000_tech"]))

print("\n"+"="*70)
print("C) 多种子稳定性 (σ=1.0) — 需求3探针均值/SD 与 需求1/4运技%")
print("-"*70)
g5l=[];g5t=[];c5l=[];pm=[];psd=[]
for sd in range(8):
    res=F.run_field(1.0, seed=20260621+sd*7919); q=quick(res)
    g5l.append(q["gold5_luck"]);g5t.append(q["gold5_tech"]);c5l.append(q["cop5000_luck"])
    pm.append(q["probe_mean"]);psd.append(q["probe_sd"])
    print(" seed{}: 金头5运={:.1f} 技={:.1f} | 铜5000运={:.1f} | 探针均={:.0f} SD={:.0f}".format(
        sd,q["gold5_luck"],q["gold5_tech"],q["cop5000_luck"],q["probe_mean"],q["probe_sd"]))
print(" 跨种子: 金头5运气% {:.1f}±{:.1f} | 金头5技术% {:.1f}±{:.1f} | 铜5000运气% {:.1f}±{:.1f}".format(
    statistics.fmean(g5l),statistics.pstdev(g5l),statistics.fmean(g5t),statistics.pstdev(g5t),
    statistics.fmean(c5l),statistics.pstdev(c5l)))
print(" 跨种子: 探针200场均值 {:.0f}±{:.0f} | 探针SD {:.0f}±{:.0f}".format(
    statistics.fmean(pm),statistics.pstdev(pm),statistics.fmean(psd),statistics.pstdev(psd)))
