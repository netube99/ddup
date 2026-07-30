import copy
import json
import logging

import numpy as np
import pandas as pd

from btcore import corporate, database, limits, match, risk, stats, types
from btcore.costs import make_costs_fn
from btcore.factors import plan as factor_plan
from btcore.filters import filter_required_columns
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage

logger = logging.getLogger(__name__)

# 数据契约必需列（docs/backend_guide.md）——缺列直接报错，不走语义不精确的兜底
# amount 不在其中：引擎内部不消费，仅为策略 select() 提供，策略通过 REQUIRED_FIELDS 声明
REQUIRED_BAR_COLUMNS = (
    "open", "high", "low", "close",
    "vol",          # 单位: 手 (1 手 = 100 股)
    "adj_factor",
    "pre_close",    # 交易所除权调整口径: 除权日 = (前裸收盘 - 现金分红) / (1 + 送转比例)
    "up_limit", "down_limit",
)

def required_bar_columns(strategy, fplan: dict | None = None) -> list[str]:
    """静态推导主面板请求列（preload 列裁剪）。

    来源: REQUIRED_BAR_COLUMNS ∪ strategy.REQUIRED_FIELDS ∪
    FILTER_RULES 显式依赖 ∪ 因子闭包基础列（fplan.main_columns）；
    派生列替换为派生基础列，伪列与物化因子列不请求。
    策略在 select() 里命令式访问的列必须声明进 REQUIRED_FIELDS。
    """
    cols = set(REQUIRED_BAR_COLUMNS)
    cols |= set(getattr(strategy, "REQUIRED_FIELDS", None) or [])
    cols |= filter_required_columns(
        getattr(strategy, "FILTER_RULES", None) or {}
    )
    if fplan:
        cols |= fplan["main_columns"]
    return factor_plan.expand_columns(cols)


