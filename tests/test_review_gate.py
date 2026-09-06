"""审查门禁测试 — findings 验收、backlog 归并、收敛判据与 done-bar。"""

from datetime import date, timedelta

import yaml

from scripts import review_gate as rg


def _findings(items, fdate="2026-08-06"):
    return {"meta": {"date": fdate}, "findings": items}


def _item(fid="T-001", severity="P2", **kw):
    base = {
        "id": fid,
        "severity": severity,
        "rule": "AGENTS.md 设计契约: 价格体系",
        "file": "btcore/types.py",
        "line": 1,
        "note": "现象与证据",
    }
    base.update(kw)
    return base


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return p


def _run(tmp_path, *argv):
    return rg.main([*argv, "--backlog", str(tmp_path / "b.yaml")])


def _load(tmp_path):
    return yaml.safe_load((tmp_path / "b.yaml").read_text(encoding="utf-8"))


def test_check_pass_merges_p2p3_to_backlog(tmp_path, capsys):
    f = _write(
        tmp_path,
        "f.yaml",
        _findings([_item("F-EMA-01"), _item("A-02", severity="P3")]),
    )
    assert _run(tmp_path, "check", str(f)) == 0
    assert "PASS" in capsys.readouterr().out
    assert {i["id"] for i in _load(tmp_path)["open"]} == {"F-EMA-01", "A-02"}


def test_blocking_fails_persists_then_close_passes(tmp_path, capsys):
    f = _write(tmp_path, "f.yaml", _findings([_item("A-01", severity="P1")]))
    assert _run(tmp_path, "check", str(f)) == 1
    assert "FAIL" in capsys.readouterr().out
    assert _load(tmp_path)["open"][0]["severity"] == "P1"
    # 修复后 close → 重跑同一文件：已处理且内容未变，跳过 → 门禁转绿
    assert _run(tmp_path, "close", "A-01") == 0
    assert _run(tmp_path, "check", str(f)) == 0
    out = capsys.readouterr().out
    assert "CONVERGED" in out
    assert _load(tmp_path)["open"] == []


def test_regression_reopens_closed_item(tmp_path):
    f1 = _write(tmp_path, "f1.yaml", _findings([_item("A-01", severity="P2")]))
    assert _run(tmp_path, "check", str(f1)) == 0
    assert _run(tmp_path, "close", "A-01") == 0
    # 同一 id 但严重级升级 → 视为复发，重新 open 且门禁 FAIL
    f2 = _write(tmp_path, "f2.yaml", _findings([_item("A-01", severity="P1")]))
    assert _run(tmp_path, "check", str(f2)) == 1
    assert [i["id"] for i in _load(tmp_path)["open"]] == ["A-01"]


def test_waive_records_user_decision(tmp_path):
    f = _write(tmp_path, "f.yaml", _findings([_item("A-01", severity="P1")]))
    assert _run(tmp_path, "check", str(f)) == 1
    assert _run(tmp_path, "close", "A-01", "--waive", "用户决策：暂不修") == 0
    assert _run(tmp_path, "check", str(f)) == 0  # waive 后同内容不再阻塞
    closed = _load(tmp_path)["closed"]
    assert closed[0]["waived"] == "用户决策：暂不修"


def test_invalid_findings_rejected(tmp_path):
    bad = _write(tmp_path, "f.yaml", _findings([_item(rule="")]))
    assert _run(tmp_path, "check", str(bad)) == 2
    assert not (tmp_path / "b.yaml").exists()  # fail-fast：不落盘


def test_unknown_severity_rejected(tmp_path):
    bad = _write(tmp_path, "f.yaml", _findings([_item(severity="P5")]))
    assert _run(tmp_path, "check", str(bad)) == 2


def test_nonexistent_file_rejected(tmp_path):
    assert _run(tmp_path, "check", str(tmp_path / "nope.yaml")) == 2


def test_bad_line_and_id_rejected(tmp_path):
    bad = _write(tmp_path, "f.yaml", _findings([_item(line=0)]))
    assert _run(tmp_path, "check", str(bad)) == 2
    bad2 = _write(tmp_path, "f2.yaml", _findings([_item(fid="没有连字符")]))
    assert _run(tmp_path, "check", str(bad2)) == 2


def test_duplicate_id_in_file_rejected(tmp_path):
    bad = _write(tmp_path, "f.yaml", _findings([_item("A-01"), _item("A-01")]))
    assert _run(tmp_path, "check", str(bad)) == 2


def test_full_scope_cooldown_warns(tmp_path, capsys):
    backlog = tmp_path / "b.yaml"
    backlog.write_text(
        yaml.safe_dump(
            {"meta": {"last_full_audit": (date.today() - timedelta(days=3)).isoformat()}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    f = _write(tmp_path, "f.yaml", _findings([]))
    assert _run(tmp_path, "check", str(f), "--scope", "full") == 0
    assert "WARN" in capsys.readouterr().out
    assert _load(tmp_path)["meta"]["last_full_audit"] == date.today().isoformat()


def test_done_runs_mechanical_checks(tmp_path):
    assert _run(tmp_path, "done") == 0


def test_close_unknown_id_fails(tmp_path):
    assert _run(tmp_path, "close", "NOPE-01") == 2
