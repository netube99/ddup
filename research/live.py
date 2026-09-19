"""实盘账本与回放 — 状态与策略完全解耦。

账本（ledger）是唯一持久化状态：append-only 成交记录 + 账户元数据，
不存任何策略内部状态。算信号时把账本灌进引擎回放：逐日应用真实成交、
原生公司行为（含 trailing 锚点 rescale）、估值结算，策略钩子逐日演化，
末日 compute_pending 的输出即次日操作单。策略可随意切换——换一份
YAML 重新回放即得到该策略口径下的明日操作。

表结构（与回测结果库同库共存）：
  ledger_meta   — 账户元数据（key/value，唯一手工维护的账户参数）
  ledger_fills  — 用户成交（BUY/SELL/ADJUST/OPENING），append-only 唯一真相源
  runs/trade_log/account_daily/holdings — 衍生表，每次 signal 整体重写，
                  与回测结果库同 schema，report/cross_validate/replay 直接可用
"""

import bisect
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from btcore import corporate, database, types
from btcore.engine import Engine, bars_to_dict, value_account
from btcore.match.core import finalize_sell, is_valid_price

logger = logging.getLogger(__name__)

LIVE_RUN_ID = 1  # 衍生表统一挂在 run_id=1（每个账本库一个逻辑 run）
LEDGER_SCHEMA_VERSION = 1  # ledger_meta.schema_version 当前版本（LIVE-14）


# ── 输入校验工具（LIVE-01/02/06）──


def require_finite(value, field: str) -> float:
    """数值入口统一 fail-fast：非数值或 NaN/Inf 直接 ValueError。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 非法（非数值）: {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} 非法（NaN/Inf）: {value!r}")
    return number


def validate_date_format(date: str) -> str:
    """校验 YYYYMMDD 八位真实日期（INFRA-05）。"""
    text = str(date)
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"日期需为 YYYYMMDD 八位数字: {date!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise ValueError(f"日期非法: {date!r}") from None
    return text


def validate_open_day(date: str, provider) -> None:
    """init --date 开市日校验：非开市日 fail-fast 并提示最近开市日。"""
    if date not in provider.get_calendar(date, date):
        prev = provider.prev_trading_day(date)
        hint = f"，最近开市日 {prev}" if prev else "（早于行情数据范围？）"
        raise ValueError(f"{date} 不是开市日{hint}")


def has_market_data(provider, date: str) -> bool:
    """全市场当日是否有行情（与 signal 对 signal_date 有 bars 的判据同口径）。"""
    bars = provider.get_engine_bars(
        None, date, lookback_start=date, columns=["close"],
    )
    return not bars.empty


def validate_fill_dates(fills: list[dict], calendar: list[str],
                        default_date: str | None = None) -> list[str]:
    """校验 fill 显式/缺省日期均落在日历内（LIVE-01）。

    返回问题描述列表（含行号/日期/symbol）；空列表 = 全部合法。
    """
    open_days = set(calendar)
    problems = []
    for i, f in enumerate(fills, start=1):
        if not isinstance(f, dict):
            problems.append(f"第 {i} 行: 非法条目 {f!r}")
            continue
        date = str(f.get("date") or default_date or "")
        if date not in open_days:
            problems.append(
                f"第 {i} 行: date={date!r} symbol={f.get('symbol')!r}"
            )
    return problems


LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS ledger_fills_id_seq;
CREATE TABLE IF NOT EXISTS ledger_fills (
    id           BIGINT PRIMARY KEY DEFAULT nextval('ledger_fills_id_seq'),
    date         VARCHAR NOT NULL,
    symbol       VARCHAR NOT NULL DEFAULT '',
    side         VARCHAR NOT NULL,
    price        DOUBLE NOT NULL DEFAULT 0,
    shares       BIGINT NOT NULL DEFAULT 0,
    commission   DOUBLE NOT NULL DEFAULT 0,
    stamp_tax    DOUBLE NOT NULL DEFAULT 0,
    transfer_fee DOUBLE NOT NULL DEFAULT 0,
    reason       VARCHAR NOT NULL DEFAULT '',
    created_at   VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_fills_date ON ledger_fills(date);

CREATE TABLE IF NOT EXISTS ledger_holdings (
    symbol       VARCHAR PRIMARY KEY,
    entry_date   VARCHAR NOT NULL,
    entry_price  DOUBLE NOT NULL,
    shares       BIGINT NOT NULL,
    cost         DOUBLE NOT NULL,
    last_price   DOUBLE NOT NULL DEFAULT 0,
    holding_days BIGINT NOT NULL DEFAULT 0
);
"""

