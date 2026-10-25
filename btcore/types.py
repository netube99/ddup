from dataclasses import dataclass, field


@dataclass
class Holding:
    symbol: str
    shares: int
    entry_date: str
    entry_price: float
    cost: float
    last_price: float = 0.0
    holding_days: int = 0
    conditions: list = field(default_factory=list)
    locked: bool = True


@dataclass
class Account:
    cash: float
    initial_capital: float
    total_value: float = 0.0
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    holdings: dict = field(default_factory=dict)
    slippage_ticks: int = 2
    # 成交量约束：单笔股数 <= 当日 vol(手) * ratio * 100（None=不限制）
    order_volume_ratio: float | None = None
    # 手动单成交价字段："open"(默认, 次日开盘) | "close"(次日收盘)
    execution_price: str = "open"


@dataclass
class Trade:
    date: str
    symbol: str
    side: str
    trigger: str
    price: float
    shares: int
    turnover: float
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage_amount: float = 0.0
    net_amount: float = 0.0
    reason: str = ""


@dataclass
class Snapshot:
    cash: float
    holdings: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    total_value: float = 0.0


def bar_get(bar, key, default=None):
    """安全读取 bar 字段，兼容 dict 和对象两种形态。"""
    if bar is None:
        return default
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def benchmark_price_column(df) -> str | None:
    """基准价格列口径：优先 hfq_close，缺列回退 close；两列都没有返回 None。

    provider/engine/stats 三侧共用的单一口径（F-STA-03：stats 此前用
    iloc[:, 0] 取第一列，后端返回不含 close/hfq_close 的 frame 时与
    provider 的“无有效基准”判定相矛盾）。
    """
    if "hfq_close" in df.columns:
        return "hfq_close"
    if "close" in df.columns:
        return "close"
    return None
