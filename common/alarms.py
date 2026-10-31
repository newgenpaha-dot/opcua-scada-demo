"""Движок аварийной сигнализации: сравнение значений с уставками LL/L/H/HH с гистерезисом.

Чистая логика без I/O — удобно тестировать и переиспользовать.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .config import AlarmLimits


class AlarmLevel(str, Enum):
    LL = "LL"   # Low-Low  — аварийный нижний
    L = "L"     # Low      — предупредительный нижний
    H = "H"     # High     — предупредительный верхний
    HH = "HH"   # High-High— аварийный верхний


# Приоритет: чем больше число, тем серьёзнее
PRIORITY = {AlarmLevel.L: 1, AlarmLevel.H: 1, AlarmLevel.LL: 2, AlarmLevel.HH: 2}


@dataclass
class AlarmEvent:
    tag: str
    level: AlarmLevel
    value: float
    ts: datetime
    active: bool  # True — аларм возник, False — снят

    @property
    def message(self) -> str:
        state = "ACTIVE" if self.active else "CLEARED"
        return f"{self.tag} {self.level.value} {state} (value={self.value:.2f})"


def evaluate(value: float, limits: AlarmLimits) -> AlarmLevel | None:
    """Вернуть самый серьёзный уровень аларма для значения (без учёта гистерезиса)."""
    if limits.hh is not None and value >= limits.hh:
        return AlarmLevel.HH
    if limits.ll is not None and value <= limits.ll:
        return AlarmLevel.LL
    if limits.h is not None and value >= limits.h:
        return AlarmLevel.H
    if limits.l is not None and value <= limits.l:
        return AlarmLevel.L
    return None


def _is_cleared(value: float, level: AlarmLevel, limits: AlarmLimits) -> bool:
    """Аларм снимается, только когда значение вернулось за уставку минус/плюс мёртвая зона."""
    db = limits.deadband
    threshold = {
        AlarmLevel.HH: limits.hh, AlarmLevel.H: limits.h,
        AlarmLevel.LL: limits.ll, AlarmLevel.L: limits.l,
    }[level]
    if threshold is None:
        return True
    if level in (AlarmLevel.H, AlarmLevel.HH):
        return value < threshold - db
    return value > threshold + db


@dataclass
class AlarmEngine:
    """Хранит текущее состояние алармов по тегам и выдаёт события при изменениях."""

    limits: dict[str, AlarmLimits]
    active: dict[str, AlarmLevel] = field(default_factory=dict)

    def process(self, tag: str, value: float, ts: datetime | None = None) -> list[AlarmEvent]:
        lim = self.limits.get(tag)
        if lim is None:
            return []
        ts = ts or datetime.now(timezone.utc)
        events: list[AlarmEvent] = []
        current = self.active.get(tag)
        new = evaluate(value, lim)

        if current is None:
            if new is not None:
                self.active[tag] = new
                events.append(AlarmEvent(tag, new, value, ts, True))
            return events

        # Аларм уже активен
        if new is not None and PRIORITY[new] > PRIORITY[current]:
            # Эскалация, напр. H -> HH: снимаем старый, ставим новый
            events.append(AlarmEvent(tag, current, value, ts, False))
            self.active[tag] = new
            events.append(AlarmEvent(tag, new, value, ts, True))
        elif _is_cleared(value, current, lim):
            del self.active[tag]
            events.append(AlarmEvent(tag, current, value, ts, False))
            # После снятия могло остаться менее серьёзное состояние (HH -> H)
            if new is not None:
                self.active[tag] = new
                events.append(AlarmEvent(tag, new, value, ts, True))
        return events