_FILL_SIDES = frozenset({"BUY", "SELL", "ADJUST", "OPENING"})


@dataclass
class ReconReport:
    """sync 对账结果：持仓逐只比对 + 现金差额。"""

    date: str
    ok: bool
    holding_diffs: dict = field(default_factory=dict)  # symbol -> (derived, actual)
    cash_derived: float = 0.0
    cash_actual: float = 0.0
    cash_delta: float = 0.0
    derived_holdings: dict = field(default_factory=dict)
    actual_holdings: dict = field(default_factory=dict)


class LedgerStore:
    """账本库读写。用户数据只进 ledger_fills/ledger_meta；其余表为衍生。"""

    def __init__(self, path: str):
        self.path = path
        # init_backtest_db 建 runs/trade_log/account_daily/holdings 同构表
        self.conn = database.init_backtest_db(path)
        self.conn.execute(LEDGER_SCHEMA_SQL)
        self.conn.commit()
        # schema 版本治理（LIVE-14）：缺失 = 兼容旧账本，未知版本 fail-fast
        version = self.get_meta("schema_version")
        if version is not None and version != str(LEDGER_SCHEMA_VERSION):
            self.conn.close()
            raise ValueError(
                f"账本 schema_version={version} 不受支持"
                f"（当前版本 {LEDGER_SCHEMA_VERSION}）——请使用对应版本的 ddup"
            )

    def close(self):
        self.conn.close()

    # ── 元数据 ──

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM ledger_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    def is_initialized(self) -> bool:
        return self.get_meta("initial_capital") is not None

    def init_account(self, start_date: str, initial_cash: float,
                     initial_capital: float):
        """写账户元数据；调用方负责事务边界（cmd_init 整段事务化）。

        本方法不 commit：DuckDB 下调用方可能已 begin()，内部提交会破坏
        建账+OPENING 持仓写入的原子性。
        """
        if self.is_initialized():
            raise ValueError("账本已初始化（initial_capital 已存在）")
        start_date = validate_date_format(start_date)
        cash = require_finite(initial_cash, "initial_cash")
        capital = require_finite(initial_capital, "initial_capital")
        if cash < 0 or capital <= 0:
            raise ValueError(
                f"initial_cash/initial_capital 非法: {cash}, {capital}"
            )
        self.set_meta("schema_version", str(LEDGER_SCHEMA_VERSION))
        self.set_meta("start_date", start_date)
        self.set_meta("initial_cash", repr(cash))
        self.set_meta("initial_capital", repr(capital))

    @property
    def start_date(self) -> str:
        v = self.get_meta("start_date")
        if v is None:
            raise ValueError("账本未初始化")
        return validate_date_format(v)

    def _finite_meta(self, key: str) -> float:
        return require_finite(self.get_meta(key) or "0", key)

    @property
    def initial_cash(self) -> float:
        return self._finite_meta("initial_cash")

    @property
    def initial_capital(self) -> float:
        return self._finite_meta("initial_capital")

    # ── 成交 ──

    def append_fill(self, date: str, symbol: str, side: str, *,
                    price: float = 0.0, shares: int = 0,
                    commission: float = 0.0, stamp_tax: float = 0.0,
                    transfer_fee: float = 0.0, reason: str = "",
                    created_at: str):
        side = str(side).strip().upper()
        if side not in _FILL_SIDES:
            raise ValueError(f"非法 fill side: {side!r}（允许 {sorted(_FILL_SIDES)}）")
        price = require_finite(price, f"{side} price")
        commission = require_finite(commission, f"{side} commission")
        stamp_tax = require_finite(stamp_tax, f"{side} stamp_tax")
        transfer_fee = require_finite(transfer_fee, f"{side} transfer_fee")
        if side in ("BUY", "SELL", "OPENING"):
            if not symbol or not isinstance(shares, int) or shares <= 0:
                raise ValueError(
                    f"{side} 需要 symbol 与正整数股数: symbol={symbol!r} shares={shares!r}"
                )
            if not is_valid_price(price):
                raise ValueError(f"{side} 需要正数成交价: {price!r}")
        self.conn.execute(
            "INSERT INTO ledger_fills (date, symbol, side, price, shares,"
            " commission, stamp_tax, transfer_fee, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, symbol, side, price, int(shares),
             commission, stamp_tax, transfer_fee, reason, created_at),
        )

    def _idempotency_key(self, row) -> tuple:
        """幂等判重键：仅用于比较，不写回归一化值（LIVE-15）。"""
        date, symbol, side, price, shares, commission, stamp_tax, \
            transfer_fee, reason = row
        return (str(date), str(symbol), str(side).strip().upper(),
                round(float(price), 6), int(shares),
                round(float(commission), 6), round(float(stamp_tax), 6),
                round(float(transfer_fee), 6), str(reason))

    def append_fills_idempotent(self, fills: list[dict],
                                created_at: str,
                                default_date: str | None = None) -> tuple[int, int]:
        """幂等追加：完全重复的成交跳过（agent 重跑同一 statement 不双重入账）。

        重复判定按 (date, symbol, side, price, shares, commission,
        stamp_tax, transfer_fee, reason) 全字段归一化后精确匹配；归一化仅用于
        判重，落库保留原始数值（LIVE-15）。OPENING 仅用于 init 建账，sync
        批量入口拒绝（LIVE-08）。
        default_date: fill 缺 date 时的缺省成交日（sync.yaml 的 statement
        级 date——见 docs/cli_and_research.md §2.9 的 fills 格式）。
        返回 (appended, skipped)。
        """
        existing = {
            self._idempotency_key(r)
            for r in self.conn.execute(
                "SELECT date, symbol, side, price, shares, commission,"
                " stamp_tax, transfer_fee, reason FROM ledger_fills"
            ).fetchall()
        }
        appended = skipped = 0
        for i, f in enumerate(fills, start=1):
            side = str(f["side"]).strip().upper()
            if side == "OPENING":
                raise ValueError(
                    f"第 {i} 行 side=OPENING 不被 sync 接受"
                    "——OPENING 仅用于 init --positions 建账"
                )
            date = str(f.get("date") or default_date or "")
            price = require_finite(f["price"], f"第 {i} 行 price")
            shares_value = require_finite(f["shares"], f"第 {i} 行 shares")
            if not float(shares_value).is_integer():
                raise ValueError(
                    f"第 {i} 行 shares 必须为整数: {f['shares']!r}"
                )
            shares = int(shares_value)
            commission = require_finite(
                f.get("commission") or 0.0, f"第 {i} 行 commission")
            stamp_tax = require_finite(
                f.get("stamp_tax") or 0.0, f"第 {i} 行 stamp_tax")
            transfer_fee = require_finite(
                f.get("transfer_fee") or 0.0, f"第 {i} 行 transfer_fee")
            reason = str(f.get("reason") or "MANUAL")
            key = self._idempotency_key(
                (date, f["symbol"], side, price, shares, commission,
                 stamp_tax, transfer_fee, reason))
            if key in existing:
                skipped += 1
                continue
            self.append_fill(date, f["symbol"], side, price=price,
                             shares=shares, commission=commission,
                             stamp_tax=stamp_tax, transfer_fee=transfer_fee,
                             reason=reason, created_at=created_at)
            existing.add(key)  # 同批完全重复的 fill 也跳过
            appended += 1
        return appended, skipped

    def fills(self, side_filter: frozenset | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, date, symbol, side, price, shares, commission,"
            " stamp_tax, transfer_fee, reason FROM ledger_fills ORDER BY id"
        ).fetchall()
        cols = ["id", "date", "symbol", "side", "price", "shares",
                "commission", "stamp_tax", "transfer_fee", "reason"]
        out = [dict(zip(cols, r)) for r in rows]
        if side_filter is not None:
            out = [f for f in out if f["side"] in side_filter]
        return out

    def fills_by_date(self) -> dict[str, list[dict]]:
        by_date: dict[str, list[dict]] = {}
        for f in self.fills(side_filter=frozenset({"BUY", "SELL", "ADJUST"})):
            by_date.setdefault(f["date"], []).append(f)
        return by_date

    # ── 衍生表重写 ──

    def rewrite_derived(self, daily_rows: list[dict], trade_rows: list[dict],
                        account: types.Account, initial_capital: float):
        """signal 回放产出整体重写（DELETE+INSERT，幂等确定性）。"""
        with database.transaction(self.conn):
            # 衍生表按 LIVE_RUN_ID 固定挂载：先校验 runs 行归属，再删写，
            # 避免 run_id 被回测占用时静默覆写（fail-fast 而非并存）
            existing = self.conn.execute(
                "SELECT strategy FROM runs WHERE run_id = ?", (LIVE_RUN_ID,)
            ).fetchone()
            end_date = daily_rows[-1]["date"] if daily_rows else self.start_date
            if existing is None:
                database.write_run(
                    self.conn, run_id=LIVE_RUN_ID,
                    created_at=pd.Timestamp.now().isoformat(),
                    strategy="live", start_date=self.start_date,
                    end_date=end_date,
                    initial_capital=initial_capital,
                    config_json=json.dumps({"ledger": self.path}, ensure_ascii=False),
                    status="live",
                )
            elif existing[0] != "live":
                raise ValueError(
                    f"账本库 run_id={LIVE_RUN_ID} 已被非账本 run 占用"
                    f"（strategy={existing[0]!r}）——请使用独立的账本库文件"
                )
            else:
                # signal 逐日推进：区间末端跟随回放末日（报告/对比展示口径）
                self.conn.execute(
                    "UPDATE runs SET end_date = ? WHERE run_id = ?",
                    (end_date, LIVE_RUN_ID),
                )
            self.conn.execute("DELETE FROM trade_log WHERE run_id = ?", (LIVE_RUN_ID,))
            self.conn.execute("DELETE FROM account_daily WHERE run_id = ?", (LIVE_RUN_ID,))
            for r in daily_rows:
                database.write_daily(
                    self.conn, LIVE_RUN_ID, r["date"], r["cash"], r["total_value"],
                    r["daily_pnl"], r["cumulative_pnl"], initial_capital,
                    r["n_holdings"],
                )
            database.write_trades(self.conn, LIVE_RUN_ID, trade_rows)
            database.write_holdings(self.conn, account)
            # init_backtest_db 每次打开清空 holdings 瞬态表；ledger_holdings
            # 是跨会话持久的当前持仓快照（status 数据源）
            self.conn.execute("DELETE FROM ledger_holdings")
            if account.holdings:
                self.conn.executemany(
                    "INSERT INTO ledger_holdings (symbol, entry_date,"
                    " entry_price, shares, cost, last_price, holding_days)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (h.symbol, h.entry_date, h.entry_price, h.shares,
                         h.cost, h.last_price, h.holding_days)
                        for h in account.holdings.values()
                    ],
                )