class Engine:
    def __init__(self, strategy, provider: DataProvider,
                 initial_capital: float | None = None,
                 db_path: str | None = None, max_positions: int | None = None,
                 debug: bool = False):
        self.strategy = strategy
        self.provider = provider
        self._debug = debug

        config = strategy.config
        self.initial_capital = float(
            initial_capital if initial_capital is not None
            else config.get("initial_capital", 1_000_000)
        )
        self.db_path = db_path or ":memory:"
        self.max_positions = int(
            max_positions if max_positions is not None
            else config.get("max_positions", 20)
        )

        slippage_ticks = config.get("slippage_ticks", 2)
        condition_slippage_ticks = config.get("condition_slippage_ticks")
        if condition_slippage_ticks is not None and (
                not isinstance(condition_slippage_ticks, int)
                or isinstance(condition_slippage_ticks, bool)
                or condition_slippage_ticks < 0):
            raise ValueError(
                "condition_slippage_ticks 必须是非负整数或 None: "
                f"{condition_slippage_ticks!r}"
            )
        self.condition_slippage_ticks = condition_slippage_ticks
        self.costs_fn = make_costs_fn(config)
        bench_code = config.get("benchmark")
        if bench_code is None:
            # 未显式设置 benchmark 时，从 index_universe 首个指数自动推导；
            # 多指数或未配置 index_universe 则回退至 CSI 300。
            idx_universe = (getattr(self.strategy, "FILTER_RULES", None) or {}).get(
                "index_universe", []
            )
            bench_code = idx_universe[0] if len(idx_universe) == 1 else "000300.SH"
        # 空字符串 / None 表示无基准（即使后端提供 get_benchmark_bars 也不取）
        self.benchmark: str | None = bench_code or None
        if self.provider is not None:
            self.provider.benchmark = self.benchmark
        self.quiet_skips = bool(config.get("quiet_skips", False))
        self.order_volume_ratio = config.get("order_volume_ratio")
        execution_price = config.get("execution_price", "open")
        if execution_price not in ("open", "close"):
            raise ValueError(
                f"execution_price 只支持 'open'/'close': {execution_price!r}"
            )
        self.account = types.Account(
            cash=self.initial_capital,
            initial_capital=self.initial_capital,
            slippage_ticks=slippage_ticks,
            order_volume_ratio=(
                float(self.order_volume_ratio)
                if self.order_volume_ratio is not None else None
            ),
            execution_price=execution_price,
        )
        self.account.total_value = self.initial_capital
        self._risk_rules = risk.validate_risk_rules(config.get("risk_rules"))
        if ("max_industry_pct" in self._risk_rules
                and not callable(getattr(provider.backend,
                                         "get_stock_industries", None))):
            raise ValueError(
                "risk_rules.max_industry_pct 需要 backend 提供 "
                "get_stock_industries 方法"
            )
        self._breaker = risk.DrawdownBreaker(
            self._risk_rules.get("max_drawdown"),
            self._risk_rules.get("cooldown_days", 1),
        )
        # 当前 pending 批次是否风控强平（卖出 trigger 标 RISK）
        self._risk_forced = False
        self.pending_actions = {"buy": [], "sell": []}
        self._debug = debug
        # run() 里由 write_run 赋真实 run_id；直接调 step() 的测试用 0
        self.run_id = 0
        self.bars_df: pd.DataFrame | None = None
        self.bars_by_date: dict = {}
        self._saved_cash: float | None = None
        self._saved_holdings: dict | None = None

    def run(self, start: str, end: str) -> dict:
        # 重置 run_id: 同一实例重复 run 时, 若本次在 write_run 前抛异常,
        # except 分支不能拿到上一次 run 的 id 去误标 failed
        self.run_id = 0
        conn = database.init_backtest_db(self.db_path)
        try:
            # 熔断状态仅活在单次 run 内
            self._breaker = risk.DrawdownBreaker(
                self._risk_rules.get("max_drawdown"),
                self._risk_rules.get("cooldown_days", 1),
            )
            calendar = self.provider.get_calendar(start, end)
            if not calendar:
                raise ValueError("日历为空")

            factor_symbols = self.strategy.get_factor_universe(self.provider, start, end)
            trade_symbols = self.strategy.get_universe(self.provider, start, end)
            # factor_universe 未配置时 factor_symbols 为 None，沿用 trade_symbols
            load_symbols = factor_symbols if factor_symbols is not None else trade_symbols
            fplan = self._build_factor_plan()
            warmup_days = fplan["main_days"] if fplan else 365
            preload_start = (
                pd.Timestamp(calendar[0]) - pd.Timedelta(days=warmup_days)
            ).strftime("%Y%m%d")
            bars_df = self.provider.get_engine_bars(
                load_symbols, calendar[-1],
                lookback_start=preload_start,
                columns=required_bar_columns(self.strategy, fplan),
            )
            bars_df.sort_index(inplace=True)
            _validate_required_columns(bars_df)
            _ensure_derived_fields(bars_df)
            if fplan:
                # 因子物化：广度面板（全市场×短窗口，投影后释放）+ 主面板
                logger.debug("factor warmup rows: %s", fplan["windows"])
                breadth_df = self._preload_breadth(fplan, calendar)
                self._attach_pseudo_columns(bars_df, fplan["needs"], "main")
                factor_plan.materialize(
                    bars_df, breadth_df, fplan, self.strategy.FACTOR_NODES
                )
                # 物化后验证
                issues = factor_plan.validate_materialization(bars_df, fplan)
                for issue in issues:
                    logger.warning("[因子验证] %s", issue["message"])
            # 若 factor_universe 比 trading universe 更宽，裁切到交易域
            if factor_symbols is not None and trade_symbols is not None:
                trade_set = set(trade_symbols)
                mask = bars_df.index.get_level_values("symbol").isin(trade_set)
                bars_df = bars_df[mask]
                if bars_df.empty:
                    raise ValueError(
                        "factor_universe 裁切后无数据：交易域符号均不在因子计算域内"
                    )
            self.bars_df = bars_df
            self.bars_by_date = {
                d: group.droplevel("trade_date")
                for d, group in bars_df.groupby(level="trade_date", sort=False)
            }
            self.provider.attach_bars(bars_df)
            self.strategy.on_start(self.provider, calendar[0], end_date=end)

            # runs 行独立事务提交: 后续 step 回滚不会把它带走,
            # 崩溃时才能把状态改写成 failed
            with conn:
                self.run_id = database.write_run(
                    conn,
                    created_at=pd.Timestamp.now().isoformat(),
                    strategy=self.strategy.__class__.__name__,
                    start_date=start,
                    end_date=end,
                    initial_capital=self.initial_capital,
                    config_json=json.dumps(
                        self.strategy.config, ensure_ascii=False, default=str
                    ),
                    status="running",
                )

            prev_day = self.provider._prev_trading_day(calendar[0])
            if prev_day:
                self._compute_pending(prev_day)

            for today in calendar:
                day_bars = self.bars_by_date.get(today)
                if day_bars is None:
                    logger.warning("[%s] 无行情数据, 跳过", today)
                    continue
                self.step(today, day_bars, conn)

            account_daily_df = pd.read_sql_query(
                "SELECT * FROM account_daily WHERE run_id = ? ORDER BY date",
                conn, params=(self.run_id,),
            )
            trade_log_df = pd.read_sql_query(
                "SELECT * FROM trade_log WHERE run_id = ? ORDER BY id",
                conn, params=(self.run_id,),
            )
            bench_fn = getattr(self.provider.backend, "get_benchmark_bars", None)
            benchmark = (
                bench_fn(self.benchmark, start, end)
                if bench_fn and self.benchmark else None
            )
            stats_result = stats.calculate_statistics(
                account_daily_df, trade_log_df,
                benchmark,
                holdings=dict(self.account.holdings),
            )
            # 提取基准净值序列，供报告使用
            benchmark_nav = None
            if benchmark is not None and not benchmark.empty:
                bm = benchmark.copy()
                if "date" in bm.columns:
                    bm = bm.set_index("date")
                hfq_col = "hfq_close" if "hfq_close" in bm.columns else "close"
                if hfq_col in bm.columns and len(bm) > 0:
                    first = float(bm[hfq_col].iloc[0])
                    if first > 0:
                        benchmark_nav = (bm[hfq_col] / first).tolist()
                        benchmark_nav = [float(v) for v in benchmark_nav]
            with conn:
                database.write_run_stats(conn, self.run_id, stats_result)
                database.update_run_status(conn, self.run_id, "completed")
            return {
                "account_daily": account_daily_df,
                "trade_log": trade_log_df,
                "statistics": stats_result,
                "benchmark_nav": benchmark_nav,
                "benchmark_code": self.benchmark,
            }
        except Exception:
            # 崩溃不留 "running" 假象；run_id=0 说明还没落库，无需标记
            if self.run_id:
                with conn:
                    database.update_run_status(conn, self.run_id, "failed")
            raise
        finally:
            conn.close()

    def _build_factor_plan(self) -> dict | None:
        """由策略 FACTOR_SPECS/FACTOR_NODES 构建因子供给计划（无因子则 None）。"""
        specs = getattr(self.strategy, "FACTOR_SPECS", None) or []
        if not specs:
            return None
        nodes = getattr(self.strategy, "FACTOR_NODES", None)
        if not nodes:
            raise ValueError(
                "FACTOR_SPECS 需要 FACTOR_NODES（因子闭包）——"
                "请经 btcore.strategy_loader 加载策略，或自行设置该属性"
            )
        return factor_plan.build_factor_plan(
            nodes, [s["name"] for s in specs]
        )

    def _preload_breadth(self, fplan: dict, calendar: list[str]):
        """广度面板：全市场 × 短窗口 × 窄列；物化投影后由调用方释放。"""
        if not fplan["needs"]["market"]:
            return None
        start = (
            pd.Timestamp(calendar[0])
            - pd.Timedelta(days=fplan["breadth_days"])
        ).strftime("%Y%m%d")
        breadth_df = self.provider.get_engine_bars(
            None, calendar[-1],
            lookback_start=start,
            columns=factor_plan.expand_columns(fplan["breadth_columns"]),
        )
        breadth_df.sort_index(inplace=True)
        _ensure_derived_fields(breadth_df)
        self._attach_pseudo_columns(breadth_df, fplan["needs"], "breadth")
        return breadth_df

    def _attach_pseudo_columns(self, df: pd.DataFrame, needs: dict, panel: str):
        """按需附着伪列：industry（backend 鸭子类型）/ log_mktcap / idx_ret。"""
        ensure_pseudo_columns(
            df, needs, panel,
            backend=self.provider.backend,
            benchmark=self.benchmark,
            derive_idx_ret=self._derive_idx_ret,
        )

    def _derive_idx_ret(self, df: pd.DataFrame) -> pd.Series:
        """指数参照序列（benchmark hfq_close 的日收益）按日期广播进面板。"""
        bench_fn = getattr(self.provider.backend, "get_benchmark_bars", None)
        if not (callable(bench_fn) and self.benchmark):
            raise ValueError(
                "因子引用 idx_ret 需要 config['benchmark'] 且 backend "
                "提供 get_benchmark_bars"
            )
        dates = df.index.get_level_values("trade_date")
        bench = bench_fn(self.benchmark, dates.min(), dates.max())
        if bench is None or bench.empty:
            raise ValueError(f"基准 {self.benchmark} 无数据, 无法派生 idx_ret")
        ret = bench["hfq_close"].pct_change()
        ret.index = pd.Index(pd.to_datetime(ret.index).strftime("%Y%m%d"))
        return dates.map(ret)

    def _derive_idx_ret(self, df: pd.DataFrame) -> pd.Series:
        """指数参照序列（benchmark hfq_close 的日收益）按日期广播进面板。"""
        bench_fn = getattr(self.provider.backend, "get_benchmark_bars", None)
        if not (callable(bench_fn) and self.benchmark):
            raise ValueError(
                "因子引用 idx_ret 需要 config['benchmark'] 且 backend "
                "提供 get_benchmark_bars"
            )
        dates = df.index.get_level_values("trade_date")
        bench = bench_fn(self.benchmark, dates.min(), dates.max())
        if bench is None or bench.empty:
            raise ValueError(f"基准 {self.benchmark} 无数据, 无法派生 idx_ret")
        ret = bench["hfq_close"].pct_change()
        ret.index = pd.Index(pd.to_datetime(ret.index).strftime("%Y%m%d"))
        return dates.map(ret)

    def step(self, today: str, day_bars: pd.DataFrame, conn):
        bars_dict = _bars_to_dict(day_bars, today)

        self._save_state()

        try:
            with conn:
                corporate_log = []
                corporate.adjust(self.account, today, bars_dict,
                                 self.provider, corporate_log)

                targets = self.pending_actions.get("target_value") or {}
                if targets:
                    manual_sell_trades = []
                    manual_buy_trades = match.manual.rebalance_to_targets(
                        self.account, bars_dict, targets,
                        self.max_positions,
                        limits.get_limit_prices, self.costs_fn, apply_slippage,
                        quiet=self.quiet_skips,
                    )
                else:
                    manual_sell_trades = match.manual.manual_sell(
                        self.account, bars_dict,
                        self.pending_actions.get("sell", []),
                        limits.get_limit_prices, self.costs_fn, apply_slippage,
                        shares_map=self.pending_actions.get("sell_shares"),
                        trigger=("RISK" if self._risk_forced else "MANUAL"),
                        quiet=self.quiet_skips,
                    )

                    manual_buy_trades = match.manual.manual_buy(
                        self.account, bars_dict,
                        self.pending_actions.get("buy", []),
                        self.max_positions,
                        limits.get_limit_prices, self.costs_fn, apply_slippage,
                        weights_map=self.pending_actions.get("buy_weights"),
                        quiet=self.quiet_skips,
                    )

                condition_trades = match.conditions.exit_conditions(
                    self.account, bars_dict,
                    limits.get_limit_prices, self.costs_fn, apply_slippage,
                    quiet=self.quiet_skips,
                    slip_ticks=self.condition_slippage_ticks,
                )

                # 条件买单最后执行: 吃到当日卖出释放的现金
                entry_trades = match.conditions.entry_conditions(
                    self.account, bars_dict,
                    self.pending_actions.get("buy_conditions", []),
                    self.max_positions,
                    limits.get_limit_prices, self.costs_fn, apply_slippage,
                    quiet=self.quiet_skips,
                    slip_ticks=self.condition_slippage_ticks,
                )

                all_trades = (manual_sell_trades + manual_buy_trades
                              + condition_trades + entry_trades)
                logger.info(
                    "[%s] 当日成交: sell=%d buy=%d cond=%d entry=%d total=%d",
                    today, len(manual_sell_trades), len(manual_buy_trades),
                    len(condition_trades), len(entry_trades), len(all_trades),
                )
                self._settle(today, bars_dict, all_trades, corporate_log, conn)

                self._compute_pending(today, bars_dict, all_trades)
                if self._debug:
                    self._write_debug_snapshot(conn, today, self.pending_actions, day_bars)
        except Exception:
            self._restore_state()
            raise

    def _settle(self, today: str, bars_dict: dict, trades: list,
                corporate_log: list, conn):
        _warn = logger.debug if self.quiet_skips else logger.warning
        total_value = self.account.cash
        for symbol, holding in self.account.holdings.items():
            bar = bars_dict.get(symbol)
            close = bar.get("close") if bar is not None else None
            if match.core.is_valid_price(close):
                holding.last_price = close
            elif bar is not None:
                _warn("[%s] %s 收盘价非法 (%s), 沿用 last_price=%s",
                               today, symbol, close, holding.last_price)
            total_value += holding.shares * holding.last_price

        self.account.total_value = total_value

        prev_cum = self.account.cumulative_pnl
        self.account.cumulative_pnl = total_value - self.initial_capital
        self.account.daily_pnl = self.account.cumulative_pnl - prev_cum

        database.write_daily(
            conn, self.run_id, today, self.account.cash, total_value,
            self.account.daily_pnl, self.account.cumulative_pnl,
            self.initial_capital, len(self.account.holdings),
        )
        database.write_holdings(conn, self.account)
        for trade in trades:
            database.write_trade(conn, self.run_id, trade)

        for event in corporate_log:
            if event["type"] == "cash_div":
                database.write_trade(conn, self.run_id, types.Trade(
                    date=today, symbol=event["symbol"], side="DIV",
                    trigger="CORPORATE", price=0.0, shares=0,
                    turnover=0.0, commission=0.0, stamp_tax=0.0,
                    transfer_fee=0.0, slippage_amount=0.0,
                    net_amount=event["net"], reason="cash_div",
                ))

    def _compute_pending(self, calc_date: str, bars_dict: dict | None = None,
                         trades: list | None = None):
        self.provider._as_of_date = calc_date

        for holding in self.account.holdings.values():
            holding.holding_days += 1
            holding.locked = False

        if bars_dict is None:
            day_bars_view = self.bars_by_date.get(calc_date)
            if day_bars_view is None:
                return
            bars_dict = _bars_to_dict(day_bars_view, calc_date)

        fills = list(trades) if trades else []
        # on_fills 是可选 hook（鸭子类型策略可能没定义），须在 select 之前调用
        on_fills = getattr(self.strategy, "on_fills", None)
        if callable(on_fills):
            on_fills(fills, self.provider)

        snapshot = types.Snapshot(
            cash=self.account.cash,
            # 深拷贝: 策略在 select 里改 snapshot 的 Holding 不能污染引擎状态
            holdings=copy.deepcopy(self.account.holdings),
            trades=fills,
            total_value=self.account.total_value,
        )
        # on_tick 是可选钩子：每日运行（绕过 schedule 包装器），在 select 之前更新策略内部状态
        on_tick = getattr(self.strategy, "on_tick", None)
        on_tick_result = None
        if callable(on_tick):
            on_tick_result = on_tick(bars_dict, snapshot, self.provider)

        actions = self.strategy.select(bars_dict, snapshot, self.provider)

        # 合并 on_tick 返回的 buy_conditions（不受 schedule 限制）
        if on_tick_result is not None and on_tick_result.get("buy_conditions"):
            existing_conds = actions.setdefault("buy_conditions", [])
            existing_conds.extend(on_tick_result["buy_conditions"])

        # 组合级风控: 熔断态强制只卖不买（次日强平, trigger=RISK）;
        # 否则按 risk_rules 裁剪买侧（卖侧永不干预）
        self._breaker.update(self.account.total_value)
        if self._breaker.tick():
            if self.account.holdings:
                logger.warning("[%s] 风控态: 强制清仓 %d 只持仓",
                               calc_date, len(self.account.holdings))
            actions = {"buy": [], "sell": list(self.account.holdings)}
            self._risk_forced = True
        else:
            actions = risk.apply_risk_rules(
                actions, self.account, self.account.total_value,
                self._risk_rules,
                industry_fn=getattr(self.provider.backend,
                                    "get_stock_industries", None),
                max_positions=self.max_positions,
            )
            self._risk_forced = False

        buy = set(actions.get("buy", []))
        sell = set(actions.get("sell", []))
        if buy & sell:
            raise ValueError(f"同日买卖冲突: {buy & sell}")
        target_value = actions.get("target_value") or {}
        if target_value and (buy or sell):
            raise ValueError("target_value 与 buy/sell 名单互斥, 同日只能用一种")

        sell_shares = actions.get("sell_shares") or {}
        if not isinstance(sell_shares, dict):
            raise ValueError("sell_shares 必须是 {symbol: 股数} 的 dict")
        for symbol, shares in sell_shares.items():
            if symbol not in sell:
                raise ValueError(
                    f"sell_shares 的 {symbol} 不在 sell 名单里"
                )
            if (not isinstance(shares, int) or isinstance(shares, bool)
                    or shares <= 0):
                raise ValueError(
                    f"sell_shares[{symbol}] 必须是正整数股数: {shares!r}"
                )

        buy_weights = actions.get("buy_weights")
        if buy_weights is not None:
            if not isinstance(buy_weights, dict):
                raise ValueError("buy_weights 必须是 {symbol: 权重} 的 dict")
            if set(buy_weights) != buy:
                raise ValueError(
                    f"buy_weights 的键必须与 buy 名单一致: "
                    f"多 {set(buy_weights) - buy}, 缺 {buy - set(buy_weights)}"
                )
            total_w = 0.0
            for symbol, w in buy_weights.items():
                if not is_valid_positive(w) or w > 1:
                    raise ValueError(
                        f"buy_weights[{symbol}] 必须 ∈ (0,1]: {w!r}"
                    )
                total_w += w
            if total_w > 1.0 + 1e-10:
                raise ValueError(f"buy_weights 权重之和必须 ≤ 1: {total_w}")

        buy_conds = actions.get("buy_conditions") or []
        if not isinstance(buy_conds, list):
            raise ValueError("buy_conditions 必须是订单 dict 的 list")
        for i, order in enumerate(buy_conds):
            if not isinstance(order, dict):
                raise ValueError(f"buy_conditions[{i}] 必须是 dict: {order!r}")
            missing = {"symbol", "type", "price"} - set(order)
            if missing:
                raise ValueError(f"buy_conditions[{i}] 缺必填键: {missing}")
            if not is_valid_positive(order["price"]):
                raise ValueError(
                    f"buy_conditions[{i}].price 必须是正数: {order['price']!r}"
                )
            has_value = order.get("value") is not None
            has_shares = order.get("shares") is not None
            if has_value == has_shares:
                raise ValueError(
                    f"buy_conditions[{i}] 必须在 value/shares 中恰填一个"
                )
            sizing = order.get("value") if has_value else order.get("shares")
            if not is_valid_positive(sizing):
                raise ValueError(
                    f"buy_conditions[{i}] value/shares 必须是正数: {sizing!r}"
                )
        bc_symbols = {o["symbol"] for o in buy_conds}
        if bc_symbols & sell:
            raise ValueError(f"同日卖出与条件买入冲突: {bc_symbols & sell}")
        if bc_symbols & buy:
            raise ValueError(f"buy 名单与条件买入重复: {bc_symbols & buy}")
        if buy_conds and target_value:
            raise ValueError("target_value 与 buy_conditions 互斥, 同日只能用一种")
        match.conditions.validate_buy_condition_types(buy_conds)

        self.pending_actions = actions

        for symbol, holding in self.account.holdings.items():
            bar = bars_dict.get(symbol, {})
            entry_price = holding.entry_price
            holding_days = holding.holding_days
            holding.conditions = self.strategy.calc_conditions(
                symbol, entry_price, bar, holding_days
            )
            match.conditions.validate_condition_types(holding.conditions)

    def _save_state(self):
        self._saved_cash = self.account.cash
        self._saved_holdings = copy.deepcopy(self.account.holdings)

    def _restore_state(self):
        if self._saved_cash is not None:
            self.account.cash = self._saved_cash
        if self._saved_holdings is not None:
            self.account.holdings = self._saved_holdings

    def _write_debug_snapshot(self, conn, today, pending_actions, day_bars):
        """收集每日调试快照并写入 debug_snapshots 表。"""
        bars = day_bars.to_dict("index")

        # holdings 详情
        holdings_detail = {}
        for symbol, h in self.account.holdings.items():
            holdings_detail[symbol] = {
                "shares": h.shares,
                "entry_price": h.entry_price,
                "entry_date": h.entry_date,
                "holding_days": h.holding_days,
            }

        # 涉及 symbol: holdings + buy 目标
        relevant_symbols = set(holdings_detail) | set(pending_actions.get("buy", []))
        bc_symbols = {c["symbol"] for c in pending_actions.get("buy_conditions", [])}
        relevant_symbols |= bc_symbols

        bars_subset = {}
        for sym in relevant_symbols:
            bar = bars.get(sym)
            if bar is not None:
                bars_subset[sym] = {k: v for k, v in bar.items()
                                    if not k.startswith("_")}

        snapshot = {
            "date": today,
            "account": {
                "cash": self.account.cash,
                "total_value": self.account.total_value,
                "n_holdings": len(self.account.holdings),
            },
            "pending": {
                "buy": pending_actions.get("buy", []),
                "sell": pending_actions.get("sell", []),
                "buy_conditions": pending_actions.get("buy_conditions", []),
            },
            "risk_forced": self._risk_forced,
            "holdings_detail": holdings_detail,
            "bars_subset": bars_subset,
        }
        database.write_debug_snapshot(conn, self.run_id, today, snapshot)


