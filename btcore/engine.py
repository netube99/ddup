import copy
import json
import logging
import math
from collections import Counter

import pandas as pd

from btcore import corporate, database, limits, match, stats, types
from btcore.costs import make_costs_fn
from btcore.factors import plan as factor_plan
from btcore.filters import filter_required_columns
from btcore.ml import runtime as ml_runtime
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage

logger = logging.getLogger(__name__)

# select() / on_tick() 返回 dict 的已知键；其余键被忽略并告警（防 typo 静默失效）
_SELECT_KEYS = frozenset({
    "buy", "sell", "buy_conditions", "target_value",
    "sell_shares", "buy_weights", "sell_reasons",
})


def _check_symbol_list(symbols: list, which: str) -> None:
    """select() 名单校验：必须是非空字符串列表且无重复。

    重复 symbol 会导致 manual_buy 双重扣款、持仓被覆盖（账户恒等式破坏），
    这里 fail-fast 而不是让撮合层静默腐化账户。
    """
    bad = [s for s in symbols if not isinstance(s, str) or not s.strip()]
    if bad:
        raise ValueError(f"select() 的 {which} 名单含非字符串或空元素: {bad!r}")
    dups = sorted({s for s, n in Counter(symbols).items() if n > 1})
    if dups:
        raise ValueError(f"select() 的 {which} 名单含重复 symbol: {dups}")


# 数据契约必需列（REQUIRED_BAR_COLUMNS）已下沉至 btcore.factors.plan，
# 与训练侧/研究脚本共享同一定义
def required_bar_columns(strategy, fplan: dict | None = None) -> list[str]:
    """静态推导主面板请求列（preload 列裁剪）。

    来源: REQUIRED_BAR_COLUMNS ∪ strategy.REQUIRED_FIELDS ∪
    FILTER_RULES 显式依赖 ∪ 因子闭包基础列（fplan.main_columns）；
    派生列替换为派生基础列，伪列与物化因子列不请求。
    策略在 select() 里命令式访问的列必须声明进 REQUIRED_FIELDS。
    """
    cols = set(factor_plan.REQUIRED_BAR_COLUMNS)
    cols |= set(getattr(strategy, "REQUIRED_FIELDS", None) or [])
    cols |= filter_required_columns(
        getattr(strategy, "FILTER_RULES", None) or {}
    )
    if fplan:
        cols |= fplan["main_columns"]
    return factor_plan.expand_columns(cols)


