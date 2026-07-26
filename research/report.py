"""单文件 HTML 回测报告生成器。

纯 Python + 内联 SVG，零第三方依赖，生成结果完全离线可读。

入口：
- generate_report(result, out_path)：内存中的 engine.run() 结果 → 单 run 报告
- generate_report_from_db(db_path, out_path, run_id)：结果库 → 单 run 报告
- generate_compare_report(db_path, out_path, run_ids)：结果库 → 多 run 对比报告
- load_runs(db_path, run_ids)：读侧入口，CLI 对比表共用
"""

import html
import re
import sqlite3

import numpy as np
import pandas as pd

from btcore import database, stats

_PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2"]
_STOCK_NAME_CACHE: dict[str, str] = {}


def _load_stock_names() -> dict[str, str]:
    """从 tushare market.db 加载 ts_code → 股票名称映射（含指数名）。"""
    global _STOCK_NAME_CACHE
    if _STOCK_NAME_CACHE:
        return _STOCK_NAME_CACHE
    market_db = _market_db_path()
    if not market_db:
        return {}
    try:
        conn = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
        rows = conn.execute("SELECT ts_code, name FROM stock_basic").fetchall()
        _STOCK_NAME_CACHE = {r[0]: r[1] for r in rows}
        # 同时加载指数名称
        idx_rows = conn.execute("SELECT ts_code, name FROM index_basic").fetchall()
        for r in idx_rows:
            _STOCK_NAME_CACHE[r[0]] = r[1]
        conn.close()
    except Exception:
        pass
    return _STOCK_NAME_CACHE


def _market_db_path() -> str | None:
    from pathlib import Path
    import re
    adapter = Path(__file__).resolve().parents[1] / "adapters" / "tushare.py"
    with open(adapter) as f:
        content = f.read()
    m = re.search(r'_DEFAULT_DB_PATH\s*=\s*"([^"]+)"', content)
    return m.group(1) if m else None


def _benchmark_name(code: str | None) -> str:
    if not code:
        return "基准"
    names = _load_stock_names()
    name = names.get(code)
    return f"{name}({code})" if name else code


def _stock_name(code: str) -> str:
    names = _load_stock_names()
    name = names.get(code, "")
    return f"{name}({code})" if name else code


# ---------------------------------------------------------------- 数据加载