# ── 成交应用（账本 → 账户状态机）──


def _check_cash_nonneg(account: types.Account, today: str, note: str) -> None:
    """LIVE-13：应用 fill 后现金为负 → fail-fast（不允许持久化负现金）。"""
    if account.cash < -1e-9:
        raise ValueError(
            f"[{today}] 应用成交后现金为负 {account.cash:.2f}（{note}）"
            "——账本数据不一致，拒绝继续"
        )


def apply_fill(account: types.Account, fill: dict, today: str) -> types.Trade | None:
    """把一条账本成交应用到账户。返回 Trade（BUY/SELL）或 None（ADJUST）。

    与撮合层的差异：价格/费用用真实成交值，无滑点、无费用模型、
    无涨跌停/成交量校验（券商已成交即是事实）。数值入口全部 isfinite
    校验（LIVE-02），应用后现金为负即 fail-fast（LIVE-13）。
    """
    side = fill["side"]
    if side == "ADJUST":
        amount = require_finite(fill["price"], "ADJUST 金额")
        account.cash += amount  # ADJUST 的金额存在 price 字段
        _check_cash_nonneg(account, today, "ADJUST")
        return None

    symbol = fill["symbol"]
    price = require_finite(fill["price"], f"{side} {symbol} price")
    shares = int(require_finite(fill["shares"], f"{side} {symbol} shares"))
    commission = require_finite(fill.get("commission") or 0.0,
                                f"{side} {symbol} commission")
    stamp_tax = require_finite(fill.get("stamp_tax") or 0.0,
                               f"{side} {symbol} stamp_tax")
    transfer_fee = require_finite(fill.get("transfer_fee") or 0.0,
                                  f"{side} {symbol} transfer_fee")
    turnover = price * shares

    if side == "BUY":
        net = -(turnover + commission + transfer_fee)
        account.cash += net
        holding = account.holdings.get(symbol)
        if holding is not None:
            # 加仓：加权均价，保留原 entry_date（红利税持股期口径偏保守）
            total_shares = holding.shares + shares
            holding.entry_price = (
                holding.entry_price * holding.shares + price * shares
            ) / total_shares
            holding.cost += turnover
            holding.shares = total_shares
            holding.locked = True  # 当日有买入即锁定全仓（A股 T+1 同口径）
        else:
            account.holdings[symbol] = types.Holding(
                symbol=symbol, shares=shares, entry_date=today,
                entry_price=price, cost=turnover,
                last_price=price, locked=True,
            )
        _check_cash_nonneg(account, today, f"BUY {symbol}")
        return types.Trade(
            date=today, symbol=symbol, side="BUY", trigger=fill["reason"],
            price=price, shares=shares, turnover=turnover,
            commission=commission, stamp_tax=0.0,
            transfer_fee=transfer_fee, slippage_amount=0.0,
            net_amount=net, reason=fill["reason"],
        )

    # SELL
    holding = account.holdings.get(symbol)
    if holding is None:
        raise ValueError(f"[{today}] 卖出未持仓标的 {symbol}——账本缺买入记录")
    if shares > holding.shares:
        raise ValueError(
            f"[{today}] 卖出 {symbol} {shares} 股超过持仓 {holding.shares}——账本不一致"
        )
    net = turnover - commission - stamp_tax - transfer_fee
    account.cash += net
    finalize_sell(account, holding, shares)
    _check_cash_nonneg(account, today, f"SELL {symbol}")
    return types.Trade(
        date=today, symbol=symbol, side="SELL", trigger=fill["reason"],
        price=price, shares=shares, turnover=turnover,
        commission=commission, stamp_tax=stamp_tax,
        transfer_fee=transfer_fee, slippage_amount=0.0,
        net_amount=net, reason=fill["reason"],
    )