def is_valid_positive(v) -> bool:
    """正数校验: 拒绝 bool / None / NaN / 非正。"""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v > 0)


def _validate_required_columns(bars_df: pd.DataFrame) -> None:
    """契约强校验：缺必需列直接失败。

    pre_close / up_limit / down_limit 曾允许引擎兜底推算，但兜底语义不精确
    （除权日涨跌停一阶错误、pct_chg 假暴跌），故改为数据契约强制提供。
    """
    missing = [c for c in REQUIRED_BAR_COLUMNS if c not in bars_df.columns]
    if missing:
        raise ValueError(
            f"bars 缺必需列: {missing}, 数据契约见 docs/backend_guide.md"
        )


def _ensure_derived_fields(bars_df: pd.DataFrame) -> None:
    """补齐可由必需列精确派生的字段（基础列存在时才派生）。

    *_hfq = 裸价 × adj_factor（hfq 定义）；pct_chg 由 pre_close（交易所
    除权调整口径，必需列）派生。这两个派生都是精确的，无语义损耗。
    广度面板按列裁剪后可能只带部分基础列，缺基础列的派生直接跳过。
    """
    if "adj_factor" in bars_df.columns:
        for src, dst in [("open", "open_hfq"), ("high", "high_hfq"),
                         ("low", "low_hfq"), ("close", "close_hfq")]:
            if dst not in bars_df.columns and src in bars_df.columns:
                bars_df[dst] = bars_df[src] * bars_df["adj_factor"]

    if ("pct_chg" not in bars_df.columns
            and {"close", "pre_close"} <= set(bars_df.columns)):
        pre = bars_df["pre_close"]
        bars_df["pct_chg"] = (bars_df["close"] - pre) / pre.replace(0, pd.NA)


