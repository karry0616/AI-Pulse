"""页面级冒烟测试：用 Streamlit AppTest 无头执行 app.py，覆盖整页渲染分支。"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import database
import notify
import pipeline

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
TIMEOUT = 40


class FakeResult:
    def __init__(self):
        self.run_id = "run-app"
        self.keyword = "AI Agent"
        self.analysis = {
            "_run_id": "run-app",
            "_mode": "deepseek",
            "_attempts": 1,
            "_passed": True,
            "heat_score": 1450,
            "positive_ratio": "62%",
            "negative_alerts": 0,
            "warning_text": "暂未监测到高风险事件，负面信号密度低于阈值。",
            "top_news": "- 某模型发布新版本\n- 行业融资回暖\n- 开源生态扩张",
            "maturity": "| 环节 | 成熟度 |\n| --- | --- |\n| 推理 | 高 |",
        }
        self.alert_id = None
        self.items = 12
        self.inserted = 8
        self.duration_ms = 4200
        self.usage = {}


@pytest.fixture(autouse=True)
def temp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / ".pulse.lock")
    monkeypatch.setattr(pipeline, "cooldown_remaining", lambda *a, **k: 0)
    database.init_db()
    yield


def _button(at: AppTest, label: str):
    for button in at.button:
        if button.label == label:
            return button
    raise AssertionError(f"未找到按钮「{label}」，当前按钮：{[b.label for b in at.button]}")


def test_app_renders_empty_state_without_exception():
    at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT).run()

    assert not at.exception
    assert any("AI Pulse" in item.value for item in at.markdown)
    # 空库时图表与指标区块给出占位提示而不是报错
    assert any("暂无数据" in item.value for item in at.info)


def test_app_run_pipeline_then_demo_alert_flow(monkeypatch):
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: FakeResult())

    at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT).run()
    _button(at, "立即更新情报").click().run()

    assert not at.exception
    assert any("今日核心动态" in item.value for item in at.markdown)
    assert any("本地规则引擎" in item.value or "DeepSeek 多 Agent" in item.value for item in at.markdown)

    # 无负面预警时可插入一条 DEMO 待审预警，覆盖 HITL 演示分支
    _button(at, "演示 HITL · 生成待审批预警").click().run()
    assert not at.exception
    assert database.latest_pending_alert("AI Agent") is not None

    # 审批推送在未配置 WEBHOOK_URL 时安全失败，页面不白屏
    monkeypatch.setattr(
        notify, "send_webhook", lambda **kwargs: notify.PushResult(False, "Webhook", "未配置")
    )
    _button(at, "审核并发送").click().run()
    assert not at.exception
    alert = database.latest_alert("run-app")
    assert alert["status"] in ("failed", "pending", "sent")
