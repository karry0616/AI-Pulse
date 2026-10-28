"""运行锁测试：跨进程互斥、异常释放、残留锁抢占。"""

import os
import time

import pytest

import database
import pipeline


@pytest.fixture(autouse=True)
def temp_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / ".pulse.lock")
    yield
    assert not pipeline.LOCK_PATH.exists(), "测试结束后锁文件应被清理"


def test_second_acquire_is_rejected():
    with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
        with pytest.raises(pipeline.RunLockedError):
            with pipeline.run_lock("AI Agent", "schedule", log=lambda m: None):
                pass


def test_lock_released_after_exception():
    with pytest.raises(ValueError):
        with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
            raise ValueError("boom")
    assert not pipeline.LOCK_PATH.exists()
    with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
        pass


def test_stale_lock_is_recovered():
    pipeline.LOCK_PATH.write_text('{"pid": 999999}', encoding="utf-8")
    old = time.time() - pipeline.LOCK_STALE_SECONDS - 10
    os.utime(pipeline.LOCK_PATH, (old, old))

    with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
        pass


def test_dead_pid_lock_is_reclaimed_without_waiting():
    """持有进程已退出时立即接管，不再空等 30 分钟。"""
    pipeline.LOCK_PATH.write_text(
        '{"pid": 999999, "keyword": "AI Agent", "trigger": "manual"}', encoding="utf-8"
    )

    with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
        status = pipeline.lock_status()
        assert status is not None and status["pid"] == os.getpid() and status["alive"]


def test_lock_status_reports_holder():
    assert pipeline.lock_status() is None
    pipeline.LOCK_PATH.write_text('{"pid": 999999, "keyword": "RAG"}', encoding="utf-8")
    status = pipeline.lock_status()
    assert status["keyword"] == "RAG"
    assert status["alive"] is False
    assert status["self"] is False
    pipeline.LOCK_PATH.unlink()


def test_force_stop_releases_dead_lock():
    pipeline.LOCK_PATH.write_text('{"pid": 999999, "trigger": "schedule"}', encoding="utf-8")

    outcome = pipeline.force_stop(log=lambda m: None)

    assert outcome["released"] is True
    assert outcome["killed"] is False
    assert not pipeline.LOCK_PATH.exists()


def test_force_stop_without_lock_is_noop():
    outcome = pipeline.force_stop(log=lambda m: None)
    assert outcome["released"] is False


def test_orphan_running_rows_are_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    database.start_run("r1", "manual", "AI Agent", ["36Kr"])

    with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
        pass

    with database._connect() as conn:
        row = conn.execute("SELECT status, error FROM run_logs WHERE run_id = 'r1'").fetchone()
    assert row["status"] == "failed"
    assert "中断" in row["error"]