def seed_opening(account: types.Account, store: LedgerStore, provider,
                 init_date: str):
    """建仓持仓以 OPENING 条目入账：种子持仓（真实 entry_date/entry_price）。

    holding_days 种子值 = entry 到 init 之间引擎会经历的结算次数
    （交易日历上 (entry, init) 的开区间长度），使回放首日 compute_pending
    递增后与连续运行的引擎口径一致。init 非开市日时锚定 ≤init 的最近
    开市日（bisect 防御，LIVE-06）。
    """
    calendar = provider.get_calendar("20000101", init_date)
    anchor_idx = bisect.bisect_right(calendar, init_date) - 1
    for f in store.fills(side_filter=frozenset({"OPENING"})):
        symbol, shares = f["symbol"], f["shares"]
        entry = f["date"]
        if entry not in calendar:
            logger.warning("OPENING %s entry_date %s 非交易日，口径近似", symbol, entry)
        entry_idx = max(0, bisect.bisect_right(calendar, entry) - 1)
        seed_days = max(0, anchor_idx - entry_idx)
        existing = account.holdings.get(symbol)
        if existing is not None:
            total = existing.shares + shares
            existing.entry_price = (
                existing.entry_price * existing.shares + f["price"] * shares
            ) / total
            existing.cost += f["price"] * shares
            existing.shares = total
            continue
        account.holdings[symbol] = types.Holding(
            symbol=symbol, shares=shares, entry_date=entry,
            entry_price=f["price"], cost=f["price"] * shares,
            last_price=f["price"], holding_days=seed_days,
            locked=(entry >= init_date),
        )


