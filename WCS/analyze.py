# -*- coding: utf-8 -*-
"""
四需求计算 + 审计  (import field_sim 的全场模拟结果)
需求1: 铜头(前1万)运气/技术%
需求2: 中位数铜头的场次(到16000)
需求3: 中位技能玩家打200场的 DP 期望值 + 范围(95%CI / ±1SD / 极值)
需求4: 金头(前10)运气/技术%
"""
import math, statistics, json, sys, time
import field_sim as F

def pct(x): return 100.0*x

def decomp(snap):
    """snap=(games,exp_self,exp_base,act_win,fw,fg,sw,sg,lc,kc)"""
    g,es,eb,aw,fw,fg,sw,sg,lc,kc = snap
    luck = lc/es if es>0 else float('nan')          # 运气局胜 / 总期望胜 (主口径, 有界)
    tech = kc/es if es>0 else float('nan')
    luckA= min(eb/es,1.0) if es>0 else float('nan') # 反事实比值口径(基准也赢/你赢)
    return dict(games=g, exp_self=es, exp_base=eb, act_win=aw,
                first_wr=(fw/fg if fg else 0), second_wr=(sw/sg if sg else 0),
                first_g=fg, second_g=sg, total_wr=aw/g if g else 0,
                luck=luck, tech=tech, luckA=luckA)

def rank_index(res):
    return sorted(((res["dp"][i],i) for i in range(res["n_field"])),
                  key=lambda t:t[0], reverse=True)

def summarize(vals):
    vals=sorted(vals); n=len(vals)
    mean=statistics.fmean(vals); sd=statistics.pstdev(vals)
    def q(p):
        k=p*(n-1); lo=int(math.floor(k)); hi=int(math.ceil(k))
        return vals[lo] if lo==hi else vals[lo]+(vals[hi]-vals[lo])*(k-lo)
    return dict(n=n, mean=mean, sd=sd, min=vals[0], max=vals[-1],
                p2_5=q(0.025), p50=q(0.5), p97_5=q(0.975),
                ci95_lo=mean-1.96*sd, ci95_hi=mean+1.96*sd,
                sd_lo=mean-sd, sd_hi=mean+sd)

def analyze(res, label=""):
    ri = rank_index(res)
    out = {}
    # ---- 代表玩家 ----
    def agent_at(rank): return ri[rank-1][1]
    reps = {"金头-第5名": agent_at(5), "金头门槛-第10名": agent_at(10),
            "中位铜头-第5000名": agent_at(5000), "铜头门槛-第10000名": agent_at(10000)}
    # 需求1 & 4: 运气/技术
    dec = {}
    for name,i in reps.items():
        snap = res["snap61"][i] if "金头" in name else res["snap16"][i]
        if snap is None:
            dec[name]=None; continue
        dec[name]=decomp(snap)
    out["decomp"]=dec
    # 需求2: 铜头(前1万)到16000的场次分布 + 中位
    games16=[res["snap16"][i][0] for _,i in ri[:10000] if res["snap16"][i] is not None]
    reached = len(games16)
    out["copper_games"]=summarize(games16)
    out["copper_reached"]=reached
    out["copper_median_rank5000_games"]= (res["snap16"][agent_at(5000)][0]
                                          if res["snap16"][agent_at(5000)] else None)
    # 需求3: 中位技能(探针 z=0) 打满200场 DP 分布
    probe_dp200=[res["dp_at_200"][i] for i in range(res["n_field"],res["n_field"]+res["n_probe"])
                 if res["dp_at_200"][i] is not None]
    out["probe_dp200"]=summarize(probe_dp200)
    out["probe_n"]=len(probe_dp200)
    # 探针先后手/胜率(平均)
    pf=range(res["n_field"],res["n_field"]+res["n_probe"])
    twr=statistics.fmean([res["act_win"][i]/res["games"][i] for i in pf])
    out["probe_total_wr"]=twr
    return out

def show(out, sigma):
    print("\n############### σ={:.3f} 四需求结果 ###############".format(sigma))
    print("\n[需求1+4] 运气/技术 分解 (主口径=运气局胜/总胜; 括号内=反事实比值口径):")
    print("  {:<18}{:>6}{:>8}{:>8}{:>8}{:>9}{:>9}{:>10}".format(
        "玩家","场次","总胜率","先手","后手","运气%","技术%","(反事实运气%)"))
    for name,d in out["decomp"].items():
        if d is None: print("  {:<18} (未达标)".format(name)); continue
        print("  {:<18}{:>6}{:>7.1%}{:>8.1%}{:>8.1%}{:>8.1f}%{:>8.1f}%{:>10.1f}%".format(
            name, d["games"], d["total_wr"], d["first_wr"], d["second_wr"],
            pct(d["luck"]), pct(d["tech"]), pct(d["luckA"])))
    cg=out["copper_games"]
    print("\n[需求2] 铜头(前1万)到16000的场次:  达标人数={}/10000".format(out["copper_reached"]))
    print("  全体: 中位={:.0f}  均值={:.0f}  SD={:.0f}  [min{:.0f} ~ max{:.0f}]  95%区间[{:.0f},{:.0f}]".format(
        cg["p50"],cg["mean"],cg["sd"],cg["min"],cg["max"],cg["p2_5"],cg["p97_5"]))
    print("  中位铜头(第5000名)场次 = {}".format(out["copper_median_rank5000_games"]))
    pd=out["probe_dp200"]
    print("\n[需求3] 中位技能玩家打满200场的最终DP (探针n={}):".format(out["probe_n"]))
    print("  期望(均值)={:.0f}   中位={:.0f}   SD={:.0f}   平均总胜率={:.1%}".format(
        pd["mean"],pd["p50"],pd["sd"],out["probe_total_wr"]))
    print("  ±1SD区间      = [{:.0f}, {:.0f}]".format(pd["sd_lo"],pd["sd_hi"]))
    print("  95%CI(正态)   = [{:.0f}, {:.0f}]".format(pd["ci95_lo"],pd["ci95_hi"]))
    print("  95%经验区间   = [{:.0f}, {:.0f}]".format(pd["p2_5"],pd["p97_5"]))
    print("  极值[min,max] = [{:.0f}, {:.0f}]".format(pd["min"],pd["max"]))

if __name__=="__main__":
    sigma=float(sys.argv[1]) if len(sys.argv)>1 else 1.0
    t0=time.time()
    res=F.run_field(sigma)
    F.diagnose(res)
    out=analyze(res, "σ=%.2f"%sigma)
    show(out, sigma)
    print("\n用时 {:.1f}s".format(time.time()-t0))
    json.dump({"sigma":sigma,"out":{k:v for k,v in out.items() if k!='decomp'},
               "decomp":out["decomp"]}, open("E:/TimeAudit/WCS/result_%.0f.json"%(sigma*100),"w",encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
