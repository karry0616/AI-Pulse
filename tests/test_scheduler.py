"""调度器编排测试：关键词串行执行，单点失败不阻断后续。"""

import scheduler_service


class FakeResult:
    run_id = "run-fake"
    inserted = 1
    alert_id = None
    analysis = {"_mode": "fallback"}


def test_run_all_iterates_keywords_in_order(monkeypatch):
    calls = []

    def fake_run(keyword, sources, trigger="schedule", log=print):
        calls.append(keyword)
        return FakeResult()

    monkeypatch.setattr(scheduler_service.pipeline, "run", fake_run)
    assert scheduler_service.run_all(["大模型", "RAG"], ["TechCrunch"]) == 0
    assert calls == ["大模型", "RAG"]


def test_run_all_continues_after_failure(monkeypatch):
    calls = []

    def fake_run(keyword, sources, trigger="schedule", log=print):
        calls.append(keyword)
        if keyword == "大模型":
            raise RuntimeError("boom")
        return FakeResult()

    monkeypatch.setattr(scheduler_service.pipeline, "run", fake_run)
    assert scheduler_service.run_all(["大模型", "RAG"], ["TechCrunch"]) == 1
    assert calls == ["大模型", "RAG"]
