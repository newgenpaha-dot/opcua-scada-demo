from common.alarms import AlarmEngine, AlarmLevel, evaluate
from common.config import AlarmLimits

LIM = AlarmLimits(ll=10, l=20, h=85, hh=95, deadband=1.0)


def test_evaluate_levels():
    assert evaluate(50, LIM) is None
    assert evaluate(85, LIM) is AlarmLevel.H
    assert evaluate(95, LIM) is AlarmLevel.HH
    assert evaluate(20, LIM) is AlarmLevel.L
    assert evaluate(10, LIM) is AlarmLevel.LL
    assert evaluate(99, LIM) is AlarmLevel.HH  # HH важнее H


def test_alarm_raise_and_clear_with_deadband():
    eng = AlarmEngine({"T": LIM})
    assert eng.process("T", 50) == []
    ev = eng.process("T", 86)
    assert [(e.level, e.active) for e in ev] == [(AlarmLevel.H, True)]
    # Внутри мёртвой зоны аларм не снимается (дребезг)
    assert eng.process("T", 84.5) == []
    ev = eng.process("T", 83.9)
    assert [(e.level, e.active) for e in ev] == [(AlarmLevel.H, False)]
    assert eng.active == {}


def test_escalation_h_to_hh_and_back():
    eng = AlarmEngine({"T": LIM})
    eng.process("T", 86)
    ev = eng.process("T", 96)
    assert [(e.level, e.active) for e in ev] == [(AlarmLevel.H, False), (AlarmLevel.HH, True)]
    # Снижение ниже HH-deadband, но всё ещё выше H: HH снимается, H возвращается
    ev = eng.process("T", 93)
    assert [(e.level, e.active) for e in ev] == [(AlarmLevel.HH, False), (AlarmLevel.H, True)]


def test_unknown_tag_ignored():
    eng = AlarmEngine({})
    assert eng.process("X", 1e9) == []


def test_partial_limits():
    lim = AlarmLimits(h=38, hh=42, deadband=0.5)
    eng = AlarmEngine({"I": lim})
    assert eng.process("I", 0) == []          # нет нижних уставок — не аларм
    assert eng.process("I", 40)[0].level is AlarmLevel.H