# ── 回放 ──


def replay_ledger(engine: Engine, provider, store: LedgerStore,
                  calendar: list[str], run_decisions: bool = True,
                  collect: bool = False,
                  price_fallback: dict | None = None):
    """账本驱动回放：真实成交替代撮合，策略钩子逐日演化（run_decisions 时）。

    engine 须已完成 prepare（因子物化/attach_bars/on_start）且账户已播种。
    price_fallback: {date: {symbol: close}}，universe 外持仓的估值补价。
    collect=True 时返回 (daily_rows, trade_rows) 供衍生表重写。
    """
    fills_by_date = store.fills_by_date()
    daily_rows: list[dict] = []
    trade_rows: list[types.Trade] = []
    # 建仓买入入衍生 trade_log（trigger=OPENING），ML 回合配对/归因需要完整故事
    for f in store.fills(side_filter=frozenset({"OPENING"})):
        trade_rows.append(types.Trade(
            date=f["date"], symbol=f["symbol"], side="BUY", trigger="OPENING",
            price=f["price"], shares=f["shares"], turnover=f["price"] * f["shares"],
            commission=0.0, stamp_tax=0.0, transfer_fee=0.0,
            slippage_amount=0.0, net_amount=0.0, reason="opening",
        ))
    # ADJUST 条目也入衍生 trade_log（现金审计轨迹）
    for f in store.fills(side_filter=frozenset({"ADJUST"})):
        trade_rows.append(types.Trade(
            date=f["date"], symbol="", side="ADJUST", trigger="ADJUST",
            price=0.0, shares=0, turnover=0.0,
            commission=0.0, stamp_tax=0.0, transfer_fee=0.0,
            slippage_amount=0.0, net_amount=f["price"],
            reason=f["reason"] or "cash_adjust",
        ))

    # 与 run() 首日行为对齐：首日前一交易日先算一次 pending（策略状态播种）。
    # 仅在有持仓时播种——回测里 prev_day 的信号由首日撮合执行，而空仓新账本
    # 首日没有任何 fill 可承接，播种只会白白消耗首次调仓触发（_last_rebalance
    # 被置为 prev_day），导致建仓单为空且账户空转一个调仓周期。
    prev_day = provider.prev_trading_day(calendar[0])
    if prev_day and run_decisions and engine.account.holdings:
        engine.compute_pending(prev_day)

    for today in calendar:
        day_bars = engine.bars_by_date.get(today)
        bars_dict = bars_to_dict(day_bars, today) if day_bars is not None else {}

        corporate_log: list = []
        corporate.adjust(engine.account, today, bars_dict, provider, corporate_log)
        corporate.apply_condition_rescale(engine.strategy, corporate_log)
        trade_rows.extend(corporate.derived_trades(corporate_log))

        day_trades = []
        for fill in fills_by_date.get(today, []):
            trade = apply_fill(engine.account, fill, today)
            if trade is not None:
                day_trades.append(trade)
                trade_rows.append(trade)

        if bars_dict or price_fallback:
            value_account(engine.account, bars_dict,
                          fallback_closes=(price_fallback or {}).get(today))

        if run_decisions and bars_dict:
            # 策略决策层：on_fills(当日真实成交) → on_tick → select →
            # calc_conditions；末日之前的 pending_actions 全部丢弃——
            # 真实世界已按账本发生
            engine.compute_pending(today, bars_dict, day_trades)

        if collect:
            daily_rows.append({
                "date": today,
                "cash": engine.account.cash,
                "total_value": engine.account.total_value,
                "daily_pnl": engine.account.daily_pnl,
                "cumulative_pnl": engine.account.cumulative_pnl,
                "n_holdings": len(engine.account.holdings),
            })

    if collect:
        return daily_rows, trade_rows
    return None


