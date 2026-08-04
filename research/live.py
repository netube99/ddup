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

import json
import logging
from dataclasses import dataclass, field

import pandas as pd

from btcore import corporate, database, types
from btcore.engine import Engine, bars_to_dict, value_account
from btcore.match.core import apply_partial_sell, is_valid_price

logger = logging.getLogger(__name__)

LIVE_RUN_ID = 1  # 衍生表统一挂在 run_id=1（每个账本库一个逻辑 run）

LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    symbol       TEXT NOT NULL DEFAULT '',
    side         TEXT NOT NULL,
    price        REAL NOT NULL DEFAULT 0,
    shares       INTEGER NOT NULL DEFAULT 0,
    commission   REAL NOT NULL DEFAULT 0,
    stamp_tax    REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_fills_date ON ledger_fills(date);

CREATE TABLE IF NOT EXISTS ledger_holdings (
    symbol       TEXT PRIMARY KEY,
    entry_date   TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    shares       INTEGER NOT NULL,
    cost         REAL NOT NULL,
    last_price   REAL NOT NULL DEFAULT 0,
    holding_days INTEGER NOT NULL DEFAULT 0
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
        self.conn.executescript(LEDGER_SCHEMA_SQL)
        self.conn.commit()

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
        if self.is_initialized():
            raise ValueError("账本已初始化（initial_capital 已存在）")
        if initial_cash < 0 or initial_capital <= 0:
            raise ValueError(
                f"initial_cash/initial_capital 非法: {initial_cash}, {initial_capital}"
            )
        self.set_meta("start_date", start_date)
        self.set_meta("initial_cash", repr(float(initial_cash)))
        self.set_meta("initial_capital", repr(float(initial_capital)))
        self.conn.commit()

    @property
    def start_date(self) -> str:
        v = self.get_meta("start_date")
        if v is None:
            raise ValueError("账本未初始化")
        return v

    @property
    def initial_cash(self) -> float:
        return float(self.get_meta("initial_cash") or "0")

    @property
    def initial_capital(self) -> float:
        return float(self.get_meta("initial_capital") or "0")

    # ── 成交 ──

    def append_fill(self, date: str, symbol: str, side: str, *,
                    price: float = 0.0, shares: int = 0,
                    commission: float = 0.0, stamp_tax: float = 0.0,
                    transfer_fee: float = 0.0, reason: str = "",
                    created_at: str):
        side = side.upper()
        if side not in _FILL_SIDES:
            raise ValueError(f"非法 fill side: {side!r}（允许 {sorted(_FILL_SIDES)}）")
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
            (date, symbol, side, float(price), int(shares),
             float(commission), float(stamp_tax), float(transfer_fee),
             reason, created_at),
        )

    def append_fills_idempotent(self, fills: list[dict],
                                created_at: str) -> tuple[int, int]:
        """幂等追加：完全重复的成交跳过（agent 重跑同一 statement 不双重入账）。

        重复判定按 (date, symbol, side, price, shares, commission,
        stamp_tax, transfer_fee, reason) 全字段归一化后精确匹配。
        返回 (appended, skipped)。
        """
        existing = {
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])
            for r in self.conn.execute(
                "SELECT date, symbol, side, price, shares, commission,"
                " stamp_tax, transfer_fee, reason FROM ledger_fills"
            ).fetchall()
        }
        appended = skipped = 0
        for f in fills:
            key = (str(f.get("date") or ""), f["symbol"], f["side"],
                   round(float(f["price"]), 6), int(f["shares"]),
                   round(float(f.get("commission") or 0.0), 6),
                   round(float(f.get("stamp_tax") or 0.0), 6),
                   round(float(f.get("transfer_fee") or 0.0), 6),
                   str(f.get("reason") or "MANUAL"))
            if key in existing:
                skipped += 1
                continue
            self.append_fill(key[0], key[1], key[2], price=key[3],
                             shares=key[4], commission=key[5],
                             stamp_tax=key[6], transfer_fee=key[7],
                             reason=key[8], created_at=created_at)
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
        with self.conn:
            self.conn.execute("DELETE FROM trade_log WHERE run_id = ?", (LIVE_RUN_ID,))
            self.conn.execute("DELETE FROM account_daily WHERE run_id = ?", (LIVE_RUN_ID,))
            if not self.conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (LIVE_RUN_ID,)
            ).fetchone():
                database.write_run(
                    self.conn, created_at=pd.Timestamp.now().isoformat(),
                    strategy="live", start_date=self.start_date,
                    end_date=daily_rows[-1]["date"] if daily_rows else self.start_date,
                    initial_capital=initial_capital,
                    config_json=json.dumps({"ledger": self.path}, ensure_ascii=False),
                    status="live",
                )
            for r in daily_rows:
                database.write_daily(
                    self.conn, LIVE_RUN_ID, r["date"], r["cash"], r["total_value"],
                    r["daily_pnl"], r["cumulative_pnl"], initial_capital,
                    r["n_holdings"],
                )
            for t in trade_rows:
                database.write_trade(self.conn, LIVE_RUN_ID, t)
            database.write_holdings(self.conn, account)
            # init_backtest_db 每次打开清空 holdings 瞬态表；ledger_holdings
            # 是跨会话持久的当前持仓快照（status 数据源）
            self.conn.execute("DELETE FROM ledger_holdings")
            for h in account.holdings.values():
                self.conn.execute(
                    "INSERT INTO ledger_holdings (symbol, entry_date,"
                    " entry_price, shares, cost, last_price, holding_days)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (h.symbol, h.entry_date, h.entry_price, h.shares,
                     h.cost, h.last_price, h.holding_days),
                )


