# -*- coding: utf-8 -*-
"""
游戏王 Master Duel DC杯二阶段(WCS) — 全场 ELO Agent-Based 模拟  v3
====================================================================
忠实赛制(用户逐条校准后):
 * 2万人同时 0 分起步, 72h 实时一起爬, ELO 按当前 DP 软匹配。
 * 自洽两人零和硬币模型:  P(自己先手赢)=sigmoid(θ+Δ),  P(自己后手赢)=sigmoid(-θ+Δ),
   Δ=r_自己 - r_对手 (logit技能差)。 后手赢 = 1 - 对手先手赢, 完全零和自洽。
 * θ 由用户硬数据解出: 10TH金头在"自己38000分"时 先手90%/后手40%
       => θ+Δ*=ln9=2.197, -θ+Δ*=ln(2/3)=-0.405  => θ=1.3014, Δ*=0.8959
   => 等技能时 先手78.6%/后手21.4% (blended50%); 纯T1+BO1 先手优势巨大(=运气主轴)。
 * 得失分: <9000 输-100, 9000~10000 输-500 (掉分保护=分从1万以下"凭空注入");
           >=10000 一律 赢+1000/输-1000 对称 => "(胜-负)*1000 = DP" 对铜头以上严格成立。
           (金头前十 -1040 仅在金头专项叠加, 已并入σ校准)
 * 涌现: 强者早期碾压弱场(高胜率快速爬) -> 进中高分段实时对手追上 -> 收敛~55开;
        唯前十能持续碾压同档高手(10TH在38000档仍90/40=65%)。"金头胜率符合时间"由此自然涌现。
纯标准库, 可被任何第三方审计。
"""
import math, random, json, statistics, sys, time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

N_FIELD   = 20000
N_PROBE   = 800
T_STEPS   = 330
H_TOTAL   = 72.0
SNAP_STEP = round(T_STEPS * 38.0 / H_TOTAL)

# 由用户数据解出的硬币偏置(先手优势, logit单位)
THETA   = (math.log(9.0) - math.log(2.0/3.0)) / 2.0    # =1.30143
DELTA_STAR = (math.log(9.0) + math.log(2.0/3.0)) / 2.0 # =0.89588 (10TH与38000档的技能差)

WIN_DP  = 1000
def loss_dp(dp):
    if dp < 9000:  return 100
    if dp < 10000: return 500
    return 1000

GOLD_DP, COPPER_DP = 61000, 15500
COPPER_STOP = 16000
GOLD_BAND = (34000, 44000)   # "38000分档"验证窗口
SNAP_TARGET = {10:49929, 100:37423, 1000:22032, 2000:17894, 5000:13578, 10000:10029}
SEED = 20260621

def sigmoid(x):
    if x >= 0:
        z = math.exp(-x); return 1.0/(1.0+z)
    z = math.exp(x); return z/(1.0+z)