# ── 轻量回放（sync 对账：无因子、无策略钩子，秒级）──


def light_replay(store: LedgerStore, provider, end: str) -> types.Account:
    """只重放现金/持仓：公司行为 + 真实成交 + 收盘估值。供 sync 对账。"""
    account = types.Account(
        cash=store.initial_cash, initial_capital=store.initial_capital,
    )
    symbols = {f["symbol"] for f in store.fills() if f["symbol"]}
    seed_opening(account, store, provider, store.start_date)
    symbols |= set(account.holdings.keys())

    calendar = provider.get_calendar(store.start_date, end)
    if not calendar:
        raise ValueError(f"日历为空: {store.start_date} ~ {end}")
    if symbols:
        bars_df = provider.get_engine_bars(
            sorted(symbols), calendar[-1],
            lookback_start=calendar[0], columns=["close", "pre_close"],
        )
        bars_df.sort_index(inplace=True)
    else:
        bars_df = None

    fills_by_date = store.fills_by_date()
    for today in calendar:
        if bars_df is not None and today in bars_df.index.get_level_values(0):
            bars_dict = bars_to_dict(bars_df.loc[today], today)
        else:
            bars_dict = {}
        corporate_log: list = []
        corporate.adjust(account, today, bars_dict, provider, corporate_log)
        for fill in fills_by_date.get(today, []):
            apply_fill(account, fill, today)
        if bars_dict:
            value_account(account, bars_dict)
    return account