class _DaySlicer:
    """按日懒切片：排序 MultiIndex 面板上 .loc 取单日截面，替代全量 groupby 预切。

    峰值内存从「完整面板 + 每日副本」（约 2× 面板）降为「面板 + 单日临时切片」。
    接口对齐 dict（get / __getitem__ / __contains__）：测试手动步进场景仍可
    直接给 engine.bars_by_date 赋普通 dict。
    """

    __slots__ = ("_df",)

    def __init__(self, df: pd.DataFrame):
        self._df = df  # 必须已按 (trade_date, symbol) MultiIndex 排序

    def get(self, trade_date: str, default=None):
        try:
            return self._df.loc[trade_date]
        except KeyError:
            return default

    def __getitem__(self, trade_date: str) -> pd.DataFrame:
        return self._df.loc[trade_date]

    def __contains__(self, trade_date: str) -> bool:
        try:
            self._df.index.get_loc(trade_date)
        except KeyError:
            return False
        return True


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
        # 2026-08 审计补校验：此前负数/NaN 静默接受（负本金直接扭曲全部收益口径）
        if not math.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError(
                f"initial_capital 必须是正有限数值: {self.initial_capital!r}"
            )
        self.db_path = db_path or ":memory:"
        self.max_positions = int(
            max_positions if max_positions is not None
            else config.get("max_positions", 20)
        )
        # 2026-08 审计补校验：≤0 时 manual_buy 静默返回 []，策略永不建仓
        if self.max_positions <= 0:
            raise ValueError(
                f"max_positions 必须是正整数: {self.max_positions!r}"
            )

        slippage_ticks = config.get("slippage_ticks", 2)
        if (not isinstance(slippage_ticks, int)
                or isinstance(slippage_ticks, bool)
                or slippage_ticks < 0):
            raise ValueError(
                f"slippage_ticks 必须是非负整数: {slippage_ticks!r}"
            )
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
        order_volume_ratio = config.get("order_volume_ratio")
        if order_volume_ratio is not None and (
                not isinstance(order_volume_ratio, (int, float))
                or isinstance(order_volume_ratio, bool)
                or not math.isfinite(order_volume_ratio)
                or order_volume_ratio <= 0):
            raise ValueError(
                f"order_volume_ratio 必须是正数或 None: {order_volume_ratio!r}"
            )
        self.order_volume_ratio = order_volume_ratio
        execution_price = config.get("execution_price", "open")
        if execution_price not in ("open", "close"):
            raise ValueError(
                f"execution_price 只支持 'open'/'close': {execution_price!r}"
            )
        self._slippage_ticks = slippage_ticks
        self._execution_price = execution_price
        self.account = self._make_account()
        self.pending_actions = {"buy": [], "sell": []}
        # ML 模型（strategy_loader 依据 YAML models 节挂接；未配置为空列表）
        self._model_specs = list(getattr(strategy, "MODEL_SPECS", None) or [])
        self._holding_models = [m for m in self._model_specs if m.scope == "holding"]
        # ml_log: "full" 落盘全截面分数；缺省只落盘决策相关标的
        self._ml_log_full = config.get("ml_log") == "full"
        # run() 里由 write_run 赋真实 run_id；直接调 step() 的测试用 0
        self.run_id = 0
        self.bars_df: pd.DataFrame | None = None
        self.bars_by_date: dict | _DaySlicer = {}
        self._saved_cash: float | None = None
        self._saved_holdings: dict | None = None
        self._saved_total_value: float | None = None
        self._saved_daily_pnl: float | None = None
        self._saved_cumulative_pnl: float | None = None
        self._saved_pending: dict | None = None
        self._saved_as_of: str | None = None

    def _make_account(self) -> types.Account:
        """新建干净账户（初始资金口径）。run() 每次重跑时用于幂等重置。"""
        account = types.Account(
            cash=self.initial_capital,
            initial_capital=self.initial_capital,
            slippage_ticks=self._slippage_ticks,
            order_volume_ratio=(
                float(self.order_volume_ratio)
                if self.order_volume_ratio is not None else None
            ),
            execution_price=self._execution_price,
        )
        account.total_value = self.initial_capital
        return account

    def run(self, start: str, end: str) -> dict:
        # 幂等：重复 run 从头重置账户与待处理指令（run_id 重置同理，
        # 本次在 write_run 前抛异常时 except 分支不会误标上一次 run）
        self.run_id = 0
        self.account = self._make_account()
        self.pending_actions = {"buy": [], "sell": []}
        self._warn_in_sample_overlap(start, end)
        conn = database.init_backtest_db(self.db_path)
        try:
            calendar = self._prepare(start, end)

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

            prev_day = self.provider.prev_trading_day(calendar[0])
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
                # 基准净值随 stats_json 落库：离线 report 才能画出基准叠加线
                stats_payload = dict(stats_result)
                stats_payload["benchmark_nav"] = benchmark_nav
                stats_payload["benchmark_code"] = self.benchmark
                database.write_run_stats(conn, self.run_id, stats_payload)
                database.update_run_status(conn, self.run_id, "completed")
            return {
                "account_daily": account_daily_df,
                "trade_log": trade_log_df,
                "statistics": stats_result,
                "benchmark_nav": benchmark_nav,
                "benchmark_code": self.benchmark,
            }
        except BaseException:
            # 崩溃（含 KeyboardInterrupt/SystemExit）不留 "running" 假象；
            # run_id=0 说明还没落库，无需标记
            if self.run_id:
                with conn:
                    database.update_run_status(conn, self.run_id, "failed")
            raise
        finally:
            conn.close()

    def _prepare(self, start: str, end: str) -> list[str]:
        """回测/实盘回放共用的预加载管线（无 DB 写入、不触碰账户）。

        日历 → 前视锚定 → universe → 因子物化（广度+主面板）→ ML 分数物化
        → 面板裁切 → provider.attach_bars → strategy.on_start。返回交易日历。
        """
        calendar = self.provider.get_calendar(start, end)
        if not calendar:
            raise ValueError("日历为空")

        # 前视钳制提前到 preload 阶段：get_universe / on_start 内的
        # provider 查询以首日前一交易日为锚（首个模拟日决策时点口径），
        # 钩子里传未来日期也拿不到未来数据
        self.provider.set_as_of(
            self.provider.prev_trading_day(calendar[0]) or calendar[0]
        )

        factor_symbols = self.strategy.get_factor_universe(self.provider, start, end)
        trade_symbols = self.strategy.get_universe(self.provider, start, end)
        # factor_universe 未配置时 factor_symbols 为 None，沿用 trade_symbols
        load_symbols = factor_symbols if factor_symbols is not None else trade_symbols
        fplan = self._build_factor_plan()
        warmup_days = fplan["main_days"] if fplan else factor_plan.DEFAULT_WARMUP_DAYS
        preload_start = (
            pd.Timestamp(calendar[0]) - pd.Timedelta(days=warmup_days)
        ).strftime("%Y%m%d")
        bars_df = self.provider.get_engine_bars(
            load_symbols, calendar[-1],
            lookback_start=preload_start,
            columns=required_bar_columns(self.strategy, fplan),
        )
        bars_df.sort_index(inplace=True)
        factor_plan.validate_required_columns(bars_df)
        factor_plan.derive_fields(bars_df)
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
                getattr(logger, issue["level"])("[因子验证] %s", issue["message"])
        if self._model_specs:
            # panel 模型批量推理 → ml_<name> 分数列（因果物化列的逐行
            # 点态函数，无前视）；在 factor_universe 裁切前执行，截面后
            # 变换的排名口径 = 因子计算域，与训练面板口径一致
            ml_runtime.materialize_predictions(bars_df, self._model_specs)
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
        self.bars_by_date = _DaySlicer(bars_df)
        self.provider.attach_bars(bars_df)
        self.strategy.on_start(self.provider, calendar[0], end_date=end)
        return calendar

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
        factor_plan.derive_fields(breadth_df)
        self._attach_pseudo_columns(breadth_df, fplan["needs"], "breadth")
        return breadth_df

    def _attach_pseudo_columns(self, df: pd.DataFrame, needs: dict, panel: str):
        """按需附着伪列：industry（backend 鸭子类型）/ log_mktcap / idx_ret。"""
        factor_plan.ensure_pseudo_columns(
            df, needs, panel,
            backend=self.provider.backend,
            benchmark=self.benchmark,
        )

    def step(self, today: str, day_bars: pd.DataFrame, conn):
        bars_dict = _bars_to_dict(day_bars, today)

        self._save_state()

        try:
            with conn:
                corporate_log = []
                corporate.adjust(self.account, today, bars_dict,
                                 self.provider, corporate_log)
                # 除权除息后同步 rescale 策略侧 trailing 锚点（S-COND-01，
                # 2026-08 实证：漏 rescale 时次日 calc_conditions 用除权前高点
                # 重算 TRAILING_TP 触发价，开盘即误触发卖出）
                cond = getattr(getattr(self.strategy, "_cond", None), "rescale", None)
                if cond is not None:
                    for entry in corporate_log:
                        scale = entry.get("scale")
                        if scale is not None:
                            cond(entry["symbol"], scale)

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
                        trigger="MANUAL",
                        reasons_map=self.pending_actions.get("sell_reasons"),
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
                    self.pending_actions.get("buy_conditions") or [],
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
                if self._model_specs:
                    self._write_ml_predictions(conn, today, bars_dict)
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
            elif event["type"] == "stk_div":
                # 送转增股必须落库：stats 往返盈亏 / Brinson 持仓重建 /
                # ML 回合配对都从 trade_log 重建持股事实，缺失会腐化三处
                database.write_trade(conn, self.run_id, types.Trade(
                    date=today, symbol=event["symbol"], side="STK_DIV",
                    trigger="CORPORATE", price=0.0, shares=event["new_shares"],
                    turnover=0.0, commission=0.0, stamp_tax=0.0,
                    transfer_fee=0.0, slippage_amount=0.0,
                    net_amount=0.0, reason="stk_div",
                ))

    def _compute_pending(self, calc_date: str, bars_dict: dict | None = None,
                         trades: list | None = None):
        self.provider.set_as_of(calc_date)

        for holding in self.account.holdings.values():
            holding.holding_days += 1
            holding.locked = False

        if bars_dict is None:
            day_bars_view = self.bars_by_date.get(calc_date)
            if day_bars_view is None:
                return
            bars_dict = _bars_to_dict(day_bars_view, calc_date)

        # holding scope 模型：账户态特征只能在决策时点计算，分数注入持仓
        # 的 bar dict——策略在 on_tick/select/calc_conditions 中像读普通列
        # 一样读 ml_<name>，引擎不负责解释分数的含义
        if self._holding_models:
            self._inject_holding_model_scores(bars_dict)

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
        # on_tick 是可选钩子：每日运行，在 select 之前更新策略内部状态
        on_tick = getattr(self.strategy, "on_tick", None)
        on_tick_result = None
        if callable(on_tick):
            on_tick_result = on_tick(bars_dict, snapshot, self.provider)

        actions = self.strategy.select(bars_dict, snapshot, self.provider)
        if not isinstance(actions, dict):
            raise ValueError(
                f"select() 必须返回 dict，得到 {type(actions).__name__}: {actions!r}"
            )
        unknown = set(actions) - _SELECT_KEYS
        if unknown:
            logger.warning(
                "select() 返回未知键（将被忽略，可能是 typo）: %s", sorted(unknown)
            )

        # 合并 on_tick 返回的 buy_conditions；其余键（含 buy/sell 等合法
        # select 键）不在 on_tick 协议内，返回即报错——静默丢弃是 typo 温床
        if on_tick_result is not None:
            if not isinstance(on_tick_result, dict):
                raise ValueError(
                    "on_tick() 必须返回 dict 或 None，得到 "
                    f"{type(on_tick_result).__name__}: {on_tick_result!r}"
                )
            tick_extra = set(on_tick_result) - {"buy_conditions"}
            if tick_extra:
                raise ValueError(
                    "on_tick() 只支持返回 buy_conditions（买卖名单请走 "
                    f"select()）: {sorted(tick_extra)}"
                )
            if on_tick_result.get("buy_conditions"):
                existing_conds = actions.setdefault("buy_conditions", [])
                existing_conds.extend(on_tick_result["buy_conditions"])

        buy_list = actions.get("buy", [])
        sell_list = actions.get("sell", [])
        if not isinstance(buy_list, list) or not isinstance(sell_list, list):
            raise ValueError(
                f"select() 的 buy/sell 必须是 list: "
                f"buy={buy_list!r} sell={sell_list!r}"
            )
        _check_symbol_list(buy_list, "buy")
        _check_symbol_list(sell_list, "sell")
        buy = set(buy_list)
        sell = set(sell_list)
        if buy & sell:
            raise ValueError(f"同日买卖冲突: {buy & sell}")
        target_value = actions.get("target_value") or {}
        if target_value and (buy or sell):
            raise ValueError("target_value 与 buy/sell 名单互斥, 同日只能用一种")
        if not isinstance(target_value, dict):
            raise ValueError("target_value 必须是 {symbol: 目标市值} 的 dict")
        for symbol, tv in target_value.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError(f"target_value 键必须是非空字符串: {symbol!r}")
            if (isinstance(tv, bool) or not isinstance(tv, (int, float))
                    or not math.isfinite(tv) or tv < 0):
                raise ValueError(
                    f"target_value[{symbol}] 必须是 ≥0 的有限数值: {tv!r}"
                )

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

        sell_reasons = actions.get("sell_reasons") or {}
        if not isinstance(sell_reasons, dict):
            raise ValueError("sell_reasons 必须是 {symbol: 原因字符串} 的 dict")
        for symbol, reason in sell_reasons.items():
            if symbol not in sell:
                raise ValueError(
                    f"sell_reasons 的 {symbol} 不在 sell 名单里"
                )
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    f"sell_reasons[{symbol}] 必须是非空字符串: {reason!r}"
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
            # 2026-08 审计补校验：calc_conditions 返回非 list（None/int/str/dict）
            # 此前以晦涩异常崩溃或空 dict 静默当作无离场计划（S-HOOK-05）
            conditions = self.strategy.calc_conditions(
                symbol, entry_price, bar, holding_days
            )
            if not isinstance(conditions, list):
                raise ValueError(
                    f"calc_conditions() 必须返回 list[dict]，{symbol} 得到 "
                    f"{type(conditions).__name__}: {conditions!r}"
                )
            holding.conditions = conditions
            match.conditions.validate_condition_types(holding.conditions)

    def _warn_in_sample_overlap(self, start: str, end: str) -> None:
        """回测窗口与模型训练窗口重叠时告警（样本内乐观偏差风险）。

        仅告警不阻断：walk-forward 复用已训练模型是合法用法。
        """
        for spec in self._model_specs:
            tw = spec.meta.get("train_window") or []
            if len(tw) != 2:
                continue
            t0, t1 = str(tw[0]), str(tw[1])
            if start <= t1 and end >= t0:
                logger.warning(
                    "模型 %s 训练窗口 [%s, %s] 与回测窗口 [%s, %s] 重叠——"
                    "样本内乐观偏差风险（walk-forward 复用可忽略此告警）",
                    spec.name, t0, t1, start, end,
                )

    def _inject_holding_model_scores(self, bars_dict: dict) -> None:
        """holding scope 模型逐持仓求值并注入 bar dict（原地，批量推理）。"""
        for spec in self._holding_models:
            items = [
                (symbol, bars_dict[symbol], holding)
                for symbol, holding in self.account.holdings.items()
                if symbol in bars_dict
            ]
            if not items:
                continue
            scores = ml_runtime.holding_scores_batch(
                spec,
                [bar for _, bar, _ in items],
                [holding for _, _, holding in items],
            )
            score_map = {
                symbol: s
                for (symbol, _, _), s in zip(items, scores)
                if s is not None
            }
            score_map = ml_runtime.apply_post_transform_flat(
                pd.Series(score_map), spec.post_transform
            ).to_dict()
            for symbol, score in score_map.items():
                bars_dict[symbol][spec.column] = score

    def _write_ml_predictions(self, conn, today: str, bars_dict: dict) -> None:
        """ML 分数落盘（ml_predictions 表）。

        panel scope：缺省只落盘决策相关标的（持仓 + 当日买卖名单 +
        条件买单），config.ml_log == "full" 时落盘全截面（体积大，仅诊断用）。
        holding scope：落盘当日注入的全部持仓分数。
        """
        rows: list[tuple] = []
        panel_specs = [m for m in self._model_specs if m.scope == "panel"]
        if panel_specs:
            if self._ml_log_full:
                symbols = set(bars_dict)
            else:
                symbols = set(self.account.holdings)
                symbols |= set(self.pending_actions.get("buy", []))
                symbols |= set(self.pending_actions.get("sell", []))
                symbols |= {
                    o["symbol"]
                    for o in (self.pending_actions.get("buy_conditions") or [])
                }
            for sym in symbols:
                bar = bars_dict.get(sym)
                if bar is None:
                    continue
                for spec in panel_specs:
                    score = bar.get(spec.column)
                    if score is not None:
                        rows.append((spec.name, sym, float(score)))
        for spec in self._holding_models:
            for symbol in self.account.holdings:
                bar = bars_dict.get(symbol)
                if bar is None:
                    continue
                score = bar.get(spec.column)
                if score is not None:
                    rows.append((spec.name, symbol, float(score)))
        if rows:
            database.write_ml_predictions(conn, self.run_id, today, rows)

    def _save_state(self):
        self._saved_cash = self.account.cash
        self._saved_holdings = copy.deepcopy(self.account.holdings)
        self._saved_total_value = self.account.total_value
        self._saved_daily_pnl = self.account.daily_pnl
        self._saved_cumulative_pnl = self.account.cumulative_pnl
        self._saved_pending = copy.deepcopy(self.pending_actions)
        self._saved_as_of = (
            self.provider._as_of_date if self.provider is not None else None
        )

    def _restore_state(self):
        if self._saved_cash is not None:
            self.account.cash = self._saved_cash
        if self._saved_holdings is not None:
            self.account.holdings = self._saved_holdings
        if self._saved_total_value is not None:
            self.account.total_value = self._saved_total_value
            self.account.daily_pnl = self._saved_daily_pnl
            self.account.cumulative_pnl = self._saved_cumulative_pnl
        if self._saved_pending is not None:
            self.pending_actions = self._saved_pending
        if self.provider is not None:
            self.provider._as_of_date = self._saved_as_of

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
        bc_symbols = {c["symbol"] for c in (pending_actions.get("buy_conditions") or [])}
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
            "holdings_detail": holdings_detail,
            "bars_subset": bars_subset,
        }
        database.write_debug_snapshot(conn, self.run_id, today, snapshot)


def is_valid_positive(v) -> bool:
    """正数校验: 拒绝 bool / None / NaN / 非正。"""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v > 0)


def _bars_to_dict(day_bars_df: pd.DataFrame, trade_date: str) -> dict:
    """单日 bars DataFrame 转 dict-of-dicts，并注入 trade_date 字段。"""
    result = day_bars_df.to_dict("index")
    for key in result:
        result[key]["trade_date"] = trade_date
    return result
