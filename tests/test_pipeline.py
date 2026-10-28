"""pipeline 纯函数测试：热度评分只由抓取结果决定。"""

import pipeline


def test_compute_heat_grows_with_items_and_sources():
    few = pipeline.compute_heat([{"source": "TechCrunch"}])
    many = pipeline.compute_heat(
        [{"source": name} for name in ["TechCrunch", "36Kr", "arXiv", "InfoQ", "Solidot"] for _ in range(4)]
    )
    assert 800 <= few < many <= 2000


def test_compute_heat_is_bounded():
    assert pipeline.compute_heat([]) == 800
    huge = pipeline.compute_heat([{"source": f"s{i}"} for i in range(200)])
    assert huge == 2000


def test_compute_heat_ignores_empty_sources():
    assert pipeline.compute_heat([{"source": ""}]) == pipeline.compute_heat([{}])
