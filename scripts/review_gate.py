#!/usr/bin/env python3
"""审查门禁：让审查循环收敛（docs/review_protocol.md 的机械执行）。

审查产出必须是 findings YAML，本脚本验收：
- P0/P1（阻塞）→ 门禁 FAIL，唯二出口：修复后 close，或用户 close --waive 记录决策
- P2/P3（非阻塞）→ 自动登记 docs/review_backlog.yaml，不要求本轮修复
- 收敛判据：一轮 check 无未决 P0/P1 且无新增发现 → 该范围审查终止
- done-bar：可用的定义 = 机械检查全绿 + 测试全绿 + 交叉验证/对账干净

用法：
  python scripts/review_gate.py check findings.yaml [--scope full|diff]
  python scripts/review_gate.py backlog [--closed]
  python scripts/review_gate.py close ID [--waive REASON]
  python scripts/review_gate.py done [--full]

退出码：0 = 门禁通过；1 = 存在未决 P0/P1；2 = 输入非法（fail-fast，不产生结论）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKLOG = ROOT / "docs" / "review_backlog.yaml"
SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING = ("P0", "P1")
COOLDOWN_DAYS = 14
FULL_AUDIT_HINT_DAYS = 60

_ID_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FINDING_FIELDS = ("severity", "rule", "file", "line", "note")


class GateError(Exception):
    """findings/backlog 文件格式非法（退出码 2）。"""


def _today() -> str:
    return date.today().isoformat()


def _days_since(iso_date: str) -> int:
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except ValueError as exc:
        raise GateError(f"backlog meta.last_full_audit 格式非法: {iso_date}") from exc


def _blank_backlog() -> dict:
    return {"meta": {}, "open": [], "closed": []}


def load_backlog(path: Path) -> dict:
    if not path.exists():
        return _blank_backlog()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise GateError(f"backlog 文件 {path} 顶层必须是映射")
    if not isinstance(data.get("meta", {}), dict):
        raise GateError(f"backlog 文件 {path} 的 meta 必须是映射")
    for key in ("open", "closed"):
        if not isinstance(data.get(key, []), list):
            raise GateError(f"backlog 文件 {path} 的 {key} 必须是列表")
        data.setdefault(key, [])
    return data


def save_backlog(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 审查 backlog：由 scripts/review_gate.py 自动维护\n"
        "# open = 未决发现（P0/P1 是门禁 FAIL 的来源）；closed = 已修复或已 waive\n"
        "# 手工编辑请保持结构；字段：id/severity/rule/file/line/note/opened/resolved/waived\n"
    )
    path.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def validate_findings(doc: dict, errors: list[str]) -> None:
    """校验 findings 结构；不合法项写入 errors。"""
    meta = doc.get("meta", {})
    if not isinstance(meta, dict) or not isinstance(meta.get("date"), (str, date)):
        errors.append("meta.date 缺失或非日期（YYYY-MM-DD）")
    elif isinstance(meta["date"], str) and not _DATE_RE.match(meta["date"]):
        errors.append(f"meta.date {meta['date']!r} 格式非法（须为 YYYY-MM-DD）")
    findings = doc.get("findings")
    if not isinstance(findings, list):
        errors.append("findings 必须是列表")
        return
    seen: set[str] = set()
    for i, item in enumerate(findings, start=1):
        prefix = f"findings[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: 必须是映射")
            continue
        fid = item.get("id")
        if not isinstance(fid, str) or not _ID_RE.match(fid):
            errors.append(f"{prefix}: id 缺失或格式非法（须为 区段-编号，如 F-EMA-01）")
        elif fid in seen:
            errors.append(f"{prefix}: id {fid} 在本文件内重复")
        else:
            seen.add(fid)
        sev = item.get("severity")
        if sev not in SEVERITIES:
            errors.append(f"{prefix}: severity 必须是 {'/'.join(SEVERITIES)}，实际 {sev!r}")
        if not isinstance(item.get("rule"), str) or not item["rule"].strip():
            errors.append(
                f"{prefix}: rule 缺失——必须引用被违反的已写规则"
                "（AGENTS.md 契约/文档/checklist 条目）"
            )
        file_ = item.get("file")
        if not isinstance(file_, str) or Path(file_).is_absolute() or not (ROOT / file_).is_file():
            errors.append(
                f"{prefix}: file {file_!r} 不存在（须为相对仓库根的路径）"
            )
        line = item.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            errors.append(f"{prefix}: line 必须是正整数")
        if not isinstance(item.get("note"), str) or not item["note"].strip():
            errors.append(f"{prefix}: note 缺失——记录现象与证据")


def cmd_check(args: argparse.Namespace) -> int:
    try:
        doc = yaml.safe_load(args.findings.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"FAIL: findings 文件不可读或非合法 YAML: {exc}")
        return 2
    if not isinstance(doc, dict):
        print("FAIL: findings 顶层必须是映射")
        return 2
    errors: list[str] = []
    validate_findings(doc, errors)
    if errors:
        print("FAIL: findings 不合法（不产生门禁结论）：")
        for e in errors:
            print(f"  - {e}")
        return 2

    backlog = load_backlog(args.backlog)
    open_ids = {item["id"] for item in backlog["open"]}
    closed_by_id = {item["id"]: item for item in backlog["closed"]}
    new_items: list[dict] = []
    skipped: list[str] = []
    for f in doc["findings"]:
        if f["id"] in open_ids:
            skipped.append(f["id"])
        else:
            prev = closed_by_id.get(f["id"])
            if prev is not None and all(
                prev.get(k) == f.get(k) for k in _FINDING_FIELDS
            ):
                skipped.append(f["id"])  # 已处理且内容未变 → 不重开
            else:
                new_items.append({**f, "opened": _today()})
    backlog["open"].extend(new_items)

    if args.scope == "full":
        last = backlog["meta"].get("last_full_audit")
        if last and _days_since(last) < COOLDOWN_DAYS:
            print(f"WARN: 全量审查冷却期未过（上次 {last}）——建议 --scope diff 只审改动")
        backlog["meta"]["last_full_audit"] = _today()
    elif backlog["meta"].get("last_full_audit"):
        days = _days_since(backlog["meta"]["last_full_audit"])
        if days > FULL_AUDIT_HINT_DAYS:
            print(f"HINT: 距上次全量审查 {days} 天，可安排一次 --scope full")
    save_backlog(args.backlog, backlog)

    if skipped:
        print(f"跳过 {len(skipped)} 条（已在 open 或已处理且内容未变）：{', '.join(skipped)}")
    if new_items:
        print(f"登记 {len(new_items)} 条新发现")
    blocking = [item for item in backlog["open"] if item["severity"] in BLOCKING]
    new_blocking = sum(1 for item in new_items if item["severity"] in BLOCKING)
    if blocking:
        print(f"FAIL: 未决阻塞 {len(blocking)} 条（本轮新增 {new_blocking}）：")
        for item in blocking:
            print(f"  - {item['id']} [{item['severity']}] {item['file']}:{item['line']}")
            print(f"      {item['note']}")
        print("处置：修复后 close <ID>；用户决策不修则 close <ID> --waive <原因>")
        return 1
    if new_items:
        print("PASS: 未决阻塞 0 条")
        print("未收敛：连续两轮 check 无 P0/P1 且新增=0 即终止该范围审查")
    else:
        print("PASS: 未决阻塞 0 条，本轮无新增发现")
        print("CONVERGED: 该范围审查已收敛——停止审查，进入 done-bar：")
        print("  python scripts/review_gate.py done")
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    backlog = load_backlog(args.backlog)
    if not backlog["open"] and not (args.closed and backlog["closed"]):
        print("backlog 为空")
        return 0
    print(f"open {len(backlog['open'])} 条：")
    for item in backlog["open"]:
        print(
            f"  - {item['id']} [{item['severity']}] {item['file']}:{item['line']} "
            f"opened={item.get('opened', '?')} — {item['note']}"
        )
    if args.closed and backlog["closed"]:
        print(f"closed {len(backlog['closed'])} 条：")
        for item in backlog["closed"]:
            tag = f" waived: {item['waived']}" if item.get("waived") else ""
            print(
                f"  - {item['id']} [{item['severity']}] resolved={item.get('resolved', '?')}{tag} "
                f"— {item['note']}"
            )
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    backlog = load_backlog(args.backlog)
    for i, item in enumerate(backlog["open"]):
        if item["id"] == args.id:
            item = backlog["open"].pop(i)
            item["resolved"] = _today()
            if args.waive:
                if not args.waive.strip():
                    raise GateError("--waive 需要写明原因（记录用户决策）")
                item["waived"] = args.waive.strip()
            backlog["closed"].append(item)
            save_backlog(args.backlog, backlog)
            action = "waive（用户决策，不修复）" if args.waive else "resolve（已修复）"
            print(f"OK: {args.id} 已{action}")
            return 0
    raise GateError(f"backlog.open 中无 {args.id}（先 check 登记，或检查拼写）")


def cmd_done(args: argparse.Namespace) -> int:
    """done-bar：可用的定义。机械检查自动跑，真实库验收打印指引。"""
    failed = 0
    for name in ("check_anticorrupt.py", "check_skill_sync.py"):
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / name)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        ok = r.returncode == 0
        print(f"[{'PASS' if ok else 'FAIL'}] python scripts/{name}")
        if not ok:
            failed += 1
            for stream in (r.stdout, r.stderr):
                if stream.strip():
                    print(stream.strip()[-1500:])
    if args.full:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        ok = r.returncode == 0
        print(f"[{'PASS' if ok else 'FAIL'}] pytest tests/ -q")
        if not ok:
            failed += 1
            print((r.stdout + r.stderr).strip()[-3000:])
    print("可用的剩余验收（需真实库，手动执行）：")
    print("  python scripts/cross_validate.py <最新 result.duckdb> --strategy <策略名>")
    print("    --run-id 1")
    print("  python scripts/live.py sync live/main.duckdb sync.yaml")
    if failed:
        print(f"done-bar FAIL: {failed} 项失败")
        return 1
    print("done-bar PASS: 机械检查全绿（+测试全绿）；真实库验收通过后即视为可用")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审查门禁：收敛审查循环")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--backlog",
        type=Path,
        default=DEFAULT_BACKLOG,
        help="backlog 文件路径（默认 docs/review_backlog.yaml）",
    )

    p_check = sub.add_parser("check", parents=[common], help="验收 findings 文件并更新 backlog")
    p_check.add_argument("findings", type=Path, help="findings YAML 路径")
    p_check.add_argument(
        "--scope",
        choices=("full", "diff"),
        default="diff",
        help="审查范围；full 记录时间并触发冷却检查",
    )
    p_check.set_defaults(func=cmd_check)

    p_backlog = sub.add_parser("backlog", parents=[common], help="列出未决（open）发现")
    p_backlog.add_argument("--closed", action="store_true", help="同时列出已关闭项")
    p_backlog.set_defaults(func=cmd_backlog)

    p_close = sub.add_parser(
        "close", parents=[common], help="关闭 backlog 项：已修复（默认）或显式放弃"
    )
    p_close.add_argument("id", help="发现 id，如 F-EMA-01")
    p_close.add_argument(
        "--waive",
        metavar="REASON",
        help="放弃修复并记录用户决策原因（P0/P1 唯一非修复出口）",
    )
    p_close.set_defaults(func=cmd_close)

    p_done = sub.add_parser("done", parents=[common], help="done-bar：机械检查 + 可用性验收清单")
    p_done.add_argument("--full", action="store_true", help="同时跑完整 pytest 测试套件")
    p_done.set_defaults(func=cmd_done)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GateError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