def reconcile(store: LedgerStore, provider, date: str,
              actual_cash: float, actual_holdings: dict) -> ReconReport:
    """对账：轻量回放衍生状态 vs 券商实际。持仓股数必须逐只相等。"""
    actual_cash = require_finite(actual_cash, "actual_cash")
    holdings: dict[str, int] = {}
    for symbol, shares in actual_holdings.items():
        value = require_finite(shares, f"holdings[{symbol}].shares")
        if not float(value).is_integer() or value <= 0:
            raise ValueError(
                f"holdings[{symbol}].shares 必须为正整数: {shares!r}"
            )
        holdings[str(symbol)] = int(value)
    account = light_replay(store, provider, date)
    derived = {s: h.shares for s, h in account.holdings.items()}
    diffs = {}
    for symbol in sorted(set(derived) | set(holdings)):
        d, a = derived.get(symbol, 0), holdings.get(symbol, 0)
        if d != a:
            diffs[symbol] = (d, a)
    delta = actual_cash - account.cash
    return ReconReport(
        date=date, ok=not diffs,
        holding_diffs=diffs,
        cash_derived=account.cash, cash_actual=actual_cash, cash_delta=delta,
        derived_holdings=derived, actual_holdings=holdings,
    )


# ── 信号编排 ──


def build_price_fallback(provider, symbols: set, calendar: list[str],
                         engine: Engine) -> dict | None:
    """universe 外持仓的估值补价面板：{date: {symbol: close}}。

    账本会持仓到策略 universe 外的标的（切策略/手动买入），策略面板按
    截面口径不能扩列，估值价单独取。面板内符号不取（零成本 fast path）。
    """
    panel_symbols = set(engine.bars_df.index.get_level_values("symbol")) \
        if engine.bars_df is not None else set()
    extra = sorted(symbols - panel_symbols)
    if not extra:
        return None
    df = provider.get_engine_bars(
        extra, calendar[-1], lookback_start=calendar[0], columns=["close"],
    )
    df.sort_index(inplace=True)
    out: dict[str, dict[str, float]] = {}
    for (date, symbol), close in df["close"].items():
        if close == close and close > 0:
            out.setdefault(date, {})[symbol] = float(close)
    return out


def run_signal(strategy, provider, store: LedgerStore, end: str):
    """全量回放 → 末日 pending_actions + 衍生表重写。返回 (engine, calendar)。"""
    engine = Engine(strategy, provider, initial_capital=store.initial_capital)
    engine.account.cash = store.initial_cash
    seed_opening(engine.account, store, provider, store.start_date)
    calendar = engine.prepare(store.start_date, end)
    if calendar[-1] != end:
        raise ValueError(f"{end} 不是交易日（日历末日 {calendar[-1]}）——数据未更新？")
    if end not in engine.bars_by_date:
        raise ValueError(f"{end} 无行情数据——请先更新行情数据库")
    ledger_symbols = {f["symbol"] for f in store.fills() if f["symbol"]}
    ledger_symbols |= set(engine.account.holdings.keys())
    price_fallback = build_price_fallback(provider, ledger_symbols, calendar, engine)
    daily_rows, trade_rows = replay_ledger(
        engine, provider, store, calendar, run_decisions=True, collect=True,
        price_fallback=price_fallback,
    )
    store.rewrite_derived(daily_rows, trade_rows, engine.account,
                          store.initial_capital)
    return engine, calendar


