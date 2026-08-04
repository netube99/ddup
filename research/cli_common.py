"""CLI 公共样板 — provider 构建与最新 run 解析。

research 层可 import 的薄 helper，供 scripts/* 薄壳 CLI 共享，
消除各入口重复的 backend 构建与 run_id 解析样板。
"""

import sqlite3


def make_provider():
    """构建 DataProvider(TushareBackend)（懒 import adapters，避免模块级硬依赖）。

    返回 provider；调用方负责关闭：provider.backend.close()。
    """
    from adapters.tushare import TushareBackend
    from btcore.provider import DataProvider

    return DataProvider(TushareBackend())


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    """runs 表最新 run_id；无 run 记录返回 None（无 runs 表抛 OperationalError）。"""
    row = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
    return row[0] if row else None
