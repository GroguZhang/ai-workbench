# -*- coding: utf-8 -*-
"""状态文件持久化回归测试（原子写）。

背景：雷达是 24/7 常驻进程，跑在会因为各种原因重启的 NAS 上。
流水文件如果「先截断再写」，进程在两步之间被杀（docker kill / OOM / 断电），
用户攒了几个月的命中历史就只剩一个空文件，而且不可逆。

这组测试的核心是最后那条：**写失败时原文件必须完好无损**。
"""
import builtins
import contextlib
import json

import pytest

import cardhobby_sniper_radar as radar


def _write_jsonl(path, n: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"i": i}, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@contextlib.contextmanager
def _poison_writes(path_tail: str):
    """让「以写模式打开且路径以 path_tail 结尾」的写入在落到磁盘前失败。

    关键点：真实 open 已经执行（因此旧实现的截断已经发生），
    随后写入抛错 —— 精确复现「截断之后、写完之前进程被杀」这一致命窗口。
    """
    real_open = builtins.open

    class Poisoned:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()
            return False

        def write(self, s):
            raise OSError("模拟断电：截断后写入失败")

        def writelines(self, lines):
            raise OSError("模拟断电：截断后写入失败")

    def patched(path, mode="r", *a, **kw):
        fh = real_open(path, mode, *a, **kw)
        if "w" in str(mode) and str(path).endswith(path_tail):
            return Poisoned(fh)
        return fh

    try:
        yield
    finally:
        builtins.open = real_open


def _poison_all_writes(monkeypatch):
    """对 hits/raw 的所有写打开下毒（含 .tmp），确保两种实现都走到失败分支。"""
    real_open = builtins.open

    class Poisoned:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()
            return False

        def write(self, s):
            raise OSError("模拟断电：截断后写入失败")

        def writelines(self, lines):
            raise OSError("模拟断电：截断后写入失败")

    def patched(path, mode="r", *a, **kw):
        fh = real_open(path, mode, *a, **kw)
        text = str(path)
        if "w" in str(mode) and (text.endswith(".jsonl") or text.endswith(".jsonl.tmp")):
            return Poisoned(fh)
        return fh

    monkeypatch.setattr(builtins, "open", patched)


# ---------------------------------------------------------------- 原子写本体

def test_atomic_write_text_roundtrip_and_leaves_no_temp(tmp_path):
    target = tmp_path / "x.jsonl"
    radar._atomic_write_text(str(target), "a\nb\n")
    assert target.read_text(encoding="utf-8") == "a\nb\n"
    assert not (tmp_path / "x.jsonl.tmp").exists(), "临时文件必须被 replace 掉"


def test_atomic_write_json_produces_valid_json(tmp_path):
    target = tmp_path / "s.json"
    radar._atomic_write_json(str(target), {"2026-11-07": {"a": 1}})
    assert json.loads(target.read_text(encoding="utf-8")) == {"2026-11-07": {"a": 1}}
    assert not (tmp_path / "s.json.tmp").exists()


# ---------------------------------------------------------------- hits 裁剪

@pytest.fixture()
def hits_path(tmp_path, monkeypatch):
    p = tmp_path / "hits.jsonl"
    monkeypatch.setattr(radar, "HITS_JSONL_PATH", str(p))
    return p


def test_trim_hits_keeps_most_recent_lines(hits_path, monkeypatch):
    monkeypatch.setattr(radar, "HITS_MAX_LINES", 10)
    monkeypatch.setattr(radar, "HITS_KEEP_LINES", 4)
    _write_jsonl(hits_path, 11)

    radar.trim_hits()

    kept = _read_jsonl(hits_path)
    assert [r["i"] for r in kept] == [7, 8, 9, 10], "应当保留最后 N 行且顺序不变"


def test_trim_hits_is_noop_under_limit(hits_path, monkeypatch):
    monkeypatch.setattr(radar, "HITS_MAX_LINES", 100)
    monkeypatch.setattr(radar, "HITS_KEEP_LINES", 50)
    _write_jsonl(hits_path, 5)

    radar.trim_hits()

    assert len(_read_jsonl(hits_path)) == 5


def test_trim_hits_does_not_destroy_history_when_write_fails(hits_path, monkeypatch):
    """核心回归：裁剪过程中写盘失败，原始流水必须一条不少。

    修复前这里用 open(path, "w") 先截断 —— 只要之后任何一步抛异常，
    hits.jsonl 就永久变成空文件。改用 临时文件 + os.replace 后，
    失败只影响 .tmp，原文件毫发无伤。
    """
    monkeypatch.setattr(radar, "HITS_MAX_LINES", 10)
    monkeypatch.setattr(radar, "HITS_KEEP_LINES", 4)
    _write_jsonl(hits_path, 11)

    _poison_all_writes(monkeypatch)

    radar.trim_hits()   # 内部捕获异常并 warning，不应向外抛

    assert len(_read_jsonl(hits_path)) == 11, "原文件被破坏，历史数据丢失"


# ---------------------------------------------------------------- 候选池裁剪

def test_trim_raw_candidates_keeps_most_recent_lines(tmp_path, monkeypatch):
    p = tmp_path / "raw.jsonl"
    monkeypatch.setattr(radar, "RAW_CANDIDATES_PATH", str(p))
    monkeypatch.setattr(radar, "RAW_CANDIDATES_MAX_LINES", 10)
    monkeypatch.setattr(radar, "RAW_CANDIDATES_KEEP_LINES", 3)
    _write_jsonl(p, 12)

    radar.trim_raw_candidates()

    assert [r["i"] for r in _read_jsonl(p)] == [9, 10, 11]


def test_trim_raw_candidates_survives_write_failure(tmp_path, monkeypatch):
    p = tmp_path / "raw.jsonl"
    monkeypatch.setattr(radar, "RAW_CANDIDATES_PATH", str(p))
    monkeypatch.setattr(radar, "RAW_CANDIDATES_MAX_LINES", 10)
    monkeypatch.setattr(radar, "RAW_CANDIDATES_KEEP_LINES", 3)
    _write_jsonl(p, 12)

    _poison_all_writes(monkeypatch)
    radar.trim_raw_candidates()

    assert len(_read_jsonl(p)) == 12, "原文件被破坏"


# ---------------------------------------------------------------- 快照原子性

def test_snapshot_write_is_atomic(tmp_path, monkeypatch):
    """快照是网页端随时在读的文件，绝不能让读端读到半截 JSON。"""
    p = tmp_path / "snap.json"
    monkeypatch.setattr(radar, "SNAPSHOT_JSON_PATH", str(p))

    radar.record_and_compare_snapshot(
        item_id="X1", scan_mode="1700", title="t", current_price=100.0,
        bid_count=1, remaining_minutes=60.0,
    )

    assert json.loads(p.read_text(encoding="utf-8")), "快照写入后应是合法且非空的 JSON"
    assert not (tmp_path / "snap.json.tmp").exists()
