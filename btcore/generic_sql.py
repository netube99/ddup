"""填表式 SQLite 后端 — 声明你的数据位置，零 SQL 接入 ddup。

用户只回答一个问题：每份数据在自己库里的 "表名.字段名" 是什么。
没有主表/辅助表之分——OHLC 分存四张表也直接各填各的；引擎需要的
(交易日, 代码) 网格面板由后端内部用所有被引用表的键外并集拼出，
某字段在某张表没有对应行即为 NaN。拼表、对齐、列裁剪全部是内部逻辑。
填好的完整示例见 adapters/tushare.py；对接说明见 docs/backend_guide.md。

表单（Python dict）：

    {
        # ══ 引擎必需 ══
        "symbol": "ts_code",                # 证券代码列的名字（各表共同的查询键）
        "date":   "trade_date",             # 交易日列的名字（YYYYMMDD 字符串）
        # 以下每个空都填 "表名.字段名"
        # 数据契约字段 —— 写在哪个表都行，自动按 (日期,代码) 对齐
        "open": "quotes.open",  "high": "quotes.high",
        "low": "quotes.low",    "close": "quotes.close",
        "vol": "quotes.vol",
        "adj_factor": "quotes.adj_factor", "pre_close": "quotes.pre_close",
        "up_limit": "limits.up_limit", "down_limit": "limits.down_limit",
        # 交易日历与分红
        "calendar_date": "trade_cal.cal_date",      # 日历表的日期列
        "dividend_ex_date": "dividend.ex_date",     # 除权除息日
        "dividend_stk_div": "dividend.stk_div",     # 每股送转
        "dividend_cash_div": "dividend.cash_div",   # 每股现金红利

        # ══ 引擎辅助能力（不填 = 该能力关闭）══
        "st_symbol": "stock_st.ts_code",            # ST 标记表的代码列
        "industry_name": "ind_class.l1_name",       # 行业分类列
        "listing_date": "stock_basic.list_date",    # 上市日期列
        "index_code": "index_weight.index_code",    # 指数代码列
        "index_member": "index_weight.con_code",    # 指数成分列
        "benchmark_close": "fund_daily.close",      # 基准收盘价
        "benchmark_adj_factor": "fund_adj.adj_factor",  # 基准复权因子（可选）
        "benchmark_code": "510300.SH",      # 默认基准代码（取值，非位置）

        # ══ 自选扩展字段（因子/策略要用什么加什么）══
        "extra_fields": {"amount": "quotes.amount", "turnover_rate": "quotes.turnover_rate"},

        # ══ 沉底：表的特殊说明（大多数库为空）══
        "tables": {                          # 某张表有特殊之处？在这里给它加一条
            "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
            "dividend": {"filter": {"div_proc": "实施", "ex_date": None}},
            # "某表": {"symbol": "code", "date": "dt"},  # 键列名与全局不一致
            # "某表": {"filter_sql": "..."},             # filter 表达不了的逃生舱
        },
    }

推断规则：每张表的代码/日期列默认叫 "symbol"/"date" 空里声明的名字，
不同名在 tables 节里用 symbol/date 键指出；能力开关 = 对应的空填没填
（index_code + index_member 必须成对；benchmark_adj_factor 可选，
指数点位等无复权概念的基准只填 benchmark_close 即可）；filter/filter_sql
只对日历/分红/ST/指数成分表有效（filter 名值对
中 None → IS NOT NULL）；任何表都可以是 VIEW。生成的 SQL 对所有表名/
列名统一加双引号，物理名撞 SQL 保留字（如 limit）的字段可直接对接。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

import pandas as pd

from btcore.backend import DataBackend

logger = logging.getLogger(__name__)


def _q(name: str) -> str:
    """SQL 标识符双引号包裹：物理表/列名撞保留字（如 limit）或含特殊字符时
    仍可对接；标识符内的双引号按 SQL 标准双写转义。"""
    return '"' + name.replace('"', '""') + '"'

# 鸭子类型扩展方法名 → 编译后角色节名；已填的空在实例上动态装配同名方法
_EXTRAS = {
    "get_benchmark_bars": "benchmark",
    "get_st_map": "st",
    "get_stock_industries": "industry",
    "get_recent_listings": "listings",
    "get_index_members": "index_members",
}

# 数据契约字段（口径见 docs/backend_guide.md）
# amount 归为扩展字段：引擎内部不消费，策略通过 REQUIRED_FIELDS 按需声明
_CONTRACT_FIELDS = (
    "open", "high", "low", "close", "vol",
    "adj_factor", "pre_close", "up_limit", "down_limit",
)

# 固定词汇：通用查询键（纯列名）/ 必需位置空 / 能力位置空 / 非位置键
_KEY_BLANKS = ("symbol", "date")
_REQUIRED_LOC_BLANKS = (
    *_CONTRACT_FIELDS,
    "calendar_date", "dividend_ex_date", "dividend_stk_div", "dividend_cash_div",
)
_CAPABILITY_BLANKS = (
    "st_symbol", "industry_name", "listing_date",
    "index_code", "index_member", "benchmark_close", "benchmark_adj_factor",
)
_NON_LOC_KEYS = ("extra_fields", "tables", "benchmark_code")
_TOP_KEYS = frozenset(
    _KEY_BLANKS + _REQUIRED_LOC_BLANKS + _CAPABILITY_BLANKS + _NON_LOC_KEYS
)

# 引擎派生 / 保留名，extra_fields 不接受
_RESERVED_FIELDS = {
    "open_hfq", "high_hfq", "low_hfq", "close_hfq", "pct_chg",  # 引擎精确派生
    "idx_ret", "log_mktcap", "industry",                        # 因子伪列
    "symbol", "trade_date",                                     # 索引键
}


class GenericSQLBackend(DataBackend):
    """通用 SQLite 后端：填表声明数据位置，行为与手写 SQL 后端一致。"""

    def __init__(self, form: dict, db_path: str):
        self._c = _compile_form(form)
        # 鸭子类型能力方法一次性装配为实例属性（hasattr/getattr 探测路径
        # 与普通方法一致，静态检查可见）；类 MRO 已定义（子类覆盖）的跳过
        for meth, sec in _EXTRAS.items():
            if sec in self._c["sections"] and getattr(type(self), meth, None) is None:
                setattr(self, meth, getattr(self, f"_impl_{meth}"))
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._check_schema()
        self._div_idx: dict[str, dict] | None = None
        # PERF-08：可选分红查询窗口（ex_date BETWEEN start AND end，YYYYMMDD）；
        # 引擎 prepare() 开头经 getattr 探测调用。未设置时 _div_idx 首次构建
        # 保持全表加载现状
        self._div_bounds: tuple[str, str] | None = None

    def set_dividend_bounds(self, start: str, end: str) -> None:
        """设置分红查询窗口（ex_date BETWEEN start AND end，YYYYMMDD）。

        首次构建 _div_idx 时按窗口剪枝，避免整张 dividend 表全量拉入内存；
        未调用时行为与现状完全一致。引擎在 prepare() 开头调用，应传入回测
        [start, end] 区间。若 _div_idx 已构建，本调用不重建索引（窗口应
        在任何 get_dividends_on_date 之前设置）。
        """
        self._div_bounds = (start, end)

    def close(self):
        self._conn.close()

    # ═══════════════════════════════════
    # 核心 — DataBackend ABC 方法
    # ═══════════════════════════════════

    def query_bars(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        c = self._c
        all_fields = {k for cols in c["panel"].values() for k in cols}
        wanted = all_fields if columns is None else set(columns)
        unknown = wanted - all_fields
        if unknown:
            raise ValueError(
                f"query_bars 未知列名: {sorted(unknown)}（未在表单中声明）"
            )
        if symbols is not None and not symbols:
            # 空列表 ≠ None（全市场）：显式空 universe 返回空面板
            idx = pd.MultiIndex.from_arrays([[], []], names=["trade_date", "symbol"])
            return pd.DataFrame(index=idx)
        frames = []
        for table, cols in c["panel"].items():
            need = {canon: phys for canon, phys in cols.items() if canon in wanted}
            if not need:
                continue  # 列裁剪：该表无字段被请求，整表跳过
            f = self._query_table(table, need, symbols, start, end)
            if f.index.has_duplicates:
                # 重复键会让 outer join 多对多爆炸、策略层 to_dict 静默丢行：
                # 必须在回源处 fail-fast（AGENTS.md 不产生静默错误结果）
                sample = f.index[f.index.duplicated()][:3].tolist()
                raise ValueError(
                    f"表 {table} 存在重复的 (交易日, 代码) 键，示例 {sample}："
                    "请检查源表数据或 tables 节的键列名声明"
                )
            frames.append(f)
        if not frames:
            idx = pd.MultiIndex.from_arrays([[], []], names=["trade_date", "symbol"])
            return pd.DataFrame(index=idx)
        # 无主表：所有被引用表的 (交易日, 代码) 键外并集拼成网格
        df = frames[0]
        for f in frames[1:]:
            df = df.join(f, how="outer")
        return df.sort_index()

    def get_calendar(self, start: str, end: str) -> list[str]:
        sec = self._c["sections"]["calendar"]
        dcol = _q(sec["date"])
        frag, fparams = self._filter_sql(sec["table"])
        where = f"({frag}) AND " if frag else ""
        rows = self._conn.execute(
            f"SELECT {dcol} FROM {_q(sec['table'])}"
            f" WHERE {where}{dcol} >= ? AND {dcol} <= ? ORDER BY {dcol}",
            (*fparams, start, end),
        ).fetchall()
        return [r[0] for r in rows]

    def get_dividends_on_date(self, date_str: str) -> dict:
        if self._div_idx is None:
            sec = self._c["sections"]["dividends"]
            sym, _ = self._keys(sec["table"])
            frag, fparams = self._filter_sql(sec["table"])
            # PERF-08：有窗口时按 ex_date 剪枝（ex_date 列名来自表单
            # dividend_ex_date 配置，经 _q() 引用真实物理列名）
            where_parts = [frag] if frag else []
            bounds_params: list = []
            if self._div_bounds is not None:
                where_parts.append(f"{_q(sec['ex_date'])} BETWEEN ? AND ?")
                bounds_params = [self._div_bounds[0], self._div_bounds[1]]
            where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
            table = sec["table"]
            # end_date/ann_date 是 tushare 表列（2026-08 重建为多阶段公告表后用于
            # 事件级归并）；通用后端无这两列时退化到值级归并
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({_q(table)})")}
            has_event_cols = "end_date" in cols and "ann_date" in cols
            sel_extra = ", end_date AS endd, ann_date AS annd" if has_event_cols else ""
            # ORDER BY ann_date DESC：ann_date 为 NULL 的行（老分红/阶段公告缺失）
            # 在 SQLite 中排序最小，排最后——同组取一/取最新时优先有公告日的行
            order_by = " ORDER BY ann_date DESC" if has_event_cols else ""
            rows = self._conn.execute(
                f"SELECT {_q(sym)} AS sym, {_q(sec['stk_div'])} AS stk,"
                f" {_q(sec['cash_div'])} AS cash, {_q(sec['ex_date'])} AS ex"
                f"{sel_extra}"
                f" FROM {_q(table)}{where}{order_by}",
                (*fparams, *bounds_params),
            ).fetchall()
            if has_event_cols:
                self._div_idx = self._build_div_idx_event(rows)
            else:
                self._div_idx = self._build_div_idx_value(rows)
        return self._div_idx.get(date_str, {})

    @staticmethod
    def _build_div_idx_value(rows):
        """值级归并（无 end_date/ann_date 列的通用后端）：
        同 (ex_date, symbol) 多行全等=重复发布取一，异值=叠加方案求和。"""
        idx = {}
        for r in rows:
            div = {"stk_div": r["stk"] or 0.0, "cash_div": r["cash"] or 0.0}
            bucket = idx.setdefault(r["ex"], {})
            prev = bucket.get(r["sym"])
            if prev is None:
                bucket[r["sym"]] = div
            elif prev == div:
                continue
            else:
                logger.warning(
                    "dividend 表 %s %s 同除权日多行异值，按叠加求和: %s + %s",
                    r["ex"], r["sym"], prev, div,
                )
                bucket[r["sym"]] = {
                    "stk_div": prev["stk_div"] + div["stk_div"],
                    "cash_div": prev["cash_div"] + div["cash_div"],
                }
        return idx

    @staticmethod
    def _build_div_idx_event(rows):
        """事件级归并（tushare 多阶段公告表，行已按 ann_date DESC 排序）：
        - 同 (ex_date, symbol, end_date) → 同事件重复/修订公告，取 ann_date 最新
        - 不同 end_date 但值全等           → 同事件重复记录（end_date 漂移），取一
        - 不同 end_date 且值不同           → 多报告期分红同日实施（叠加事件，求和）
        桶内部携带 endds（报告期集合）参与归并，最后剥离只留消费契约两键。"""
        idx = {}
        for r in rows:
            div = {"stk_div": r["stk"] or 0.0, "cash_div": r["cash"] or 0.0,
                   "endd": r["endd"]}
            bucket = idx.setdefault(r["ex"], {})
            prev = bucket.get(r["sym"])
            if prev is None:
                bucket[r["sym"]] = {"stk_div": div["stk_div"],
                                     "cash_div": div["cash_div"],
                                     "endds": {div["endd"]}}
            elif div["endd"] in prev["endds"] or (
                    prev["stk_div"] == div["stk_div"]
                    and prev["cash_div"] == div["cash_div"]):
                prev["endds"].add(div["endd"])
            else:
                logger.warning(
                    "dividend 表 %s %s 同除权日多事件异值，按叠加求和: %s + %s",
                    r["ex"], r["sym"],
                    {k: prev[k] for k in ("stk_div", "cash_div")},
                    {k: div[k] for k in ("stk_div", "cash_div")},
                )
                prev["stk_div"] += div["stk_div"]
                prev["cash_div"] += div["cash_div"]
                prev["endds"].add(div["endd"])
        return {
            d: {s: {k: v[k] for k in ("stk_div", "cash_div")}
                for s, v in b.items()}
            for d, b in idx.items()
        }

    # ═══════════════════════════════════
    # 鸭子类型扩展实现（经 __getattr__ 按已填的空装配）
    # ═══════════════════════════════════

    def _impl_get_benchmark_bars(
        self, code: str | None = None, start: str = "", end: str = ""
    ) -> pd.DataFrame | None:
        sec = self._c["sections"]["benchmark"]
        code = code or sec.get("code")
        if not code:
            raise ValueError("未填 benchmark_code（默认基准代码），调用需显式传 code")
        csym, cdate = self._keys(sec["close_table"])
        df = pd.read_sql_query(
            f"SELECT {_q(cdate)} AS trade_date, {_q(sec['close'])} AS close"
            f" FROM {_q(sec['close_table'])}"
            f" WHERE {_q(csym)}=? AND {_q(cdate)}>=? AND {_q(cdate)}<=?"
            f" ORDER BY {_q(cdate)}",
            self._conn, params=[code, start, end],
        )
        if df.empty:
            return None
        if sec["adj_table"] is None:
            # 未填 benchmark_adj_factor（如指数点位基准，无复权概念）：直接用 close
            merged = df
            merged["hfq_close"] = merged["close"]
        else:
            asym, adate = self._keys(sec["adj_table"])
            adj = pd.read_sql_query(
                f"SELECT {_q(adate)} AS trade_date, {_q(sec['adj'])} AS adj_factor"
                f" FROM {_q(sec['adj_table'])}"
                f" WHERE {_q(asym)}=? AND {_q(adate)}>=? AND {_q(adate)}<=?"
                f" ORDER BY {_q(adate)}",
                self._conn, params=[code, start, end],
            )
            merged = df.merge(adj, on="trade_date", how="left")
            if len(adj) == 0:
                # adj 表查空时 merged["adj_factor"] 全 NaN；退化为未复权 close
                logger.warning("%s 无 %s 复权因子，benchmark 退化为未复权 close",
                               sec["adj_table"], code)
                merged["hfq_close"] = merged["close"]
            else:
                # EDGE-11：adj 表部分日期缺失 → merged adj_factor NaN → hfq_close
                # NaN 洞 → 下游 pct_change/dropna 静默出洞；检测占比并告警。
                # 全空路径已在上方告警，这里只覆盖部分缺失（不重复告警）
                missing_adj = merged["adj_factor"].isna()
                missing_n = int(missing_adj.sum())
                if missing_n > 0:
                    miss_dates = merged.loc[missing_adj, "trade_date"]
                    logger.warning(
                        "benchmark %s 复权因子缺失 %d/%d 日（%s ~ %s），"
                        "hfq_close 将出现 NaN 洞",
                        code, missing_n, len(merged),
                        miss_dates.min(), miss_dates.max(),
                    )
                # hfq 锚定区间首日：close * adj_factor / first_adj
                first_adj = adj.iloc[0]["adj_factor"]
                merged["hfq_close"] = (
                    merged["close"] * merged["adj_factor"] / first_adj
                )
        merged["trade_date"] = pd.to_datetime(merged["trade_date"])
        merged.set_index("trade_date", inplace=True)
        return merged[["hfq_close"]]

    def _impl_get_st_map(self, from_date: str) -> dict[str, set[str]]:
        sec = self._c["sections"]["st"]
        _, dcol = self._keys(sec["table"])
        frag, fparams = self._filter_sql(sec["table"])
        where = f" AND {frag}" if frag else ""
        rows = self._conn.execute(
            f"SELECT {_q(sec['symbol'])}, {_q(dcol)} FROM {_q(sec['table'])}"
            f" WHERE {_q(dcol)} >= ?{where} ORDER BY {_q(dcol)}",
            (from_date, *fparams),
        ).fetchall()
        result: dict[str, set[str]] = {}
        for r in rows:
            result.setdefault(r[1], set()).add(r[0])
        return result

    def _impl_get_stock_industries(self, ts_codes: list[str]) -> dict[str, str]:
        if not ts_codes:
            return {}
        sec = self._c["sections"]["industry"]
        sym, _ = self._keys(sec["table"])
        ph = ",".join("?" * len(ts_codes))
        rows = self._conn.execute(
            f"SELECT {_q(sym)}, {_q(sec['name'])} FROM {_q(sec['table'])}"
            f" WHERE {_q(sym)} IN ({ph})",
            ts_codes,
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _impl_get_recent_listings(
        self, cutoff_days: int = 60, as_of: str | None = None
    ) -> set[str]:
        sec = self._c["sections"]["listings"]
        sym, _ = self._keys(sec["table"])
        lcol = _q(sec["list_date"])
        if as_of is None:
            as_of = date.today().strftime("%Y%m%d")
        cutoff = (date.fromisoformat(as_of) - timedelta(days=cutoff_days)).strftime("%Y%m%d")
        rows = self._conn.execute(
            f"SELECT {_q(sym)} FROM {_q(sec['table'])}"
            f" WHERE {lcol} >= ? AND {lcol} <= ?",
            (cutoff, as_of),
        ).fetchall()
        return {r[0] for r in rows}

    def _impl_get_index_members(
        self, index_codes: list[str], start: str, end: str
    ) -> dict[str, set[str]]:
        if not index_codes:
            return {}
        sec = self._c["sections"]["index_members"]
        _, dcol = self._keys(sec["table"])
        icol, mcol = sec["index"], sec["member"]
        frag, fparams = self._filter_sql(sec["table"])
        where = f" AND {frag}" if frag else ""
        ph = ",".join("?" * len(index_codes))
        rows = self._conn.execute(
            f"SELECT {_q(dcol)}, {_q(mcol)} FROM {_q(sec['table'])}"
            f" WHERE {_q(icol)} IN ({ph}) AND {_q(dcol)} >= ? AND {_q(dcol)} <= ?{where}"
            f" ORDER BY {_q(dcol)}",
            (*index_codes, start, end, *fparams),
        ).fetchall()
        result: dict[str, set[str]] = {}
        for r in rows:
            result.setdefault(r[0], set()).add(r[1])
        return result

    # ═══════════════════════════════════
    # 内部
    # ═══════════════════════════════════

    def _keys(self, table: str) -> tuple[str, str]:
        """表的 (代码列, 日期列)：table_keys 覆盖，缺省用全局声明的键列名。"""
        return self._c["keys"].get(table, (self._c["ksym"], self._c["kdate"]))

    def _filter_sql(self, table: str) -> tuple[str, list]:
        """filters / filters_sql 编译为 (WHERE 片段, 参数)；None → IS NOT NULL。
        filter 的列名自动加引号；filter_sql 是用户手写的 SQL 原文，不处理。"""
        c = self._c
        frag, params = [], []
        for col, val in (c["filters"].get(table) or {}).items():
            if val is None:
                frag.append(f"{_q(col)} IS NOT NULL")
            else:
                frag.append(f"{_q(col)} = ?")
                params.append(val)
        sql = c["filters_sql"].get(table)
        if sql:
            frag.append(f"({sql})")
        return " AND ".join(frag), params

    def _query_table(
        self,
        table: str,
        need: dict[str, str],
        symbols: list[str] | None,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """加载单张表中被请求的列，返回 MultiIndex (trade_date, symbol)。"""
        ksym, kdate = (_q(k) for k in self._keys(table))
        select = [f"{ksym} AS symbol", f"{kdate} AS trade_date"]
        select += [
            _q(phys) if phys == canon else f"{_q(phys)} AS {_q(canon)}"
            for canon, phys in need.items()
        ]
        if symbols is not None:
            ph = ",".join("?" * len(symbols))
            where = f"{ksym} IN ({ph}) AND "
            params: list = list(symbols)
        else:
            where, params = "", []
        params += [start, end]
        df = pd.read_sql_query(
            f"SELECT {', '.join(select)} FROM {_q(table)}"
            f" WHERE {where}{kdate} >= ? AND {kdate} <= ?",
            self._conn, params=params,
        )
        # read_sql_query 即使 0 行也返回完整列，直接 set_index
        return df.set_index(["trade_date", "symbol"])

    def _check_schema(self):
        """表单引用的表与列全部落库校验（物理表或 VIEW 均可），
        初始化期暴露拼写错误，报错定位到表单条目。"""
        c = self._c
        refs: dict[str, dict[str, str]] = {}  # 表 -> {列: 表单出处}

        def ref(table: str, col: str, desc: str):
            refs.setdefault(table, {})[col] = desc

        for table, cols in c["panel"].items():
            ksym, kdate = self._keys(table)
            ref(table, ksym, f"表 {table} 的代码列")
            ref(table, kdate, f"表 {table} 的日期列")
            for canon, phys in cols.items():
                ref(table, phys, f"字段 {canon!r}")
        secs = c["sections"]
        cal = secs["calendar"]
        ref(cal["table"], cal["date"], "'calendar_date'")
        div = secs["dividends"]
        dsym, _ = self._keys(div["table"])
        ref(div["table"], dsym, f"表 {div['table']} 的代码列")
        for key, blank in (("ex_date", "dividend_ex_date"),
                           ("stk_div", "dividend_stk_div"),
                           ("cash_div", "dividend_cash_div")):
            ref(div["table"], div[key], f"'{blank}'")
        if "st" in secs:
            sec = secs["st"]
            _, sdate = self._keys(sec["table"])
            ref(sec["table"], sec["symbol"], "'st_symbol'")
            ref(sec["table"], sdate, f"表 {sec['table']} 的日期列")
        if "industry" in secs:
            sec = secs["industry"]
            isym, _ = self._keys(sec["table"])
            ref(sec["table"], isym, f"表 {sec['table']} 的代码列")
            ref(sec["table"], sec["name"], "'industry_name'")
        if "listings" in secs:
            sec = secs["listings"]
            lsym, _ = self._keys(sec["table"])
            ref(sec["table"], lsym, f"表 {sec['table']} 的代码列")
            ref(sec["table"], sec["list_date"], "'listing_date'")
        if "index_members" in secs:
            sec = secs["index_members"]
            _, xdate = self._keys(sec["table"])
            ref(sec["table"], xdate, f"表 {sec['table']} 的日期列")
            ref(sec["table"], sec["index"], "'index_code'")
            ref(sec["table"], sec["member"], "'index_member'")
        if "benchmark" in secs:
            sec = secs["benchmark"]
            csym, cdate = self._keys(sec["close_table"])
            bench_refs = [
                (sec["close_table"], csym, "benchmark 代码列"),
                (sec["close_table"], cdate, "benchmark 日期列"),
                (sec["close_table"], sec["close"], "'benchmark_close'"),
            ]
            if sec["adj_table"] is not None:
                asym, adate = self._keys(sec["adj_table"])
                bench_refs += [
                    (sec["adj_table"], asym, "benchmark 代码列"),
                    (sec["adj_table"], adate, "benchmark 日期列"),
                    (sec["adj_table"], sec["adj"], "'benchmark_adj_factor'"),
                ]
            for tbl, col, desc in bench_refs:
                ref(tbl, col, desc)
        for table, fcols in c["filters"].items():
            for col in fcols:
                ref(table, col, f"filters[{table!r}]")

        ph = ",".join("?" * len(refs))
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master"
            f" WHERE type IN ('table', 'view') AND name IN ({ph})",
            sorted(refs),
        ).fetchall()
        missing_tables = set(refs) - {r[0] for r in rows}
        if missing_tables:
            raise ValueError(f"表单引用的表在库中不存在: {sorted(missing_tables)}")
        bad_cols = []
        for table, cols in refs.items():
            have = {
                r[0] for r in self._conn.execute(
                    # pragma 只收字符串字面量：单引号双写转义
                    f"SELECT name FROM pragma_table_info('{table.replace("'", "''")}')"
                )
            }
            for col, desc in cols.items():
                if col not in have:
                    bad_cols.append(f"{table}.{col}（{desc}）")
        if bad_cols:
            raise ValueError(f"表单引用的列在库中不存在: {bad_cols}")
        self._check_key_types()

    def _check_key_types(self):
        """抽样验证键列存储类型：日期列须为 YYYYMMDD 文本，代码列须为 TEXT。

        SQLite 类型序 INTEGER < TEXT：键列存成 INTEGER 时与文本参数的比较恒假，
        面板会静默查空（每日"无行情数据"但回测照常跑完）。每列取首个非 NULL
        样本探一次；空表无样本可验，跳过（数据缺失在运行期以显式告警呈现）。
        """
        c = self._c
        probes: list[tuple[str, str, str]] = []  # (表, 列, "date" | "text")

        def probe(table: str, col: str, kind: str):
            item = (table, col, kind)
            if item not in probes:
                probes.append(item)

        for table in c["panel"]:
            ksym, kdate = self._keys(table)
            probe(table, ksym, "text")
            probe(table, kdate, "date")
        secs = c["sections"]
        cal = secs["calendar"]
        probe(cal["table"], cal["date"], "date")
        div = secs["dividends"]
        dsym, _ = self._keys(div["table"])
        probe(div["table"], dsym, "text")
        probe(div["table"], div["ex_date"], "date")
        if "st" in secs:
            sec = secs["st"]
            _, sdate = self._keys(sec["table"])
            probe(sec["table"], sec["symbol"], "text")
            probe(sec["table"], sdate, "date")
        if "industry" in secs:
            sec = secs["industry"]
            isym, _ = self._keys(sec["table"])
            probe(sec["table"], isym, "text")
        if "listings" in secs:
            sec = secs["listings"]
            lsym, _ = self._keys(sec["table"])
            probe(sec["table"], lsym, "text")
            probe(sec["table"], sec["list_date"], "date")
        if "index_members" in secs:
            sec = secs["index_members"]
            _, xdate = self._keys(sec["table"])
            probe(sec["table"], xdate, "date")
            probe(sec["table"], sec["index"], "text")
            probe(sec["table"], sec["member"], "text")
        if "benchmark" in secs:
            sec = secs["benchmark"]
            csym, cdate = self._keys(sec["close_table"])
            probe(sec["close_table"], csym, "text")
            probe(sec["close_table"], cdate, "date")
            if sec["adj_table"] is not None:
                asym, adate = self._keys(sec["adj_table"])
                probe(sec["adj_table"], asym, "text")
                probe(sec["adj_table"], adate, "date")

        for table, col, kind in probes:
            row = self._conn.execute(
                f"SELECT typeof({_q(col)}), {_q(col)} FROM {_q(table)}"
                f" WHERE {_q(col)} IS NOT NULL LIMIT 1"
            ).fetchone()
            if row is None:
                continue
            dtype, value = row
            if kind == "date":
                ok = (dtype == "text" and isinstance(value, str)
                      and len(value) == 8 and value.isdigit())
                expect = "YYYYMMDD 文本（8 位数字）"
            else:
                ok = dtype == "text"
                expect = "TEXT"
            if not ok:
                raise ValueError(
                    f"表 {table} 的列 {col} 应为{expect}，实测类型 {dtype} 值 {value!r}："
                    "SQLite 中 INTEGER < TEXT，类型不匹配的键比较恒假，查询会静默查空"
                )


def _parse_key_name(form: dict, name: str) -> str:
    """解析通用查询键为纯列名（不带表名；它声明的是各表共同的键列名字）。"""
    value = form[name]
    if not isinstance(value, str) or not value.strip() or "." in value:
        raise ValueError(
            f"表单 {name!r} 只需填列名（各表共同的键列名字，不带表名），实际值: {value!r}"
        )
    return value


def _parse_loc(form: dict, name: str) -> tuple[str, str] | None:
    """解析位置空为 (表名, 字段名)；未填返回 None。"""
    value = form.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or "." not in value:
        raise ValueError(f"表单 {name!r} 需填 '表名.字段名'，实际值: {value!r}")
    table, _, col = value.partition(".")
    if not table or not col:
        raise ValueError(f"表单 {name!r} 需填 '表名.字段名'，实际值: {value!r}")
    return table, col


def _compile_form(form: dict) -> dict:
    """校验并编译表单为内部表示（不修改入参）；错误信息定位到表单条目。"""
    if not isinstance(form, dict):
        raise ValueError("表单必须是 dict（填表模板见 docs/backend_guide.md）")
    unknown = set(form) - _TOP_KEYS
    if unknown:
        raise ValueError(
            f"表单存在未知键 {sorted(unknown)}，请对照 docs/backend_guide.md 的模板"
        )
    for name in _KEY_BLANKS + _REQUIRED_LOC_BLANKS:
        if form.get(name) is None:
            raise ValueError(f"表单缺少必需项 {name!r}")

    locs = {}  # 空名 -> (table, col)
    declared_tables = set()
    for name in _REQUIRED_LOC_BLANKS + _CAPABILITY_BLANKS:
        loc = _parse_loc(form, name)
        if loc:
            locs[name] = loc
            declared_tables.add(loc[0])
    ksym = _parse_key_name(form, "symbol")
    kdate = _parse_key_name(form, "date")

    # panel 字段（契约 10 + 自选扩展）按表分组，无主/辅之分
    extra = form.get("extra_fields") or {}
    if not isinstance(extra, dict):
        raise ValueError("'extra_fields' 必须是 {字段名: '表名.字段名'} 的 dict")
    panel: dict[str, dict[str, str]] = {}
    for canon in _CONTRACT_FIELDS:
        table, col = locs[canon]
        panel.setdefault(table, {})[canon] = col
    for canon, value in extra.items():
        if canon in _RESERVED_FIELDS or canon in _TOP_KEYS:
            raise ValueError(f"extra_fields[{canon!r}] 是引擎派生/保留名，不应填写")
        table, col = _parse_loc(extra, canon)
        panel.setdefault(table, {})[canon] = col
        declared_tables.add(table)

    # 键列名覆盖与行筛选在 sections 组装后统一从 "tables" 节解析（见下）

    # 角色节：从扁平位置空组装
    sections: dict[str, dict] = {
        "calendar": {"table": locs["calendar_date"][0], "date": locs["calendar_date"][1]},
    }
    div_tables = {locs[n][0] for n in
                  ("dividend_ex_date", "dividend_stk_div", "dividend_cash_div")}
    if len(div_tables) != 1:
        raise ValueError("dividend_ex_date / dividend_stk_div / dividend_cash_div"
                         " 必须在同一张表")
    sections["dividends"] = {
        "table": locs["dividend_ex_date"][0],
        "ex_date": locs["dividend_ex_date"][1],
        "stk_div": locs["dividend_stk_div"][1],
        "cash_div": locs["dividend_cash_div"][1],
    }
    if "st_symbol" in locs:
        sections["st"] = {"table": locs["st_symbol"][0], "symbol": locs["st_symbol"][1]}
    if "industry_name" in locs:
        sections["industry"] = {"table": locs["industry_name"][0],
                                "name": locs["industry_name"][1]}
    if "listing_date" in locs:
        sections["listings"] = {"table": locs["listing_date"][0],
                                "list_date": locs["listing_date"][1]}
    idx_pair = ("index_code" in locs) + ("index_member" in locs)
    if idx_pair == 1:
        raise ValueError("'index_code' 与 'index_member' 必须成对填写")
    if idx_pair == 2:
        if locs["index_code"][0] != locs["index_member"][0]:
            raise ValueError("'index_code' 与 'index_member' 必须在同一张表")
        sections["index_members"] = {"table": locs["index_code"][0],
                                     "index": locs["index_code"][1],
                                     "member": locs["index_member"][1]}
    if "benchmark_adj_factor" in locs and "benchmark_close" not in locs:
        raise ValueError("'benchmark_adj_factor' 不能脱离 'benchmark_close' 单独填写")
    if "benchmark_close" in locs:
        code = form.get("benchmark_code")
        if code is not None and not isinstance(code, str):
            raise ValueError("'benchmark_code' 必须是基准代码字符串")
        # benchmark_adj_factor 可选：指数点位等无复权概念的基准只填 close 即可
        has_adj = "benchmark_adj_factor" in locs
        sections["benchmark"] = {
            "close_table": locs["benchmark_close"][0],
            "close": locs["benchmark_close"][1],
            "adj_table": locs["benchmark_adj_factor"][0] if has_adj else None,
            "adj": locs["benchmark_adj_factor"][1] if has_adj else None,
            "code": code,
        }

    # 沉底："tables" 节 —— 表的特殊说明（位置之外）
    # filter/filter_sql 只对角色表（日历/分红/ST/指数成分）有效；
    # symbol/date（键列名覆盖）对任何已声明表有效
    for name, sec in sections.items():
        if name == "benchmark":
            declared_tables.add(sec["close_table"])
            if sec["adj_table"] is not None:
                declared_tables.add(sec["adj_table"])
        else:
            declared_tables.add(sec["table"])
    role_tables = {
        sections[n]["table"]
        for n in ("calendar", "dividends", "st", "index_members") if n in sections
    }
    tables_opt = form.get("tables") or {}
    if not isinstance(tables_opt, dict):
        raise ValueError("'tables' 必须是 {表名: {...}} 的 dict")
    stray = set(tables_opt) - declared_tables
    if stray:
        raise ValueError(f"tables 中的表未被任何字段引用: {sorted(stray)}")
    keys: dict[str, tuple[str, str]] = {}
    filters: dict[str, dict] = {}
    filters_sql: dict[str, str] = {}
    for table, opt in tables_opt.items():
        if not isinstance(opt, dict):
            raise ValueError(f"tables[{table!r}] 必须是 dict")
        bad = set(opt) - {"filter", "filter_sql", "symbol", "date"}
        if bad:
            raise ValueError(
                f"tables[{table!r}] 存在未知键 {sorted(bad)}"
                "（只接受 filter/filter_sql/symbol/date）"
            )
        if ("filter" in opt or "filter_sql" in opt) and table not in role_tables:
            raise ValueError(
                f"tables[{table!r}] 的 filter 只对日历/分红/ST/指数成分表有效"
            )
        if "symbol" in opt or "date" in opt:
            keys[table] = (opt.get("symbol", ksym), opt.get("date", kdate))
        if "filter" in opt:
            if not isinstance(opt["filter"], dict):
                raise ValueError(f"tables[{table!r}] 的 'filter' 必须是 {{列: 值}} dict")
            filters[table] = opt["filter"]
        if "filter_sql" in opt:
            if not isinstance(opt["filter_sql"], str):
                raise ValueError(f"tables[{table!r}] 的 'filter_sql' 必须是 SQL 字符串")
            filters_sql[table] = opt["filter_sql"]

    return {
        "ksym": ksym, "kdate": kdate,
        "panel": panel, "keys": keys,
        "filters": filters, "filters_sql": filters_sql,
        "sections": sections,
    }