def ensure_pseudo_columns(
    df: pd.DataFrame,
    needs: dict,
    panel: str,
    *,
    backend,
    benchmark: str | None = None,
    derive_idx_ret=None,
) -> None:
    """按需附着伪列：industry / log_mktcap / idx_ret（原地写列）。

    引擎与 scripts/factor_eval.py 共用，backend 为鸭子类型（只需有对应方法即可）。
    idx_ret 派生需要 benchmark 和 derive_idx_ret 回调（引擎内联供给）。
    """
    if needs.get(f"industry_{panel}"):
        fn = getattr(backend, "get_stock_industries", None)
        if not callable(fn):
            raise ValueError(
                "因子引用 industry 分组需要 backend 提供 get_stock_industries"
            )
        symbols = df.index.get_level_values("symbol").unique().tolist()
        mapping = fn(symbols)
        df["industry"] = df.index.get_level_values("symbol").map(mapping)
    if needs.get(f"mktcap_{panel}"):
        total_mv = df["total_mv"]
        df["log_mktcap"] = np.log(total_mv.where(total_mv > 0))
    if needs.get("index"):
        if derive_idx_ret is None or not benchmark:
            raise ValueError(
                "因子引用 idx_ret 需要 benchmark 且 derive_idx_ret 回调不可缺"
            )
        df["idx_ret"] = derive_idx_ret(df)


def _bars_to_dict(day_bars_df: pd.DataFrame, trade_date: str) -> dict:
    """单日 bars DataFrame 转 dict-of-dicts，并注入 trade_date 字段。"""
    result = day_bars_df.to_dict("index")
    for key in result:
        result[key]["trade_date"] = trade_date
    return result
