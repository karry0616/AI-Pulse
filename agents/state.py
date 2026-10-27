"""Agent 共享状态。"""

from __future__ import annotations

from dataclasses import dataclass, field

from rag.memory import MemoryContext


@dataclass
class AgentState:
    run_id: str
    keyword: str
    items: list[dict]
    memory: MemoryContext

    events: list[dict] = field(default_factory=list)
    draft: dict | None = None
    critique: list[str] = field(default_factory=list)
    critique_history: list[list[str]] = field(default_factory=list)
    attempts: int = 0
    mode: str = "deepseek"
    degraded: bool = False
    passed: bool = False

    @property
    def titles(self) -> list[str]:
        seen: set[str] = set()
        titles: list[str] = []
        for item in self.items:
            title = str(item.get("title") or "").strip()
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
        return titles
