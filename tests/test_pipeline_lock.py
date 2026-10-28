"""运行锁测试：跨进程互斥、异常释放、残留锁抢占。"""

import os
import time

import pytest

import pipeline


@pytest.fixture(autouse=True)
def temp_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / ".pulse.lock")
    yield
    assert not pipeline.LOCK_PATH.exists(), "测试结束后锁文件应被清理"


def test_second_acquire_is_rejected():
    with pipeline.run_lock("AI Agent", "manual", log=lambda m: None):
        with pytest.raises(pipeline.RunLocked):
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
