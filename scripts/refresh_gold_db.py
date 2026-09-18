#!/usr/bin/env python3
"""构建/刷新黄金研究数据库 data/gold_market.duckdb（ddup 黄金策略专用后端数据源）。

数据源（只读合并两类）：
  1. tushare_db market.duckdb（fund_factor_pro / fund_adj / fund_share / trade_cal /
     etf_basic）—— 4 只黄金 ETF 的日频行情与份额；
  2. tushare pro API 直拉（stdlib urllib，无需 requests）—— 宏观/外盘对齐序列：
     us_trycr（美债实际收益率）、us_tycr（名义收益率）、fx_daily（XAUUSD / USDCNH，
     FXCM）、sge_daily（上海金 Au99.99）、cn_m（货币供应）。

对齐契约（禁止前视）：
  - 中国市场同交易日数据（ETF 行情、fund_share）当日收盘可用；
  - 美国/外盘序列（美债收益率、XAUUSD、USDCNH）与 SGE 收盘统一按
    "严格早于中国交易日"（merge_asof backward, allow_exact_matches=False）
    对齐——T 日决策只用 T 日之前已收盘的海外数据；
  - cn_m 月频按"次月 1 日可用"对齐。

输出表（data/gold_market.duckdb）：
  gold_panel(ts_code, trade_date, open, high, low, close, pre_close, vol, amount,
             adj_factor, up_limit, down_limit, fd_share,
             xau_usd, usdcnh, us_real_y5, us_real_y10, us_real_y30, us_nom_y10,
             sge_close, m1_yoy, m2_yoy)  PK(ts_code, trade_date)
  trade_cal(exchange, cal_date, is_open, pretrade_date)   -- 从 market.duckdb 复制
  etf_basic(ts_code, cname, index_name, list_date, ...)   -- 黄金 ETF 子集
  dividend(ts_code, ex_date, stk_div, cash_div)           -- 空表（黄金 ETF 无分红，
                                                          -- 满足填表法 13 必需空）

用法：
    python scripts/refresh_gold_db.py [--market-db PATH] [--out PATH] [--since 20030101]

Token 解析顺序：DDUP_TUSHARE_TOKEN 环境变量 → TUSHARE_TOKEN → tushare_db
user_config.yaml 的 tushare_token 字段。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import duckdb
import pandas as pd

from btcore.database import assert_duckdb_file
from btcore.generic_sql import connect_market_db

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MARKET_DB = Path(
    os.environ.get("DDUP_MARKET_DB", "/home/netube/aiwork/tushare_db/data/market.duckdb")
)
DEFAULT_OUT = ROOT / "data" / "gold_market.duckdb"
TUSHARE_URL = "https://api.tushare.pro"

GOLD_SYMBOLS = ["518880.SH", "159934.SZ", "159937.SZ", "518800.SH"]
ETF_PRICE_LIMIT = 0.10  # ETF 涨跌幅限制 10%

US_APIS = ("us_trycr", "us_tycr")
FX_CODES = ("XAUUSD.FXCM", "USDCNH.FXCM")
SGE_CODE = "Au99.99"


def _resolve_token() -> str:
    for env in ("DDUP_TUSHARE_TOKEN", "TUSHARE_TOKEN"):
        tok = os.environ.get(env)
        if tok:
            return tok.strip()
    cfg = Path("/home/netube/aiwork/tushare_db/user_config.yaml")
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("tushare_token:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    raise SystemExit(
        "未找到 tushare token：设置 DDUP_TUSHARE_TOKEN 或 TUSHARE_TOKEN，"
        "或在 tushare_db/user_config.yaml 写入 tushare_token"
    )


class TsClient:
    """最小 tushare pro HTTP 客户端（stdlib urllib + 限速）。"""

    def __init__(self, token: str, interval_ms: int = 200):
        self._token = token
        self._interval = interval_ms / 1000.0
        self._last = 0.0

    def call(self, api: str, **params) -> pd.DataFrame:
        wait = self._interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        body = json.dumps(
            {"api_name": api, "token": self._token, "params": params, "fields": ""}
        ).encode("utf-8")
        req = urllib.request.Request(
            TUSHARE_URL, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self._last = time.time()
        if payload.get("code") != 0:
            raise RuntimeError(f"tushare {api} 失败: {payload.get('code')} {payload.get('msg')}")
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        return pd.DataFrame(items, columns=fields)


def _pull_years(client: TsClient, api: str, since: int, until: int, **extra) -> pd.DataFrame:
    frames = []
    for y in range(since, until + 1):
        df = client.call(
            api, start_date=f"{y}0101", end_date=f"{y}1231", **extra
        )
        if df is not None and len(df):
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_market(conn: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    def q(sql, args=()):
        return conn.execute(sql, list(args)).df()

    ph = ",".join("?" * len(GOLD_SYMBOLS))
    return {
        "prices": q(
            f"SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount "
            f"FROM fund_factor_pro WHERE ts_code IN ({ph})", GOLD_SYMBOLS),
        "adj": q(
            f"SELECT ts_code, trade_date, adj_factor FROM fund_adj "
            f"WHERE ts_code IN ({ph})", GOLD_SYMBOLS),
        "share": q(
            f"SELECT ts_code, trade_date, fd_share FROM fund_share "
            f"WHERE ts_code IN ({ph})", GOLD_SYMBOLS),
        "cal": q("SELECT exchange, cal_date, is_open, pretrade_date FROM trade_cal"),
        "basic": q(
            f"SELECT ts_code, cname, index_code, index_name, list_date, list_status "
            f"FROM etf_basic WHERE ts_code IN ({ph})", GOLD_SYMBOLS),
    }


def _asof_strict(dates: pd.Series, df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """严格早于对齐（backward, 不含相等）——海外收盘晚于中国收盘，T 日只用 <T 的值。

    返回行序与入参 ``dates`` 按位置一一对应（内部按日期排序做 merge_asof，
    随后按原位置复原）——调用方以行拼接宏观列，行序错位会让宏观值静默错配。
    """
    base = pd.DataFrame({
        "trade_date": dates.astype("int64"),
        "_pos": range(len(dates)),
    })
    if df.empty:
        out = base.copy()
        for c in cols:
            out[c] = float("nan")
        out.index = dates.index
        return out[cols]
    d = df.sort_values("trade_date").dropna(subset=["trade_date"]).copy()
    d["trade_date"] = d["trade_date"].astype("int64")
    merged = pd.merge_asof(
        base.sort_values("trade_date"),
        d[["trade_date"] + cols], on="trade_date",
        direction="backward", allow_exact_matches=False,
    )
    merged = merged.sort_values("_pos").drop(columns="_pos")
    merged.index = dates.index
    return merged[cols]


def build(market_db: Path, out_db: Path, since: int, until: int) -> None:
    if not market_db.exists():
        raise SystemExit(f"行情库不存在: {market_db}（上游 tushare_db 是否已迁移到 DuckDB？）")
    src = connect_market_db(str(market_db))
    md = _load_market(src)
    src.close()
    if md["prices"].empty:
        raise SystemExit("market.duckdb 无黄金 ETF 行情数据（fund_factor_pro）")

    token = _resolve_token()
    client = TsClient(token)
    print(f"[1/4] 拉取宏观/外盘序列 {since}~{until} ...")
    trycr = _pull_years(client, "us_trycr", since, until)
    tycr = _pull_years(client, "us_tycr", since, until)
    xau = _pull_years(client, "fx_daily", since, until, ts_code="XAUUSD.FXCM")
    cnh = _pull_years(client, "fx_daily", since, until, ts_code="USDCNH.FXCM")
    sge = _pull_years(client, "sge_daily", since, until, ts_code=SGE_CODE)
    cnm = client.call("cn_m", start_m=f"{since // 100}01", end_m=f"{until // 100}12")
    for name, df in [("us_trycr", trycr), ("us_tycr", tycr), ("xau", xau),
                     ("cnh", cnh), ("sge", sge), ("cn_m", cnm)]:
        print(f"    {name}: {len(df)} 行")

    def _fx_norm(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["trade_date", prefix])
        d = df[["trade_date", "bid_close"]].copy()
        d["trade_date"] = d["trade_date"].astype(str)
        return d.rename(columns={"bid_close": prefix})

    trycr_n = trycr.rename(columns={"date": "trade_date"})[
        ["trade_date", "y5", "y10", "y30"]].rename(
        columns={"y5": "us_real_y5", "y10": "us_real_y10", "y30": "us_real_y30"})
    tycr_n = tycr.rename(columns={"date": "trade_date"})[
        ["trade_date", "y10"]].rename(columns={"y10": "us_nom_y10"})
    sge_n = sge.rename(columns={"close": "sge_close"})[["trade_date", "sge_close"]]
    cnm_n = cnm[["month", "m1_yoy", "m2_yoy"]].copy()
    cnm_n["avail_date"] = (
        pd.to_datetime(cnm_n["month"].astype(str), format="%Y%m") + pd.offsets.MonthBegin(1)
    ).dt.strftime("%Y%m%d")
    cnm_n = cnm_n.rename(columns={"avail_date": "trade_date"})[
        ["trade_date", "m1_yoy", "m2_yoy"]]

    print("[2/4] 对齐到中国交易日（严格早于口径）...")
    panel = md["prices"].merge(md["adj"], on=["ts_code", "trade_date"], how="left")
    panel = panel.merge(md["share"], on=["ts_code", "trade_date"], how="left")
    panel["trade_date"] = panel["trade_date"].astype(str)
    dates = panel["trade_date"]
    panel = pd.concat([
        panel.reset_index(drop=True),
        _asof_strict(dates, _fx_norm(xau, "xau_usd"), ["xau_usd"]).reset_index(drop=True),
        _asof_strict(dates, _fx_norm(cnh, "usdcnh"), ["usdcnh"]).reset_index(drop=True),
        _asof_strict(dates, trycr_n,
                     ["us_real_y5", "us_real_y10", "us_real_y30"]).reset_index(drop=True),
        _asof_strict(dates, tycr_n, ["us_nom_y10"]).reset_index(drop=True),
        _asof_strict(dates, sge_n, ["sge_close"]).reset_index(drop=True),
        _asof_strict(dates, cnm_n, ["m1_yoy", "m2_yoy"]).reset_index(drop=True),
    ], axis=1)
    panel["adj_factor"] = panel["adj_factor"].fillna(1.0)
    panel["up_limit"] = (panel["pre_close"] * (1 + ETF_PRICE_LIMIT)).round(2)
    panel["down_limit"] = (panel["pre_close"] * (1 - ETF_PRICE_LIMIT)).round(2)
    panel = panel[
        ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
         "vol", "amount", "adj_factor", "up_limit", "down_limit", "fd_share",
         "xau_usd", "usdcnh", "us_real_y5", "us_real_y10", "us_real_y30",
         "us_nom_y10", "sge_close", "m1_yoy", "m2_yoy"]
    ].sort_values(["trade_date", "ts_code"])

    print(f"[3/4] 写入 {out_db} ...")
    out_db.parent.mkdir(parents=True, exist_ok=True)
    assert_duckdb_file(str(out_db), "黄金库")
    # 原子替换：先写临时文件，成功后 os.replace——中途失败不破坏现有可用库
    tmp_db = out_db.with_name(out_db.name + ".tmp")
    if tmp_db.exists():
        tmp_db.unlink()
    dst = duckdb.connect(str(tmp_db))
    try:
        dst.execute(
            """
            DROP TABLE IF EXISTS gold_panel;
            CREATE TABLE gold_panel (
                ts_code VARCHAR NOT NULL, trade_date VARCHAR NOT NULL,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
                vol DOUBLE, amount DOUBLE, adj_factor DOUBLE,
                up_limit DOUBLE, down_limit DOUBLE, fd_share DOUBLE,
                xau_usd DOUBLE, usdcnh DOUBLE,
                us_real_y5 DOUBLE, us_real_y10 DOUBLE, us_real_y30 DOUBLE,
                us_nom_y10 DOUBLE, sge_close DOUBLE, m1_yoy DOUBLE, m2_yoy DOUBLE,
                PRIMARY KEY (ts_code, trade_date)
            );
            DROP TABLE IF EXISTS trade_cal;
            CREATE TABLE trade_cal (
                exchange VARCHAR NOT NULL, cal_date VARCHAR NOT NULL,
                is_open BIGINT NOT NULL, pretrade_date VARCHAR,
                PRIMARY KEY (exchange, cal_date)
            );
            DROP TABLE IF EXISTS etf_basic;
            CREATE TABLE etf_basic (
                ts_code VARCHAR PRIMARY KEY, cname VARCHAR, index_code VARCHAR,
                index_name VARCHAR, list_date VARCHAR, list_status VARCHAR
            );
            DROP TABLE IF EXISTS dividend;
            CREATE TABLE dividend (
                ts_code VARCHAR, ex_date VARCHAR, stk_div DOUBLE, cash_div DOUBLE
            );
            DROP TABLE IF EXISTS meta;
            CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR);
            """
        )
        # register + INSERT SELECT：DuckDB 向量化批量装载（pandas to_sql 不可用）。
        # 显式列名：不依赖 DataFrame 列顺序与 DDL 一致，插列时不会静默错列
        _panel_cols = (
            "ts_code, trade_date, open, high, low, close, pre_close, vol, amount,"
            " adj_factor, up_limit, down_limit, fd_share, xau_usd, usdcnh,"
            " us_real_y5, us_real_y10, us_real_y30, us_nom_y10, sge_close,"
            " m1_yoy, m2_yoy"
        )
        _cal_cols = "exchange, cal_date, is_open, pretrade_date"
        _basic_cols = "ts_code, cname, index_code, index_name, list_date, list_status"
        dst.register("_panel_df", panel)
        dst.execute(f"INSERT INTO gold_panel ({_panel_cols})"
                    f" SELECT {_panel_cols} FROM _panel_df")
        dst.unregister("_panel_df")
        dst.register("_cal_df", md["cal"])
        dst.execute(f"INSERT INTO trade_cal ({_cal_cols})"
                    f" SELECT {_cal_cols} FROM _cal_df")
        dst.unregister("_cal_df")
        dst.register("_basic_df", md["basic"])
        dst.execute(f"INSERT INTO etf_basic ({_basic_cols})"
                    f" SELECT {_basic_cols} FROM _basic_df")
        dst.unregister("_basic_df")
        dst.execute(
            "INSERT INTO meta VALUES ('refreshed_at', ?),"
            " ('source_db', ?), ('rows', ?)",
            [datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             str(market_db), str(len(panel))],
        )
        dst.commit()
        idx = dst.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date), COUNT(DISTINCT ts_code) "
            "FROM gold_panel"
        ).fetchone()
    except BaseException:
        dst.close()
        if tmp_db.exists():
            tmp_db.unlink()
        raise
    else:
        dst.close()
        os.replace(tmp_db, out_db)
    print(f"[4/4] 完成：{idx[0]} 行 × {idx[3]} 只，{idx[1]} ~ {idx[2]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新 ddup 黄金研究数据库")
    ap.add_argument("--market-db", default=str(DEFAULT_MARKET_DB), help="tushare_db 行情库路径")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出 gold_market.duckdb 路径")
    ap.add_argument("--since", type=int, default=2003, help="宏观序列起始年")
    ap.add_argument("--until", type=int, default=int(pd.Timestamp.today().strftime("%Y")))
    args = ap.parse_args()
    build(Path(args.market_db), Path(args.out), args.since, args.until)
    return 0


if __name__ == "__main__":
    sys.exit(main())