# ── 成交应用（账本 → 账户状态机）──


def apply_fill(account: types.Account, fill: dict, today: str) -> types.Trade | None:
    """把一条账本成交应用到账户。返回 Trade（BUY/SELL）或 None（ADJUST）。

    与撮合层的差异：价格/费用用真实成交值，无滑点、无费用模型、
    无涨跌停/成交量校验（券商已成交即是事实）。
    """
    side = fill["side"]
    if side == "ADJUST":
        account.cash += fill["price"]  # ADJUST 的金额存在 price 字段
        return None

    symbol = fill["symbol"]
    shares = fill["shares"]
    turnover = fill["price"] * shares

    if side == "BUY":
        net = -(turnover + fill["commission"] + fill["transfer_fee"])
        account.cash += net
        holding = account.holdings.get(symbol)
        if holding is not None:
            # 加仓：加权均价，保留原 entry_date（红利税持股期口径偏保守）
            total_shares = holding.shares + shares
            holding.entry_price = (
                holding.entry_price * holding.shares + fill["price"] * shares
            ) / total_shares
            holding.cost += turnover
            holding.shares = total_shares
            holding.locked = True  # 当日有买入即锁定全仓（A股 T+1 同口径）
        else:
            account.holdings[symbol] = types.Holding(
                symbol=symbol, shares=shares, entry_date=today,
                entry_price=fill["price"], cost=turnover,
                last_price=fill["price"], locked=True,
            )
        return types.Trade(
            date=today, symbol=symbol, side="BUY", trigger=fill["reason"],
            price=fill["price"], shares=shares, turnover=turnover,
            commission=fill["commission"], stamp_tax=0.0,
            transfer_fee=fill["transfer_fee"], slippage_amount=0.0,
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
    net = turnover - fill["commission"] - fill["stamp_tax"] - fill["transfer_fee"]
    account.cash += net
    if shares == holding.shares:
        del account.holdings[symbol]
    else:
        apply_partial_sell(holding, shares)
    return types.Trade(
        date=today, symbol=symbol, side="SELL", trigger=fill["reason"],
        price=fill["price"], shares=shares, turnover=turnover,
        commission=fill["commission"], stamp_tax=fill["stamp_tax"],
        transfer_fee=fill["transfer_fee"], slippage_amount=0.0,
        net_amount=net, reason=fill["reason"],
    )


def seed_opening(account: types.Account, store: LedgerStore, provider,
                 init_date: str):
    """建仓持仓以 OPENING 条目入账：种子持仓（真实 entry_date/entry_price）。

    holding_days 种子值 = entry 到 init 之间引擎会经历的结算次数
    （交易日历上 (entry, init) 的开区间长度），使回放首日 compute_pending
    递增后与连续运行的引擎口径一致。
    """
    calendar = provider.get_calendar("20000101", init_date)
    init_idx = {d: i for i, d in enumerate(calendar)}
    for f in store.fills(side_filter=frozenset({"OPENING"})):
        symbol, shares = f["symbol"], f["shares"]
        entry = f["date"]
        if entry not in init_idx:
            logger.warning("OPENING %s entry_date %s 非交易日，口径近似", symbol, entry)
        seed_days = max(0, init_idx.get(init_date, 0) - init_idx.get(entry, 0))
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

    # 与 run() 首日行为对齐：首日前一交易日先算一次 pending（策略状态播种）
    prev_day = provider.prev_trading_day(calendar[0])
    if prev_day and run_decisions:
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
    account = light_replay(store, provider, date)
    derived = {s: h.shares for s, h in account.holdings.items()}
    diffs = {}
    for symbol in sorted(set(derived) | set(actual_holdings)):
        d, a = derived.get(symbol, 0), int(actual_holdings.get(symbol, 0))
        if d != a:
            diffs[symbol] = (d, a)
    delta = actual_cash - account.cash
    return ReconReport(
        date=date, ok=not diffs,
        holding_diffs=diffs,
        cash_derived=account.cash, cash_actual=actual_cash, cash_delta=delta,
        derived_holdings=derived, actual_holdings=dict(actual_holdings),
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


def next_trading_day(provider, date: str) -> str | None:
    end = (pd.Timestamp(date) + pd.Timedelta(days=30)).strftime("%Y%m%d")
    cal = provider.get_calendar(date, end)
    for d in cal:
        if d > date:
            return d
    return None


def build_op_sheet(engine: Engine, provider, today: str) -> dict:
    """次日操作单：开盘手动单 + 券商条件单 + 提示。全部来自末日决策输出。"""
    pending = engine.pending_actions or {}
    account = engine.account
    next_day = next_trading_day(provider, today)

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
    for symbol in sorted(account.holdings.keys()):
        if day_bars is None or symbol not in day_bars.index:
            notices.append({"type": "suspended", "symbol": symbol,
                            "message": f"{symbol} 今日无行情（停牌？），明日不可操作"})
    if next_day:
        divs = provider.get_dividends_on_date(next_day) or {}
        for symbol in sorted(account.holdings.keys()):
            if symbol in divs:
                notices.append({"type": "ex_div", "symbol": symbol,
                                "message": f"{symbol} 明日除权除息: {divs[symbol]}"})
    locked = [s for s, h in account.holdings.items() if h.locked]
    for s in sorted(locked):
        notices.append({"type": "t1_lock", "symbol": s,
                        "message": f"{s} 今日买入，明日可卖（T+1 已解锁于明日）"})

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