def run_field(sigma, t_steps=T_STEPS, n_field=N_FIELD, n_probe=N_PROBE, seed=SEED, jit=600.0,
              base_rank=8000):
    rng = random.Random(seed)
    N = n_field + n_probe
    skill = [rng.gauss(0.0, sigma) for _ in range(n_field)] + [0.0]*n_probe
    is_probe = [False]*n_field + [True]*n_probe
    dp = [0.0]*N
    real_sorted = sorted(skill[:n_field])
    r_base = real_sorted[n_field - base_rank]     # 反事实"基准场玩家"= 第 base_rank 名(默认前40%门槛)

    games    = [0]*N
    exp_self = [0.0]*N
    exp_base = [0.0]*N
    lc = [0.0]*N      # 运气局期望胜(基准玩家也会赢的局, prob_base>=0.5)
    kc = [0.0]*N      # 技术局期望胜(60%的人会输的局, prob_base<0.5)
    act_win  = [0]*N
    fg=[0]*N; fw=[0]*N; sg=[0]*N; sw=[0]*N        # 先/后手 场次&胜场(全程)
    hfg=[0]*N; hfw=[0]*N; hsg=[0]*N; hsw=[0]*N     # 38000档窗口内的先/后手 场次&胜场
    dp_at_200= [None]*N
    dp_snap  = None
    # "首次到达 16000 / 61000 时"的累计战绩快照(绕开满勤偏差): (场次,期望自胜,期望基准胜,实胜,先胜,先场,后胜,后场)
    snap16 = [None]*N
    snap61 = [None]*N

    th = THETA; rbase = r_base
    idx = list(range(N)); uni = rng.uniform; rnd = rng.random
    lo, hi = GOLD_BAND
    for step in range(t_steps):
        jitter = [dp[i] + uni(-jit, jit) for i in idx]
        order = sorted(idx, key=jitter.__getitem__)
        for k in range(0, N - 1, 2):
            a = order[k]; b = order[k+1]
            sa = skill[a]; sb = skill[b]; gap = sa - sb
            da0 = dp[a]; db0 = dp[b]
            a_first = rnd() < 0.5
            if a_first:
                pa      = sigmoid(th + gap)
                pa_base = sigmoid(th + (rbase - sb))
                pb_base = sigmoid(-th + (rbase - sa))
            else:
                pa      = sigmoid(-th + gap)
                pa_base = sigmoid(-th + (rbase - sb))
                pb_base = sigmoid(th + (rbase - sa))
            pb = 1.0 - pa
            exp_self[a]+=pa; exp_self[b]+=pb
            exp_base[a]+=pa_base; exp_base[b]+=pb_base
            if pa_base>=0.5: lc[a]+=pa
            else:            kc[a]+=pa
            if pb_base>=0.5: lc[b]+=pb
            else:            kc[b]+=pb
            ga=games[a]+1; gb=games[b]+1; games[a]=ga; games[b]=gb
            a_wins = rnd() < pa
            if a_wins:
                act_win[a]+=1; dp[a]=da0+WIN_DP
                d=db0-loss_dp(db0); dp[b]=d if d>0 else 0.0
            else:
                act_win[b]+=1; dp[b]=db0+WIN_DP
                d=da0-loss_dp(da0); dp[a]=d if d>0 else 0.0
            # 先后手记账
            if a_first:
                fg[a]+=1; sg[b]+=1
                if a_wins: fw[a]+=1
                else:      sw[b]+=1
                if lo<=da0<hi:
                    hfg[a]+=1;
                    if a_wins: hfw[a]+=1
                if lo<=db0<hi:
                    hsg[b]+=1
                    if not a_wins: hsw[b]+=1
            else:
                sg[a]+=1; fg[b]+=1
                if a_wins: sw[a]+=1
                else:      fw[b]+=1
                if lo<=da0<hi:
                    hsg[a]+=1
                    if a_wins: hsw[a]+=1
                if lo<=db0<hi:
                    hfg[b]+=1
                    if not a_wins: hfw[b]+=1
            if snap16[a] is None and dp[a]>=COPPER_STOP:
                snap16[a]=(ga,exp_self[a],exp_base[a],act_win[a],fw[a],fg[a],sw[a],sg[a],lc[a],kc[a])
            if snap16[b] is None and dp[b]>=COPPER_STOP:
                snap16[b]=(gb,exp_self[b],exp_base[b],act_win[b],fw[b],fg[b],sw[b],sg[b],lc[b],kc[b])
            if snap61[a] is None and dp[a]>=GOLD_DP:
                snap61[a]=(ga,exp_self[a],exp_base[a],act_win[a],fw[a],fg[a],sw[a],sg[a],lc[a],kc[a])
            if snap61[b] is None and dp[b]>=GOLD_DP:
                snap61[b]=(gb,exp_self[b],exp_base[b],act_win[b],fw[b],fg[b],sw[b],sg[b],lc[b],kc[b])
            if ga==200: dp_at_200[a]=dp[a]
            if gb==200: dp_at_200[b]=dp[b]
        if step+1==SNAP_STEP: dp_snap=dp[:]
    return dict(skill=skill,is_probe=is_probe,dp=dp,dp_snap=dp_snap,games=games,
                exp_self=exp_self,exp_base=exp_base,act_win=act_win,
                fg=fg,fw=fw,sg=sg,sw=sw,hfg=hfg,hfw=hfw,hsg=hsg,hsw=hsw,
                snap16=snap16,snap61=snap61,dp_at_200=dp_at_200,r_base=r_base,sigma=sigma,
                n_field=n_field,n_probe=n_probe,snap_step=SNAP_STEP)