def load_runs(db_path: str, run_ids: list[int] | None = None) -> list[dict]:
    """从结果库加载 run 列表，每项 {meta, account_daily, trade_log, statistics}。

    stats_json 为 NULL 的老 run 用 stats.calculate_statistics 现场重算
    （无 benchmark / 期末持仓，benchmark_compare 与浮盈口径会比 run 时略少）。
    """
    # 先经 init_backtest_db 打开一次，确保老库完成 stats_json 迁移
    database.init_backtest_db(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        runs_df = database.read_runs(conn)
        if run_ids:
            runs_df = runs_df[runs_df["run_id"].isin(run_ids)]
        runs = []
        for meta in runs_df.to_dict("records"):
            adf, tdf, stats_dict = database.read_run_data(conn, meta["run_id"])
            if stats_dict is None and not adf.empty:
                stats_dict = stats.calculate_statistics(adf, tdf)
            runs.append({
                "meta": meta,
                "account_daily": adf,
                "trade_log": tdf,
                "statistics": stats_dict or {},
            })
        return runs
    finally:
        conn.close()


# ---------------------------------------------------------------- 格式化


def _pct(v) -> str:
    return f"{float(v) * 100:.2f}%"


def _num(v) -> str:
    return f"{float(v):,.2f}"


def _ratio(v) -> str:
    return f"{float(v):.2f}"


def _int(v) -> str:
    return f"{int(v)}"


def _esc(s) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------- SVG 图表


def _svg_line_chart(series: list[tuple[str, list[float], str]],
                    x_labels: list[str] | None = None,
                    width: int = 920, height: int = 300,
                    pct: bool = False, fill: bool = False,
                    baseline: float | None = None) -> str:
    """多序列折线图（可选面积填充）。series 为 [(名称, 值列表, 颜色)]。"""
    series = [(name, vals, color) for name, vals, color in series if len(vals) >= 2]
    if not series:
        return '<p class="empty">数据不足，无法绘图</p>'

    ml, mr, mt, mb = 64.0, 16.0, 20.0, 36.0
    pw, ph = width - ml - mr, height - mt - mb
    all_vals = [v for _, vals, _ in series for v in vals]
    vmin, vmax = min(all_vals), max(all_vals)
    if baseline is not None:
        vmin, vmax = min(vmin, baseline), max(vmax, baseline)
    pad = (vmax - vmin) * 0.05 or 0.01
    vmin, vmax = vmin - pad, vmax + pad

    def y_of(v: float) -> float:
        return mt + (vmax - v) / (vmax - vmin) * ph

    def x_of(i: int, n: int) -> float:
        return ml + i / (n - 1) * pw

    def fmt(v: float) -> str:
        return f"{v * 100:.1f}%" if pct else f"{v:,.2f}"

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']

    # 横向网格线 + Y 轴刻度
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = y_of(v)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#6b7280">{fmt(v)}</text>'
        )

    # X 轴刻度（首/中/尾等 5 个位置）
    if x_labels and len(x_labels) >= 2:
        n_lab = len(x_labels)
        for k in range(5):
            i = round(k * (n_lab - 1) / 4)
            x = x_of(i, n_lab)
            parts.append(
                f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle" '
                f'font-size="11" fill="#6b7280">{_esc(x_labels[i])}</text>'
            )

    if baseline is not None and vmin < baseline < vmax:
        y0 = y_of(baseline)
        parts.append(
            f'<line x1="{ml}" y1="{y0:.1f}" x2="{width - mr}" y2="{y0:.1f}" '
            f'stroke="#9ca3af" stroke-width="1" stroke-dasharray="4 3"/>'
        )

    # 折线（面积图填充到 baseline，缺省到底边）
    for name, vals, color in series:
        n = len(vals)
        points = " ".join(f"{x_of(i, n):.1f},{y_of(v):.1f}" for i, v in enumerate(vals))
        if fill:
            yb = y_of(baseline) if baseline is not None else mt + ph
            area = (
                f"{x_of(0, n):.1f},{yb:.1f} " + points + f" {x_of(n - 1, n):.1f},{yb:.1f}"
            )
            parts.append(f'<polygon points="{area}" fill="{color}" fill-opacity="0.15"/>')
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>'
        )

    # 图例（图表顶部居中，半透明白底避免与曲线重叠）
    if len(series) > 0:
        # 估算图例总宽度：色块 + 间距 + 文字
        item_w = 120  # 每项约 120px
        total_w = len(series) * item_w
        lx_start = (width - total_w) / 2
        ly = mt + 2
        lh = 22
        # 半透明白底
        parts.append(
            f'<rect x="{lx_start - 4}" y="{ly}" width="{total_w + 8}" height="{lh}" '
            f'fill="#ffffff" fill-opacity="0.85" rx="3"/>'
        )
        for k, (name, _, color) in enumerate(series):
            cx = lx_start + k * item_w
            parts.append(
                f'<rect x="{cx}" y="{ly + 6}" width="10" height="10" fill="{color}"/>'
                f'<text x="{cx + 14}" y="{ly + 15}" font-size="11" '
                f'fill="#374151">{_esc(name)}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- HTML 片段


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0 auto; max-width: 980px; padding: 24px; color: #111827; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }}
h2 {{ font-size: 17px; margin-top: 32px; border-left: 4px solid #2563eb;
     padding-left: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }}
th, td {{ border: 1px solid #e5e7eb; padding: 5px 8px; text-align: right; }}
th {{ background: #f3f4f6; }}
td:first-child, th:first-child {{ text-align: left; }}
.meta {{ color: #6b7280; font-size: 13px; }}
.empty {{ color: #9ca3af; font-size: 13px; }}
.up {{ color: #dc2626; }}
.down {{ color: #16a34a; }}
svg {{ width: 100%; height: auto; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _metric_table(spec: list[tuple[str, str, object]], statistics: dict) -> str:
    rows = []
    for label, key, fmt in spec:
        if key not in statistics:
            continue
        rows.append(f"<tr><td>{_esc(label)}</td><td>{fmt(statistics[key])}</td></tr>")
    if not rows:
        return '<p class="empty">无数据</p>'
    return "<table><tr><th>指标</th><th>值</th></tr>" + "".join(rows) + "</table>"


def _dict_table(title_row: tuple[str, str], data: dict, fmt,
                colorize: bool = False) -> str:
    if not data:
        return '<p class="empty">无数据</p>'
    if colorize:
        rows = "".join(
            f"<tr><td>{_esc(k)}</td><td class=\"{'up' if float(v) > 0 else 'down' if float(v) < 0 else ''}\">"
            f"{fmt(v)}</td></tr>" for k, v in data.items()
        )
    else:
        rows = "".join(
            f"<tr><td>{_esc(k)}</td><td>{fmt(v)}</td></tr>" for k, v in data.items()
        )
    return f"<table><tr><th>{title_row[0]}</th><th>{title_row[1]}</th></tr>{rows}</table>"


_CORE_SPEC = [
    ("总收益率", "total_return", _pct),
    ("年化收益率", "annualized_return", _pct),
    ("年化波动率", "annualized_volatility", _pct),
    ("夏普比率", "sharpe", _ratio),
    ("Sortino 比率", "sortino", _ratio),
    ("Calmar 比率", "calmar", _ratio),
    ("最大回撤", "max_drawdown", _pct),
    ("最大回撤修复天数", "max_drawdown_recovery_days", _int),
    ("日胜率", "win_rate", _pct),
    ("回测天数", "total_days", _int),
    ("成交笔数", "trade_count", _int),
    ("区间换手率", "turnover_rate", _ratio),
    ("平均持仓数", "avg_positions", _ratio),
]

_FRICTION_SPEC = [
    ("总交易成本", "total_cost", _num),
    ("单笔平均成本", "cost_per_trade", _num),
    ("双边综合磨损率", "cost_pct_of_turnover", _pct),
    ("年化磨损拖累", "annualized_cost_drag", _pct),
    ("成本占盈利交易利润比", "cost_pct_of_gross_profit", _pct),
    ("无摩擦对照收益率", "no_cost_total_return", _pct),
    ("滑点占成本比", "slippage_share", _pct),
]

_COST_SPEC = [
    ("买入佣金", "buy_commission", _num),
    ("卖出佣金", "sell_commission", _num),
    ("印花税", "stamp_tax", _num),
    ("过户费", "transfer_fee", _num),
    ("滑点金额", "slippage", _num),
]

_COMPLEXITY_SPEC = [
    ("最大同时持仓数", "max_positions", _int),
    ("日均成交笔数", "avg_trades_per_day", _ratio),
    ("有成交日日均笔数", "avg_trades_per_active_day", _ratio),
    ("单日最大成交笔数", "max_trades_per_day", _int),
    ("有成交天数占比", "active_day_ratio", _pct),
    ("单笔买入金额均值", "avg_buy_amount", _num),
    ("单笔买入金额最小值", "min_buy_amount", _num),
    ("单票平均市值", "avg_position_value", _num),
]

_ROUND_TRIP_SPEC = [
    ("已完成往返次数", "total_round_trips", _int),
    ("期末未平仓笔数", "open_positions", _int),
    ("已实现盈亏", "total_realized_pnl", _num),
    ("未实现盈亏", "total_unrealized_pnl", _num),
    ("盈利次数", "win_count", _int),
    ("亏损次数", "loss_count", _int),
    ("盈亏次数比", "win_loss_ratio", _ratio),
    ("分红合计", "total_dividend_received", _num),
    ("平均单笔盈亏", "avg_pnl", _num),
    ("平均持有天数", "avg_holding_days", _ratio),
]


def _nav_series(account_daily: pd.DataFrame) -> tuple[list[str], list[float]]:
    dates = [str(d) for d in account_daily["date"]]
    nav = (account_daily["total_value"] / account_daily["initial_capital"]).tolist()
    return dates, nav


def _drawdown_series(nav: list[float]) -> list[float]:
    arr = np.asarray(nav, dtype=float)
    peak = np.maximum.accumulate(arr)
    return (arr / peak - 1.0).tolist()


def _trade_table(trade_log: pd.DataFrame) -> str:
    if trade_log.empty:
        return '<p class="empty">无成交记录</p>'
    cols = ["date", "symbol", "side", "trigger", "price", "shares", "turnover",
            "commission", "stamp_tax", "transfer_fee", "slippage_amount", "reason"]
    cols = [c for c in cols if c in trade_log.columns]
    headers = [_stock_name(c) if c == "symbol" else c for c in cols]
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    rows = []
    for row in trade_log[cols].itertuples(index=False):
        cells = []
        for c, v in zip(cols, row):
            if c == "symbol":
                cells.append(f"<td>{_esc(_stock_name(str(v)))}</td>")
            elif c in ("price", "turnover", "commission", "stamp_tax",
                     "transfer_fee", "slippage_amount"):
                cells.append(f"<td>{float(v):,.2f}</td>")
            else:
                cells.append(f"<td>{_esc(v)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


def _symbol_contribution_table(statistics: dict) -> str:
    contrib = statistics.get("symbol_contribution") or {}
    if not contrib:
        return '<p class="empty">无数据</p>'
    top = sorted(
        contrib.items(), key=lambda kv: abs(kv[1]["total_contribution"]), reverse=True
    )[:10]
    rows = "".join(
        f"<tr><td>{_esc(_stock_name(sym))}</td><td>{_num(d['realized_pnl'])}</td>"
        f"<td>{_num(d['unrealized_pnl'])}</td><td>{_num(d['dividend_received'])}</td>"
        f"<td>{_num(d['total_contribution'])}</td></tr>"
        for sym, d in top
    )
    return (
        "<table><tr><th>股票</th><th>已实现盈亏</th><th>未实现盈亏</th>"
        f"<th>分红</th><th>总贡献</th></tr>{rows}</table>"
    )


def _sell_source_table(statistics: dict) -> str:
    src = statistics.get("sell_source") or {}
    if not src:
        return '<p class="empty">无数据</p>'
    rows = "".join(
        f"<tr><td>{_esc(trigger)}</td><td>{d['count']}</td>"
        f"<td>{_num(d['total_pnl'])}</td><td>{_num(d['avg_pnl'])}</td>"
        f"<td>{_pct(d['win_rate'])}</td><td>{d['avg_holding_days']:.1f}</td></tr>"
        for trigger, d in sorted(
            src.items(), key=lambda kv: abs(kv[1]["total_pnl"]), reverse=True
        )
    )
    return (
        "<table><tr><th>卖出来源</th><th>次数</th><th>总盈亏</th>"
        f"<th>平均盈亏</th><th>胜率</th><th>平均持仓天数</th></tr>{rows}</table>"
    )


# ---------------------------------------------------------------- 单 run 报告


def _single_run_body(result: dict, title: str, meta_line: str) -> str:
    statistics = result["statistics"]
    account_daily = result["account_daily"]
    trade_log = result["trade_log"]

    dates, nav = _nav_series(account_daily)
    dd = _drawdown_series(nav)

    parts = [f"<h1>{_esc(title)}</h1>", f'<p class="meta">{_esc(meta_line)}</p>']

    parts.append("<h2>核心指标</h2>")
    parts.append(_metric_table(_CORE_SPEC, statistics))

    # 净值曲线：策略 + 基准（如有）
    bm_code = result.get("benchmark_code")
    bm_label = _benchmark_name(bm_code)
    nav_series = [("策略净值", nav, _PALETTE[0])]
    bm_nav = result.get("benchmark_nav")
    if bm_nav and len(bm_nav) == len(dates):
        nav_series.append((bm_label, bm_nav, "#9ca3af"))
    parts.append("<h2>净值曲线</h2>")
    parts.append(_svg_line_chart(nav_series, dates, baseline=1.0))

    parts.append("<h2>回撤曲线</h2>")
    parts.append(_svg_line_chart(
        [("回撤", dd, _PALETTE[1])], dates, pct=True, fill=True, baseline=0.0
    ))

    parts.append("<h2>月度收益</h2>")
    parts.append(_dict_table(("月份", "收益"), statistics.get("monthly_returns", {}), _pct, colorize=True))

    parts.append(f"<h2>基准对比 — {bm_label}</h2>")
    bench = statistics.get("benchmark_compare", {})
    if bench:
        bench_spec = [
            ("策略收益率", "strategy_total_return", _pct),
            ("基准收益率", "benchmark_total_return", _pct),
            ("基准年化收益率", "benchmark_annual_return", _pct),
            ("基准最大回撤", "benchmark_max_drawdown", _pct),
            ("Alpha", "alpha", _pct),
            ("Beta", "beta", _ratio),
            ("信息比率", "information_ratio", _ratio),
            ("跟踪误差", "tracking_error", _pct),
        ]
        parts.append(_metric_table(bench_spec, bench))
    else:
        parts.append('<p class="empty">无基准数据</p>')

    parts.append("<h2>交易磨损</h2>")
    parts.append(_metric_table(_FRICTION_SPEC, statistics.get("trading_friction", {})))
    parts.append(_metric_table(_COST_SPEC, statistics.get("cost_breakdown", {})))

    parts.append("<h2>持仓管理复杂度</h2>")
    parts.append(_metric_table(_COMPLEXITY_SPEC, statistics.get("management_complexity", {})))

    parts.append("<h2>往返交易汇总</h2>")
    rt = statistics.get("round_trip", {})
    parts.append(_metric_table(_ROUND_TRIP_SPEC, rt.get("summary", {})))

    parts.append("<h2>卖出来源归因</h2>")
    parts.append(_sell_source_table(statistics))

    parts.append("<h2>个股盈亏贡献 Top10</h2>")
    parts.append(_symbol_contribution_table(statistics))

    parts.append("<h2>成交明细</h2>")
    parts.append(_trade_table(trade_log))

    return "".join(parts)


def generate_report(result: dict, out_path: str, title: str | None = None):
    """engine.run() 的返回 dict → 单文件 HTML 报告。"""
    statistics = result["statistics"]
    if title is None:
        title = (
            f"回测报告 {statistics.get('start_date', '')} ~ "
            f"{statistics.get('end_date', '')}"
        )
    meta_line = (
        f"初始资金 {_num(statistics.get('initial_capital', 0))} · "
        f"期末市值 {_num(statistics.get('final_value', 0))} · "
        f"成交 {statistics.get('trade_count', 0)} 笔"
    )
    body = _single_run_body(result, title, meta_line)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_PAGE.format(title=_esc(title), body=body))


def generate_report_from_db(db_path: str, out_path: str, run_id: int | None = None):
    """从结果库离线生成单 run 报告；run_id 缺省取最新。"""
    runs = load_runs(db_path, [run_id] if run_id else None)
    if not runs:
        raise ValueError(f"结果库 {db_path} 中没有可用 run")
    run = runs[-1]
    meta = run["meta"]
    result = {
        "account_daily": run["account_daily"],
        "trade_log": run["trade_log"],
        "statistics": run["statistics"],
    }
    title = (
        f"回测报告 {meta.get('strategy', '')} "
        f"{meta.get('start_date', '')} ~ {meta.get('end_date', '')} (run {meta['run_id']})"
    )
    generate_report(result, out_path, title=title)


# ---------------------------------------------------------------- 对比报告


_COMPARE_SPEC = [
    ("总收益率", "total_return", _pct),
    ("年化收益率", "annualized_return", _pct),
    ("夏普比率", "sharpe", _ratio),
    ("最大回撤", "max_drawdown", _pct),
    ("Calmar 比率", "calmar", _ratio),
    ("日胜率", "win_rate", _pct),
    ("成交笔数", "trade_count", _int),
    ("区间换手率", "turnover_rate", _ratio),
    ("总交易成本", "trading_friction.total_cost", _num),
    ("年化磨损拖累", "trading_friction.annualized_cost_drag", _pct),
    ("单日最大成交笔数", "management_complexity.max_trades_per_day", _int),
]


def _dig(statistics: dict, dotted: str):
    cur = statistics
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _run_label(run: dict) -> str:
    meta = run["meta"]
    return f"run{meta['run_id']} {meta.get('strategy', '')}"


def build_compare_table(runs: list[dict]) -> tuple[list[str], list[list[str]]]:
    """多 run 关键指标对比表（CLI 与 HTML 共用）：表头 + 行。"""
    header = ["指标"] + [_run_label(r) for r in runs]
    rows = []
    for label, key, fmt in _COMPARE_SPEC:
        row = [label]
        for run in runs:
            v = _dig(run["statistics"], key)
            row.append(fmt(v) if v is not None else "-")
        rows.append(row)
    return header, rows


def generate_compare_report(db_path: str, out_path: str,
                            run_ids: list[int] | None = None):
    """多 run 对比 HTML：元信息表 + 指标对比表 + 归一化净值叠加曲线。"""
    runs = load_runs(db_path, run_ids)
    if len(runs) < 2:
        raise ValueError("对比至少需要 2 个 run")

    title = "多 run 对比报告"
    parts = [f"<h1>{title}</h1>"]

    meta_rows = "".join(
        f"<tr><td>{m['run_id']}</td><td>{_esc(m.get('strategy', ''))}</td>"
        f"<td>{_esc(m.get('start_date', ''))} ~ {_esc(m.get('end_date', ''))}</td>"
        f"<td>{_num(m.get('initial_capital', 0))}</td>"
        f"<td>{_esc(m.get('status', ''))}</td><td>{_esc(m.get('created_at', ''))}</td></tr>"
        for m in (r["meta"] for r in runs)
    )
    parts.append("<h2>Runs</h2>")
    parts.append(
        "<table><tr><th>run_id</th><th>策略</th><th>区间</th>"
        f"<th>初始资金</th><th>状态</th><th>创建时间</th></tr>{meta_rows}</table>"
    )

    header, rows = build_compare_table(runs)
    head = "".join(f"<th>{_esc(h)}</th>" for h in header)
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    parts.append("<h2>关键指标对比</h2>")
    parts.append(f"<table><tr>{head}</tr>{body_rows}</table>")

    series = []
    longest_dates: list[str] = []
    for k, run in enumerate(runs):
        dates, nav = _nav_series(run["account_daily"])
        if len(dates) > len(longest_dates):
            longest_dates = dates
        series.append((_run_label(run), nav, _PALETTE[k % len(_PALETTE)]))
    parts.append("<h2>归一化净值对比</h2>")
    parts.append(_svg_line_chart(series, longest_dates, baseline=1.0))

    body = "".join(parts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_PAGE.format(title=title, body=body))
