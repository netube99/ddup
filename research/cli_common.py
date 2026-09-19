"""CLI 公共样板 — provider 构建、run_id 解析与用户输入错误通道。

research 层可 import 的薄 helper，供 scripts/* 薄壳 CLI 共享，
消除各入口重复的 backend 构建与 run_id 解析样板。
"""

import importlib
import os
import sys

import duckdb

DEFAULT_BACKEND = "adapters.tushare:TushareBackend"


def fail(message: str) -> int:
    """用户输入错误的统一通道：打印到 stderr，返回退出码 1。"""
    print(f"错误：{message}", file=sys.stderr)
    return 1


def validate_date_range(start: str, end: str) -> str | None:
    """校验 YYYYMMDD 起止区间；合法返回 None，否则返回错误消息。"""
    for label, value in (("--start", start), ("--end", end)):
        if len(value) != 8 or not value.isdigit():
            return f"{label} 日期须为 8 位数字 YYYYMMDD，实际: {value!r}"
    if start > end:
        return f"起止日期颠倒：--start {start} > --end {end}"
    return None


def _load_backend():
    """按 DDUP_BACKEND='module:Class' 构建后端实例；缺省 = 股票后端。

    数据源按项目切换：缺省 adapters.tushare:TushareBackend（A 股个股）。
    """
    spec = os.environ.get("DDUP_BACKEND", DEFAULT_BACKEND).strip()
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise ValueError(
            f"DDUP_BACKEND 需为 'module:Class' 格式，实际: {spec!r}"
        )
    module = importlib.import_module(module_name)
    try:
        backend_cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"DDUP_BACKEND 指向的类不存在: {spec}") from exc
    return backend_cls()


def make_provider():
    """构建 DataProvider（懒 import adapters，避免模块级硬依赖）。

    返回 provider；调用方负责关闭：provider.backend.close()。
    """
    from btcore.provider import DataProvider

    return DataProvider(_load_backend())


def latest_run_id(conn: duckdb.DuckDBPyConnection) -> int | None:
    """runs 表最新 run_id；无 run 记录返回 None（无 runs 表抛 CatalogException）。"""
    row = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
    return row[0] if row else None
