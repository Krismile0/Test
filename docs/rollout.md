# 上线方案（Production Rollout）

配套实现：`src/rule.py`（可直接 `from rule import should_flag`）。

---

## 1. 决策点放在哪

**放在「库存展示」之前，而不是下单之后。**

```
海外仓同步 → [新鲜度闸门] → 库存展示 → 买家下单 → 履约
                 ↑
            lag > 8 分钟？
```

- 若在下单后才标记，买家已经付过钱，只能走退款/取消，损失仍是 90 元；
- 在展示环节拦截，买家看到的是「库存确认中」，不会产生支付，15 元成本才有意义。

> 题目给的是「提前标记缺货」，本方案按「展示前拦截」实现。若业务只能做事后标记，规则不变，但收益要乘上拦截率 e（见 README 的灰度实验）。

---

## 2. 参考实现

```python
from rule import should_flag

d = should_flag(
    lag_min=(order_time_utc - stock_synced_at_utc).total_seconds() / 60,
    platform_channel=row.platform_channel,
    stock_display=row.stock_display,
    variant="B",          # 灰度期用 B；验证 e 后可切 C
)

if d.flag:
    show_stock_state("confirming")   # 不展示具体件数
else:
    show_stock_state("in_stock", row.stock_display)
```

等价的 SQL（在库存展示查询里加一列）：

```sql
SELECT sku_id,
       CASE WHEN lag_min > 8 AND platform_channel <> 'amazon_us' THEN 0
            ELSE stock_display END AS display_stock
FROM (
  SELECT s.*,
         EXTRACT(EPOCH FROM (NOW() - last_synced_at)) / 60 AS lag_min
  FROM sku_stock_snapshot s
) t;
```

---

## 3. 配置项（不要硬编码）

| 配置 | 建议值 | 说明 |
|---|---|---|
| `stale_threshold_min` | 8 | 数据自然断点；若上游同步周期改变，需重新扫描 |
| `excluded_channels` | `amazon_us` | Amazon 迟滞档砍单率 9.1% < 16.7% 决策线 |
| `amazon_low_stock` | 3 | 仅用于规则 C，灰度阶段开启 |
| `cost_stockout / cost_flag` | 90 / 15 | 成本变化时重算盈亏平衡点 |
| `variant` | B | 灰度通过后评估切 C |

---

## 4. 买家侧文案

- 命中：`库存确认中，预计 10 分钟内告知`（不要写「缺货」，避免误伤）
- 确认有货：恢复正常展示并优先发货
- 确认无货：主动通知 + 补偿券，把砍单转成「延迟履约」

关键：**文案决定误报的真实代价**。如果「确认中」会显著降低转化，README 中「误报 30 元/笔」的压力测试（盈亏平衡 e 升到 60.4%）就是必须复核的红线。

---

## 5. 监控与告警（上线即配）

| 指标 | 当前基线 | 告警线 |
|---|---|---|
| 迟滞档订单占比 | 25.0% | 连续 3 天 > 35%（同步在退化） |
| 迟滞档砍单率 | 42.1% | 连续 3 天 > 55%（库存本身出问题） |
| 标记率（flagged / 总单） | 18.7%（193/1034） | 单日 > 30%（规则误触发） |
| 标记单最终砍单率 | 53.4% | < 25%（规则已失效，需重训阈值） |
| Amazon 新鲜低库存砍单率 | 50% | 连续 3 天 < 20%（可关闭规则 C） |

---

## 6. 灰度与测量拦截率 e

1. 对命中订单 50/50 随机分流：处理组标记，对照组照常；
2. 跑 **约 11 天**（每组需 151 单，见 `outputs/robustness.json` 的 `pilot_design`）；
3. `e = 1 − 处理组砍单率 / 对照组砍单率`；
4. 判定：
   - e ≥ 31.2% → 规则 B 净收益为正，全量；
   - e < 31.2% → 停止标记，只能走「修同步」路线（预期减少 73.5% 砍单）；
   - 若误报导致 30 元/笔转化损失，门槛提高到 60.4%。

---

## 7. 回滚条件

出现任一情况立即回滚到「不过滤、正常展示」：

- 标记率连续 2 小时 > 40%；
- 订单转化率相对对照组下降 > 5%；
- 同步管道恢复后，迟滞档占比 < 5%（规则已无必要）。

---

## 8. 与「修同步」的关系

| 方案 | 成本 | 预期效果 | 上线难度 |
|---|---|---|---|
| 规则 B（本方案） | 15 元/次 | 省 52.1%（e=100% 上限） | 低，改展示层即可 |
| 同步 SLA 压到 8 分钟内 | 工程投入 | 砍单减少 73.5%（反事实） | 中，需改上游管道 |
| 两者都做 | — | 规则只在同步故障时触发，成本更低 | — |

**结论：规则是止血，修同步是治本。先上规则拿时间，同步改造并行推进。**