def ladder_at(dp_list,n_field,ranks):
    real=sorted(dp_list[:n_field],reverse=True); return {r:real[r-1] for r in ranks}
def real_rank_index(res):
    return sorted(((res["dp"][i],i) for i in range(res["n_field"])),key=lambda t:t[0],reverse=True)

def diagnose(res):
    sig=res["sigma"]
    fin=ladder_at(res["dp"],res["n_field"],[10,100,1000,2000,5000,10000])
    snap=ladder_at(res["dp_snap"],res["n_field"],[10,100,1000,2000,5000,10000]) if res["dp_snap"] else {}
    ri=real_rank_index(res)
    print(f"\n========= σ={sig:.3f} θ={THETA:.3f} steps={T_STEPS} =========")
    print(" 终局: 10TH={:.0f}(目标{})  10000TH={:.0f}(目标{})".format(fin[10],GOLD_DP,fin[10000],COPPER_DP))
    print(" 第38h快照 vs 目标:")
    for r in [10,100,1000,2000,5000,10000]:
        print("   {:>6}TH: 模拟{:8.0f}  目标{:8.0f}  误差{:+.0f}".format(r,snap.get(r,0),SNAP_TARGET[r],snap.get(r,0)-SNAP_TARGET[r]))
    print(" 各名次 胜率/先后手/净胜×1000 vs DP:")
    for r in [10,100,1000,5000,10000]:
        d,i=ri[r-1]; g=res["games"][i]; w=res["act_win"][i]; net=2*w-g; wr=w/g if g else 0
        fwr=res["fw"][i]/res["fg"][i] if res["fg"][i] else 0
        swr=res["sw"][i]/res["sg"][i] if res["sg"][i] else 0
        print("   rank{:>5}: DP={:7.0f} 胜率={:.3f} 先手={:.2f} 后手={:.2f} 场次={} 净胜×1000={:7d} (DP-净={:+.0f})".format(
            r,d,wr,fwr,swr,g,net*1000,d-net*1000))
    # 10TH 在38000档的先后手验证
    d,i=ri[0]
    hf=res["hfw"][i]/res["hfg"][i] if res["hfg"][i] else 0
    hs=res["hsw"][i]/res["hsg"][i] if res["hsg"][i] else 0
    print("   ★10TH在{}档: 先手={:.2f}(目标0.90) 后手={:.2f}(目标0.40) [场次先{}后{}]".format(
        GOLD_BAND,hf,hs,res["hfg"][i],res["hsg"][i]))
    gg=[res["games"][i] for _,i in ri[:10]]
    print(" 金头(前10)场次: min={} max={} 均值={:.0f}".format(min(gg),max(gg),sum(gg)/len(gg)))
    return fin,snap

if __name__=="__main__":
    if len(sys.argv)>1 and sys.argv[1]=="scan":
        for sg in [0.8,1.0,1.2,1.4]:
            t0=time.time(); res=run_field(sg); diagnose(res); print("  用时{:.1f}s".format(time.time()-t0))
    else:
        sigma=float(sys.argv[1]) if len(sys.argv)>1 else 1.0
        t0=time.time(); res=run_field(sigma); diagnose(res); print(" 用时{:.1f}s".format(time.time()-t0))
