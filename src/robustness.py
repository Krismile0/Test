"""
稳健性与敏感性分析（补充 analyze.py）
    python src/robustness.py
输出：outputs/robustness.json, outputs/sensitivity.csv, outputs/fig_sensitivity.png
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

df = pd.read_csv(ROOT / "data" / "order_fulfillment.csv")
df["lag_min"] = (pd.to_datetime(df["order_time"], utc=True)
                 - pd.to_datetime(df["stock_synced_at"], utc=True)).dt.total_seconds() / 60
d = df.dropna(subset=["order_status"]).copy()
d["is_stockout"] = (d["order_status"] == 0).astype(int)

rng = np.random.default_rng(42)
res: dict = {}


def rule_cost(dd: pd.DataFrame, flags: pd.Series, c_flag: float, c_stock: float) -> float:
    y = dd["is_stockout"].astype(bool)
    f = flags.astype(bool)
    return c_flag * int(f.sum()) + c_stock * int((~f & y).sum())


# ---------- 1. Bootstrap：迟滞档砍单率、Amazon 免疫、规则节省 ----------
def bootstrap_ci(mask: pd.Series, n_boot: int = 5000, alpha: float = 0.05):
    x = d.loc[mask, "is_stockout"].to_numpy()
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    rates = x[idx].mean(axis=1)
    return [round(float(np.quantile(rates, alpha / 2)), 4),
            round(float(np.quantile(rates, 1 - alpha / 2)), 4)]


stale = d["lag_min"] > 8
amz_stale = stale & (d["platform_channel"] == "amazon_us")
oth_stale = stale & (d["platform_channel"] != "amazon_us")
fresh = ~stale
low_stock = d["stock_display"] <= 3

res["bootstrap"] = {
    "stale_rate": [round(float(d.loc[stale, "is_stockout"].mean()), 4), bootstrap_ci(stale)],
    "fresh_rate": [round(float(d.loc[fresh, "is_stockout"].mean()), 4), bootstrap_ci(fresh)],
    "amazon_stale_rate": [round(float(d.loc[amz_stale, "is_stockout"].mean()), 4), bootstrap_ci(amz_stale)],
    "other_stale_rate": [round(float(d.loc[oth_stale, "is_stockout"].mean()), 4), bootstrap_ci(oth_stale)],
    "fresh_lowstock_rate": [round(float(d.loc[fresh & low_stock, "is_stockout"].mean()), 4),
                            bootstrap_ci(fresh & low_stock)],
    "fresh_normal_rate": [round(float(d.loc[fresh & ~low_stock, "is_stockout"].mean()), 4),
                          bootstrap_ci(fresh & ~low_stock)],
}

# 规则 B / C 节省的 Bootstrap（对订单重抽样）
base_rule = stale & (d["platform_channel"] != "amazon_us")
rule_c = base_rule | ((d["platform_channel"] == "amazon_us") & (~stale) & (d["stock_display"] <= 3))
n = len(d)
idx_all = rng.integers(0, n, size=(2000, n))
for label, rule in [("rule_b", base_rule), ("rule_c", rule_c)]:
    savings = []
    for row in idx_all:
        y = d["is_stockout"].to_numpy()[row].astype(bool)
        f = rule.to_numpy()[row].astype(bool)
        base_cost = y.sum() * 90
        savings.append(base_cost - (15 * f.sum() + 90 * ((~f) & y).sum()))
    savings = np.array(savings)
    res[f"{label}_saving"] = {
        "point": int(d["is_stockout"].sum() * 90 - rule_cost(d, rule, 15, 90)),
        "ci95": [int(np.quantile(savings, 0.025)), int(np.quantile(savings, 0.975))],
        "p_saving_positive": round(float((savings > 0).mean()), 4),
    }

# ---------- 2. 成本敏感性：标记成本 c_flag 变化时的最优规则 ----------
rules = {
    "lag>8": stale,
    "规则C: B + Amazon新鲜档低库存<=3": rule_c,
    "lag>8 且非 Amazon": base_rule,
    "lag>8 且非 Amazon，或新鲜档低库存(<=3)": base_rule | (fresh & low_stock),
    "仅在迟滞档且低库存": stale & low_stock,
}
rows = []
for c_flag in [5, 10, 15, 20, 30, 45, 60]:
    for c_stock in [90]:
        base_cost = int(d["is_stockout"].sum()) * c_stock
        for name, flags in rules.items():
            cost = rule_cost(d, flags, c_flag, c_stock)
            rows.append(dict(c_flag=c_flag, c_stock=c_stock, rule=name, cost=cost,
                             saving=base_cost - cost,
                             saving_pct=round(100 * (base_cost - cost) / base_cost, 2),
                             flagged=int(flags.sum())))
sens = pd.DataFrame(rows)
sens.to_csv(OUT / "sensitivity.csv", index=False, encoding="utf-8-sig")

# 每个 c_flag 下的最优规则
best_by_cost = sens.loc[sens.groupby("c_flag")["cost"].idxmin()]
res["best_rule_by_flag_cost"] = best_by_cost[["c_flag", "rule", "cost", "saving", "saving_pct"]].to_dict("records")

# 盈亏平衡：标记成本升到多少时规则B不再值得做（相对不干预）
b = sens[(sens.rule == "lag>8 且非 Amazon")]
breakeven = b[b.saving <= 0].c_flag.min() if (b.saving <= 0).any() else None
res["rule_b_breakeven_flag_cost"] = int(breakeven) if breakeven else ">60"

# ---------- 2.5 分层优势比（Mantel-Haenszel），验证 lag 效应不是渠道/类目/浏览混杂 ----------
def mh_or(frame: pd.DataFrame, strata: str) -> float:
    num = den = 0.0
    for _, g in frame.groupby(strata, observed=True):
        a = int(((g["lag_min"] > 8) & (g["is_stockout"] == 1)).sum())
        b = int(((g["lag_min"] > 8) & (g["is_stockout"] == 0)).sum())
        c = int(((g["lag_min"] <= 8) & (g["is_stockout"] == 1)).sum())
        e = int(((g["lag_min"] <= 8) & (g["is_stockout"] == 0)).sum())
        n = len(g)
        num += a * e / n
        den += b * c / n
    return round(num / den, 2)


d["browse_q"] = pd.qcut(d["buyer_browsing_sec"], 5, duplicates="drop")
res["mantel_haenszel_or"] = {
    "by_channel": mh_or(d, "platform_channel"),
    "by_category": mh_or(d, "product_category"),
    "by_browse_quintile": mh_or(d, "browse_q"),
}
# 迟滞档内是否存在剂量反应
stale_d = d[d["lag_min"] > 8].copy()
stale_d["lag_bucket"] = pd.cut(stale_d["lag_min"], [8, 20, 30, 40, 50, 60])
res["dose_response_in_stale"] = (stale_d.groupby("lag_bucket", observed=True)
                                 .agg(n=("is_stockout", "size"), rate=("is_stockout", "mean"))
                                 .reset_index().assign(rate=lambda x: x["rate"].round(3),
                                                       lag_bucket=lambda x: x["lag_bucket"].astype(str))
                                 .to_dict("records"))

# ---------- 2.7 拦截率（efficacy）回填：标记只有部分概率真的避免砍单 ----------
# 期望节省 = e × TP × 90 − 15 × flag数 − m × FP（m=误报导致的转化损失，题目未给，默认 0）
idx_e = []
rules_e = {"规则A: lag>8": stale, "规则B: lag>8 且非Amazon": base_rule, "规则C: B + Amazon低库存": rule_c}
for name, flags in rules_e.items():
    y = d["is_stockout"].astype(bool)
    tp = int((flags & y).sum())
    fp = int((flags & ~y).sum())
    flagged = int(flags.sum())
    base = int(y.sum()) * 90
    for m in [0, 15, 30]:
        for e in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            saving = e * tp * 90 - 15 * flagged - m * fp
            idx_e.append(dict(rule=name, efficacy=round(e, 2), fp_loss=m, tp=tp, fp=fp,
                              flagged=flagged, saving=round(saving, 0),
                              saving_pct=round(100 * saving / base, 2)))
    be = (15 * flagged + 0 * fp) / (tp * 90) if tp else None
    be_m30 = (15 * flagged + 30 * fp) / (tp * 90) if tp else None
    res.setdefault("efficacy_breakeven", {})[name] = {
        "breakeven_e_fp_loss_0": round(be, 4) if be is not None else None,
        "breakeven_e_fp_loss_30": round(be_m30, 4) if be_m30 is not None else None,
    }
pd.DataFrame(idx_e).to_csv(OUT / "efficacy_sensitivity.csv", index=False, encoding="utf-8-sig")

# 灰度实验设计：50/50 分流，检验 e 是否 >= 30%（双比例检验，α=0.05，power=0.8）
from math import sqrt
y_b = d["is_stockout"].astype(bool)
p1 = float((base_rule & y_b).sum() / base_rule.sum())      # 对照组（不干预）砍单率
p2 = p1 * (1 - 0.30)                                       # 若 e=30%，处理组预期砍单率
pbar = (p1 + p2) / 2
z_a, z_b = 1.959963985, 0.841621234
n_arm = ((z_a * sqrt(2 * pbar * (1 - pbar)) + z_b * sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
         / (p1 - p2) ** 2)
flagged_per_week = base_rule.sum() / 7
res["pilot_design"] = {
    "control_stockout_rate_among_flagged": round(p1, 4),
    "detect_effect_size": 0.30,
    "n_per_arm": int(-(-n_arm // 1)),
    "flagged_per_day": round(float(base_rule.sum() / 7), 1),
    "estimated_days": round(float(n_arm / (base_rule.sum() / 7 / 2)), 1),  # 50/50 分流，处理组每天只有一半
}

fig, ax = plt.subplots(figsize=(8, 4.6))
for name in rules_e:
    g = pd.DataFrame(idx_e)
    g = g[(g.rule == name) & (g.fp_loss == 0)]
    ax.plot(g.efficacy, g.saving, marker="o", ms=3, label=name)
ax.axhline(0, color="k", lw=0.8)
ax.axvline(0.312, color="gray", ls="--", lw=1, label="规则B盈亏平衡 e≈31%")
ax.set_xlabel("拦截率 e（标记后真正避免砍单的比例）"); ax.set_ylabel("净节省（元/7天）")
ax.set_title("收益对拦截率高度敏感：e<31% 时规则B开始亏钱")
ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(OUT / "fig_efficacy.png", dpi=150); plt.close(fig)

# ---------- 3. 时序验证：前 4 天定规则，后 3 天验证 ----------
d["order_date"] = pd.to_datetime(d["order_time"], utc=True).dt.date
dates = sorted(d["order_date"].unique())
train_dates = set(dates[:5])   # 08-31 ~ 09-04
test = d[d["order_date"].isin(set(dates) - train_dates)]
tr = d[d["order_date"].isin(train_dates)]


def summary(sub: pd.DataFrame, name: str):
    st = sub["lag_min"] > 8
    non_amz = st & (sub["platform_channel"] != "amazon_us")
    base = int(sub["is_stockout"].sum()) * 90
    cost = rule_cost(sub, non_amz, 15, 90)
    return dict(period=name, n=len(sub), stockouts=int(sub["is_stockout"].sum()),
                stockout_rate=round(float(sub["is_stockout"].mean()), 4),
                stale_rate=round(float(sub.loc[st, "is_stockout"].mean()), 4),
                fresh_rate=round(float(sub.loc[~st, "is_stockout"].mean()), 4),
                rule_b_cost=cost, rule_b_saving=base - cost,
                rule_b_saving_pct=round(100 * (base - cost) / base, 2))


res["time_split"] = {
    "train": summary(tr, f"{dates[0]} ~ {dates[4]}"),
    "test": summary(test, f"{dates[5]} ~ {dates[-1]}"),
}

# ---------- 4. 小时分布：同步迟滞是否集中在某些时段 ----------
d["hour"] = pd.to_datetime(d["order_time"], utc=True).dt.tz_convert("Asia/Shanghai").dt.hour
hourly = d.groupby("hour").agg(orders=("order_id", "size"),
                               stale=("lag_min", lambda x: int((x > 8).sum())),
                               stockouts=("is_stockout", "sum")).reset_index()
hourly["stale_rate"] = (hourly["stale"] / hourly["orders"]).round(3)
hourly.to_csv(OUT / "hourly.csv", index=False, encoding="utf-8-sig")
res["hourly"] = hourly.to_dict("records")

# ---------- 5. 图：敏感性 ----------
fig, ax = plt.subplots(figsize=(8, 4.6))
for name in rules:
    g = sens[sens.rule == name]
    ax.plot(g.c_flag, g.saving, marker="o", ms=3, label=name)
ax.axhline(0, color="k", lw=0.8)
ax.set_xlabel("每次提前标记的成本（元）"); ax.set_ylabel("净节省（元/7天）")
ax.set_title("标记成本敏感性：规则 B 在成本升到 ~60 元前仍有效")
ax.legend(fontsize=7); fig.tight_layout(); fig.savefig(OUT / "fig_sensitivity.png", dpi=150); plt.close(fig)

(OUT / "robustness.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
