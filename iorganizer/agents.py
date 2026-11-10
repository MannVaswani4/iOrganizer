"""Replaceable, deterministic agents; no network or model calls are made."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from icalendar import Calendar

from .domain import Candidate, ItemKind


class ContentExtractor(ABC):
    @abstractmethod
    def extract(self, text: str, source: str) -> list[Candidate]:
        """Convert untrusted source text into reviewable candidates."""


class RuleBasedExtractor(ContentExtractor):
    """A transparent baseline agent that detects task-like lines."""
    TASK_MARKERS = ("todo", "to do", "remind me", "need to", "action:", "follow up")

    def extract(self, text: str, source: str) -> list[Candidate]:
        results: list[Candidate] = []
        for index, raw in enumerate(text.splitlines()):
            line = raw.strip().lstrip("-•* ")
            if not line or not any(marker in line.lower() for marker in self.TASK_MARKERS):
                continue
            cleaned = re.sub(r"^(todo|to do|action):\s*", "", line, flags=re.I).strip()
            if not cleaned:
                continue
            category = "work" if any(w in cleaned.lower() for w in ("meeting", "project", "client")) else "personal"
            results.append(Candidate(
                title=cleaned[:120], body=line, kind=ItemKind.TASK,
                category=category, source_ref=f"{source}:line:{index + 1}",
            ))
        return results


class AgentPipeline:
    """Orchestrates agent strategies while keeping the acceptance step human-led."""
    def __init__(self, extractor: ContentExtractor) -> None:
        self._extractor = extractor

    def process(self, text: str, source: str) -> list[Candidate]:
        return self._extractor.extract(text, source)

    def process_calendar(self, content: bytes, source: str) -> list[Candidate]:
        calendar = Calendar.from_ical(content)
        candidates: list[Candidate] = []
        for component in calendar.walk("VEVENT"):
            title = str(component.get("summary", "")).strip()
            if not title:
                continue
            start = component.get("dtstart")
            due_at = start.dt if start else None
            if due_at is not None and not isinstance(due_at, datetime):
                due_at = datetime.combine(due_at, datetime.min.time(), tzinfo=timezone.utc)
            elif due_at is not None and due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            uid = str(component.get("uid", title))
            candidates.append(Candidate(title=title, body=str(component.get("description", "")),
                                        kind=ItemKind.EVENT, category="calendar", due_at=due_at,
                                        source_ref=f"{source}:event:{uid}"))
        return candidates
