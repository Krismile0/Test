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
