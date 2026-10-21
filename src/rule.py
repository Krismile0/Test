"""
生产参考实现：下单时判断是否应「提前标记缺货」。

决策依据（与 README 一致）：
    标记一次成本 15 元，漏掉一次砍单损失 90 元。
    因此当 P(砍单) > 15/90 ≈ 16.7% 时才值得标记。

用法：
    from rule import should_flag
    d = should_flag(lag_min=23, platform_channel="tiktok_us", stock_display=12)
    if d.flag:
        ...  # 库存置为「确认中」，不承诺可售

自测：
    python src/rule.py
"""
from dataclasses import dataclass

COST_STOCKOUT = 90.0
COST_FLAG = 15.0
BREAKEVEN_PROB = COST_FLAG / COST_STOCKOUT  # 0.1667

STALE_MINUTES = 8          # 9–11 分钟在数据中为空，8 分钟是自然断点
AMAZON = "amazon_us"
AMAZON_LOW_STOCK = 3       # Amazon 新鲜档库存 <=3 的砍单率 50%


@dataclass(frozen=True)
class Decision:
    flag: bool
    variant: str
    reason: str


def should_flag(*, lag_min: float, platform_channel: str,
                stock_display: int, variant: str = "B") -> Decision:
    """返回是否对该订单提前标记缺货。

    variant:
        "A" — 只要同步延迟 > 8 分钟就标记（不区分渠道）
        "B" — 延迟 > 8 分钟且非 Amazon（推荐默认）
        "C" — B + Amazon 新鲜档低库存兜底（灰度项）
    """
    if variant not in {"A", "B", "C"}:
        raise ValueError(f"unknown variant: {variant}")

    stale = lag_min > STALE_MINUTES
    is_amazon = platform_channel == AMAZON

    if variant == "A":
        if stale:
            return Decision(True, "A", f"同步延迟 {lag_min:.0f} 分钟 > {STALE_MINUTES}")
        return Decision(False, "A", "同步新鲜")

    if stale and not is_amazon:
        return Decision(True, variant, f"同步延迟 {lag_min:.0f} 分钟 > {STALE_MINUTES}，且非 Amazon")

    if variant == "C" and is_amazon and not stale and stock_display <= AMAZON_LOW_STOCK:
        return Decision(True, "C", f"Amazon 新鲜档但展示库存 {stock_display} <= {AMAZON_LOW_STOCK}")

    return Decision(False, variant, "未命中任何高风险条件")


def _selftest() -> None:
    f = should_flag
    # 边界：8 分钟不标，9 分钟标（非 Amazon）
    assert not f(lag_min=8, platform_channel="tiktok_us", stock_display=20).flag
    assert f(lag_min=9, platform_channel="tiktok_us", stock_display=20).flag
    # Amazon 迟滞不标（B）
    assert not f(lag_min=45, platform_channel="amazon_us", stock_display=20).flag
    # Amazon 迟滞在 A 下要标
    assert f(lag_min=45, platform_channel="amazon_us", stock_display=20, variant="A").flag
    # Amazon 新鲜低库存：B 不标，C 标
    assert not f(lag_min=5, platform_channel="amazon_us", stock_display=3).flag
    assert f(lag_min=5, platform_channel="amazon_us", stock_display=3, variant="C").flag
    # Amazon 新鲜但库存充足：C 也不标
    assert not f(lag_min=5, platform_channel="amazon_us", stock_display=10, variant="C").flag
    # 非 Amazon 新鲜低库存：C 不标（4.2% < 16.7%）
    assert not f(lag_min=5, platform_channel="temu_us", stock_display=1, variant="C").flag
    print("rule.py self-test passed")


if __name__ == "__main__":
    _selftest()
