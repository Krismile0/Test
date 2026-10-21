"""
order_fulfillment.csv 分析脚本
一条命令复现 README 中所有数字：
    python src/analyze.py
输出：outputs/metrics.json, outputs/*.csv, outputs/*.png
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "order_fulfillment.csv"
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

COST_STOCKOUT = 90  # 每次砍单损失
COST_FLAG = 15      # 每次提前标记缺货的成本

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def load() -> pd.DataFrame:
    df = pd.read_csv(DATA)
    # order_time 带 +08:00，stock_synced_at 是 UTC(Z)，统一转 UTC 再相减
    t_order = pd.to_datetime(df["order_time"], utc=True)
    t_sync = pd.to_datetime(df["stock_synced_at"], utc=True)
    df["lag_min"] = (t_order - t_sync).dt.total_seconds() / 60
    df["order_date"] = t_order.dt.date
    return df


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rule_cost(labels: pd.Series, flags: pd.Series) -> dict:
    """labels: 1=履约,0=砍单；flags: True=提前标记缺货"""
    y = labels == 0
    tp = int((flags & y).sum())
    fp = int((flags & ~y).sum())
    fn = int((~flags & y).sum())
    tn = int((~flags & ~y).sum())
    cost = COST_FLAG * (tp + fp) + COST_STOCKOUT * fn
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, flagged=tp + fp, cost=cost)


def normalize_outputs() -> None:
    """Generated CSVs use LF only, so reruns produce zero diff across platforms."""
    for f in OUT.glob("*.csv"):
        f.write_bytes(f.read_bytes().replace(bytes([13, 10]), bytes([10])))


def main() -> None:
    df = load()
    m: dict = {}
    m["rows_total"] = len(df)
    m["rows_labeled"] = int(df["order_status"].notna().sum())
    m["rows_missing_status"] = int(df["order_status"].isna().sum())
    m["duplicate_rows"] = int(df.duplicated().sum())
    m["unique_order_ids"] = int(df["order_id"].nunique())
    m["stockouts"] = int((df["order_status"] == 0).sum())
    m["fulfilled"] = int((df["order_status"] == 1).sum())
    m["order_period"] = [str(df["order_date"].min()), str(df["order_date"].max())]
    m["lag_min"] = df["lag_min"].describe().round(2).to_dict()

    # ---- 1. 延迟的结构：正常档 vs 迟滞档（9~11 分钟无样本，自然断点）----
    counts = df["lag_min"].value_counts().sort_index()
    m["lag_value_counts"] = {str(int(k)): int(v) for k, v in counts.items()}
    stale_thr = 8
    m["stale_threshold"] = stale_thr
    m["gap_9_11_rows"] = int(df["lag_min"].between(9, 11).sum())

    d = df.dropna(subset=["order_status"]).copy()
    fresh = d[d["lag_min"] <= stale_thr]
    stale = d[d["lag_min"] > stale_thr]
    m["fresh"] = dict(n=len(fresh), stockouts=int((fresh.order_status == 0).sum()),
                      rate=round(1 - fresh.order_status.mean(), 4),
                      ci=[round(x, 4) for x in wilson_ci(int((fresh.order_status == 0).sum()), len(fresh))])
    m["stale"] = dict(n=len(stale), stockouts=int((stale.order_status == 0).sum()),
                      rate=round(1 - stale.order_status.mean(), 4),
                      ci=[round(x, 4) for x in wilson_ci(int((stale.order_status == 0).sum()), len(stale))])
    m["stale_share_of_orders"] = round(len(stale) / len(d), 4)
    m["stale_share_of_stockouts"] = round(int(((d.lag_min > stale_thr) & (d.order_status == 0)).sum())
                                          / int((d.order_status == 0).sum()), 4)

    # ---- 2. 渠道分层：lag 效应是否只存在于部分渠道 ----
    seg = []
    for ch in sorted(d["platform_channel"].unique()):
        g = d[d.platform_channel == ch]
        for lab, sub in [("fresh(<=8)", g[g.lag_min <= stale_thr]), ("stale(>8)", g[g.lag_min > stale_thr])]:
            k = int((sub.order_status == 0).sum())
            seg.append(dict(channel=ch, segment=lab, n=len(sub), stockouts=k,
                            stockout_rate=round(1 - sub.order_status.mean(), 4) if len(sub) else None,
                            ci_low=round(wilson_ci(k, len(sub))[0], 4) if len(sub) else None,
                            ci_high=round(wilson_ci(k, len(sub))[1], 4) if len(sub) else None))
    seg_df = pd.DataFrame(seg)
    seg_df.to_csv(OUT / "channel_segments.csv", index=False, encoding="utf-8-sig")
    m["channel_segments"] = seg

    # 渠道 × 延迟桶
    d["lag_bucket"] = pd.cut(d["lag_min"], [-0.1, 8, 15, 30, 60],
                             labels=["0-8min", "9-15min", "16-30min", "31-60min"])
    ct = pd.crosstab([d["platform_channel"], d["lag_bucket"]], d["order_status"], dropna=True)
    ct.to_csv(OUT / "channel_lag_crosstab.csv", encoding="utf-8-sig")
    rate_tab = pd.crosstab([d["platform_channel"], d["lag_bucket"]], d["order_status"],
                           normalize="index", dropna=True)
    rate_tab.to_csv(OUT / "channel_lag_rate.csv", encoding="utf-8-sig")

    # amazon 是否真的免疫：卡方/比例差
    amz_stale = d[(d.platform_channel == "amazon_us") & (d.lag_min > stale_thr)]
    oth_stale = d[(d.platform_channel != "amazon_us") & (d.lag_min > stale_thr)]
    m["amazon_stale"] = dict(n=len(amz_stale), stockouts=int((amz_stale.order_status == 0).sum()),
                             rate=round(1 - amz_stale.order_status.mean(), 4))
    m["other_stale"] = dict(n=len(oth_stale), stockouts=int((oth_stale.order_status == 0).sum()),
                            rate=round(1 - oth_stale.order_status.mean(), 4))

    # 浏览时长：控制 lag 后是否还相关
    browse = []
    for lab, sub in [("fresh(<=8)", fresh), ("stale(>8)", stale)]:
        for b_lab, b_sub in sub.groupby(pd.cut(sub.buyer_browsing_sec, [0, 40, 60, 80, 100]), observed=True):
            browse.append(dict(segment=lab, browse_bucket=str(b_lab), n=len(b_sub),
                               stockout_rate=round(1 - b_sub.order_status.mean(), 4)))
    pd.DataFrame(browse).to_csv(OUT / "browse_segments.csv", index=False, encoding="utf-8-sig")
    m["browse_within_segment"] = browse
    m["corr"] = d[["lag_min", "stock_display", "buyer_browsing_sec", "order_status"]].corr().round(3).to_dict()

    # ---- 3. 决策规则与成本 ----
    base = int((d.order_status == 0).sum()) * COST_STOCKOUT
    m["baseline_cost"] = base
    rules = {
        "no_action": pd.Series(False, index=d.index),
        "rule_lag>8": d["lag_min"] > stale_thr,
        "rule_lag>8_and_channel!=amazon": (d["lag_min"] > stale_thr) & (d["platform_channel"] != "amazon_us"),
        "rule_C_plus_amazon_lowstock": (((d["lag_min"] > stale_thr) & (d["platform_channel"] != "amazon_us"))
                                        | ((d["platform_channel"] == "amazon_us") & (d["lag_min"] <= stale_thr)
                                           & (d["stock_display"] <= 3))),
    }
    # 逐阈值扫描（单变量 lag 规则）
    scan = []
    for thr in range(0, 61):
        r = rule_cost(d["order_status"], d["lag_min"] > thr)
        scan.append(dict(threshold_min=thr, **r))
    scan_df = pd.DataFrame(scan)
    scan_df.to_csv(OUT / "threshold_scan.csv", index=False, encoding="utf-8-sig")
    best = scan_df.loc[scan_df["cost"].idxmin()]
    m["best_lag_threshold"] = dict(threshold_min=int(best.threshold_min), cost=int(best.cost),
                                   saving=int(base - best.cost),
                                   saving_pct=round(100 * (base - best.cost) / base, 2))

    res = []
    for name, flags in rules.items():
        r = rule_cost(d["order_status"], flags)
        r.update(rule=name, saving=base - r["cost"], saving_pct=round(100 * (base - r["cost"]) / base, 2))
        res.append(r)
    rules_df = pd.DataFrame(res)
    rules_df.to_csv(OUT / "rule_costs.csv", index=False, encoding="utf-8-sig")
    m["rules"] = res

    # Amazon 新鲜档低库存：渠道专属的第二信号
    amz_low = d[(d.platform_channel == "amazon_us") & (d.lag_min <= stale_thr) & (d.stock_display <= 3)]
    amz_low_hi = d[(d.platform_channel == "amazon_us") & (d.lag_min <= stale_thr) & (d.stock_display > 3)]
    m["amazon_fresh_lowstock"] = dict(n=len(amz_low), stockouts=int((amz_low.order_status == 0).sum()),
                                      rate=round(1 - amz_low.order_status.mean(), 4),
                                      ci_low=round(wilson_ci(int((amz_low.order_status == 0).sum()), len(amz_low))[0], 4),
                                      ci_high=round(wilson_ci(int((amz_low.order_status == 0).sum()), len(amz_low))[1], 4))
    m["amazon_fresh_highstock"] = dict(n=len(amz_low_hi), stockouts=int((amz_low_hi.order_status == 0).sum()),
                                       rate=round(1 - amz_low_hi.order_status.mean(), 4))
    m["amazon_lowstock_by_day"] = (amz_low.assign(day=pd.to_datetime(amz_low.order_time, utc=True)
                                                   .dt.tz_convert("Asia/Shanghai").dt.date)
                                   .groupby("day").agg(n=("order_status", "size"),
                                                       stockouts=("order_status", lambda x: int((x == 0).sum())))
                                   .reset_index().astype(str).to_dict("records"))

    # 重复行敏感性：去重后重算核心指标
    nd = df.drop_duplicates().dropna(subset=["order_status"])
    st_nd = nd.lag_min > stale_thr
    flags_nd = ((st_nd & (nd.platform_channel != "amazon_us"))
                | ((nd.platform_channel == "amazon_us") & (~st_nd) & (nd.stock_display <= 3)))
    base_nd = int((nd.order_status == 0).sum()) * COST_STOCKOUT
    cost_nd = rule_cost(nd["order_status"], flags_nd)
    m["dedupe_sensitivity"] = dict(
        rows_after_dedupe=len(nd), duplicate_rows=int(df.duplicated().sum()),
        fresh_rate=round(1 - nd[~st_nd].order_status.mean(), 4),
        stale_rate=round(1 - nd[st_nd].order_status.mean(), 4),
        rule_c_cost=cost_nd["cost"], rule_c_saving=int(base_nd - cost_nd["cost"]),
        rule_c_saving_pct=round(100 * (base_nd - cost_nd["cost"]) / base_nd, 2))

    # 若彻底修好同步（反事实：迟滞订单按新鲜档 3.5% 砍单率计）
    fresh_rate = 1 - fresh.order_status.mean()
    counterfactual_stockouts = int((fresh.order_status == 0).sum()) + len(stale) * fresh_rate
    m["counterfactual_fix_sync"] = dict(
        assumed_fresh_rate=round(fresh_rate, 4),
        expected_stockouts=round(counterfactual_stockouts, 1),
        stockout_reduction_pct=round(100 * (1 - counterfactual_stockouts / int((d.order_status == 0).sum())), 2),
        expected_cost=round(counterfactual_stockouts * COST_STOCKOUT, 0),
        expected_saving=round(base - counterfactual_stockouts * COST_STOCKOUT, 0),
        saving_pct=round(100 * (1 - counterfactual_stockouts * COST_STOCKOUT / base), 2),
    )

    # ---- 4. 时间稳定性 ----
    daily = d.groupby("order_date").agg(
        orders=("order_id", "size"),
        stale_orders=("lag_min", lambda x: int((x > stale_thr).sum())),
        stockouts=("order_status", lambda x: int((x == 0).sum())),
        stockout_rate=("order_status", lambda x: round(1 - x.mean(), 4)),
    ).reset_index()
    daily.to_csv(OUT / "daily.csv", index=False, encoding="utf-8-sig")
    m["daily"] = daily.astype(str).to_dict("records")

    # ---- 5. 图 ----
    # 5.1 砍单率 vs 延迟
    fig, ax = plt.subplots(figsize=(8, 4.5))
    xs, ys, ns = [], [], []
    for thr in range(0, 9):
        sub = d[d.lag_min == thr]
        xs.append(thr); ys.append(1 - sub.order_status.mean()); ns.append(len(sub))
    ax.bar(xs, ys, color="#4C78A8")
    for x, y, n in zip(xs, ys, ns):
        ax.text(x, y + 0.008, f"{y:.0%}\nn={n}", ha="center", fontsize=7)
    ax.axhline(1 - d.order_status.mean(), color="gray", ls="--", lw=1, label=f"整体 {1 - d.order_status.mean():.1%}")
    ax.set_xlabel("同步延迟（分钟）"); ax.set_ylabel("砍单率")
    ax.set_title("新鲜档（0-8 分钟）：砍单率稳定在低位")
    ax.legend(); fig.tight_layout(); fig.savefig(OUT / "fig_rate_fresh.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = ["新鲜档\n(<=8min, n=%d)" % len(fresh), "迟滞档\n(>8min, n=%d)" % len(stale)]
    vals = [1 - fresh.order_status.mean(), 1 - stale.order_status.mean()]
    bars = ax.bar(labels, vals, color=["#54A24B", "#E45756"])
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.1%}", ha="center", fontweight="bold")
    ax.set_ylabel("砍单率"); ax.set_title("同步延迟 >8 分钟：砍单率跳升约 12 倍")
    fig.tight_layout(); fig.savefig(OUT / "fig_rate_regime.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for ch in sorted(d["platform_channel"].unique()):
        g = d[d.platform_channel == ch]
        xs = [0, 1]
        ys = [1 - g[g.lag_min <= stale_thr].order_status.mean(),
              1 - g[g.lag_min > stale_thr].order_status.mean()]
        ax.plot(xs, ys, marker="o", label=ch)
    ax.set_xticks([0, 1]); ax.set_xticklabels(["lag<=8", "lag>8"])
    ax.set_ylabel("砍单率"); ax.set_title("延迟效应按渠道分层：Amazon 几乎不受影响")
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(OUT / "fig_channel.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(scan_df.threshold_min, scan_df.cost, marker="o", ms=3, color="#4C78A8", label="lag 阈值规则")
    ax.axhline(base, color="gray", ls="--", lw=1, label=f"不干预 ¥{base:,}")
    best_r = rules_df[rules_df.rule == "rule_lag>8_and_channel!=amazon"].iloc[0]
    ax.axhline(best_r.cost, color="#54A24B", ls="-.", lw=1.2,
               label=f"lag>8 且非 Amazon ¥{int(best_r.cost):,}")
    ax.set_xlabel("阈值：lag > X 分钟即标记缺货"); ax.set_ylabel("总成本（元）")
    ax.set_title("阈值扫描：总成本在 8 分钟后不再下降")
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(OUT / "fig_cost_curve.png", dpi=150); plt.close(fig)

    normalize_outputs()
    (OUT / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: m[k] for k in ["rows_total", "rows_labeled", "stockouts", "fresh", "stale",
                                        "stale_share_of_stockouts", "baseline_cost", "rules",
                                        "counterfactual_fix_sync"]}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