def build_op_sheet(engine: Engine, provider, today: str) -> dict:
    """次日操作单：开盘手动单 + 券商条件单 + 提示。全部来自末日决策输出。"""
    pending = engine.pending_actions or {}
    account = engine.account
    probe_end = (pd.Timestamp(today) + pd.Timedelta(days=30)).strftime("%Y%m%d")
    next_day = None
    for d in provider.get_calendar(today, probe_end):
        if d > today:
            next_day = d
            break

    sell_reasons = pending.get("sell_reasons") or {}
    sell_shares = pending.get("sell_shares") or {}
    sells = [{
        "symbol": s,
        "shares": sell_shares.get(s, account.holdings[s].shares
                 if s in account.holdings else None),
        "reason": sell_reasons.get(s, "MANUAL"),
    } for s in pending.get("sell", [])]

    # 买入预估：manual_buy 等权口径（总资产/max_positions，现金均分兜底），
    # 价格用 T 收盘——实际股数以明日开盘价为准
    max_pos = engine.max_positions
    per_pos = account.total_value / max_pos if max_pos > 0 else 0.0
    buys = []
    buy_list = pending.get("buy", [])
    weights = pending.get("buy_weights")
    n_left = len(buy_list)
    for s in buy_list:
        bar = engine.bars_by_date.get(today)
        close = None
        if bar is not None and s in bar.index:
            close = float(bar.loc[s, "close"])
        if weights is not None:
            amount = min(account.total_value * weights[s], account.cash)
        else:
            amount = min(per_pos, account.cash / n_left)
        est_shares = int(amount / close / 100) * 100 if close and close > 0 else None
        buys.append({"symbol": s, "ref_close": close,
                     "est_amount": round(amount, 2), "est_shares": est_shares})
        n_left -= 1

    conditions = []
    for symbol, holding in sorted(account.holdings.items()):
        if not holding.conditions:
            continue
        conditions.append({
            "symbol": symbol,
            "shares": holding.shares,
            "entry_price": round(holding.entry_price, 4),
            "holding_days": holding.holding_days,
            "orders": [
                {"type": c.get("type"), "trigger_price": c.get("price")}
                for c in holding.conditions
            ],
        })

    notices = []
    day_bars = engine.bars_by_date.get(today)
    # universe 外持仓（切策略/手动买入）用补价面板判停牌，避免误报（LIVE-03）。
    # t1_lock 不再提示：操作单面向次日，届时当日买入已解锁（LIVE-04 删除死代码）
    fallback_today = (build_price_fallback(
        provider, set(account.holdings), [today], engine) or {}).get(today, {})
    for symbol in sorted(account.holdings.keys()):
        if day_bars is not None and symbol in day_bars.index:
            continue
        if symbol in fallback_today:
            continue
        notices.append({"type": "suspended", "symbol": symbol,
                        "message": f"{symbol} 今日无行情（停牌？），明日不可操作"})
    if next_day:
        divs = provider.get_dividends_on_date(next_day) or {}
        for symbol in sorted(account.holdings.keys()):
            if symbol in divs:
                notices.append({"type": "ex_div", "symbol": symbol,
                                "message": f"{symbol} 明日除权除息: {divs[symbol]}"})

    return {
        "signal_date": today,
        "trade_date": next_day,
        "account": {
            "cash": round(account.cash, 2),
            "total_value": round(account.total_value, 2),
            "n_holdings": len(account.holdings),
            "holdings": {s: h.shares for s, h in sorted(account.holdings.items())},
        },
        "open_sells": sells,
        "open_buys": buys,
        "buy_conditions": pending.get("buy_conditions") or [],
        "broker_conditions": conditions,
        "notices": notices,
    }
