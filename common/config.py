"""Общая конфигурация: адрес OPC UA сервера, путь к БД, описание тегов и уставок алармов."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OPC_URL = os.getenv("OPC_URL", "opc.tcp://127.0.0.1:4840/plc-sim/")
OPC_NAMESPACE = "urn:bmz:plc-sim"
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "scada.db"))

# Период обновления подписки OPC UA, мс
SUBSCRIPTION_PERIOD_MS = int(os.getenv("SUBSCRIPTION_PERIOD_MS", "500"))
# Период цикла ПЛК в симуляторе, с
PLC_CYCLE_S = float(os.getenv("PLC_CYCLE_S", "0.5"))


@dataclass(frozen=True)
class AlarmLimits:
    """Уставки аварийной сигнализации в стиле ISA-18.2: LL / L / H / HH."""

    ll: float | None = None
    l: float | None = None
    h: float | None = None
    hh: float | None = None
    # Гистерезис (мёртвая зона) для снятия аларма, в единицах тега
    deadband: float = 0.0


@dataclass(frozen=True)
class Tag:
    name: str            # строковый NodeId внутри namespace, напр. "Tank.Level"
    description: str
    unit: str
    dtype: type          # float | bool | int | str
    writable: bool = False
    limits: AlarmLimits | None = None


TAGS: list[Tag] = [
    # --- Ёмкость ---
    Tag("Tank.Level", "Уровень в ёмкости", "%", float,
        limits=AlarmLimits(ll=10, l=20, h=85, hh=95, deadband=1.0)),
    Tag("Tank.LevelSetpoint", "Уставка уровня", "%", float, writable=True),
    Tag("Tank.InletValve", "Положение впускного клапана", "%", float),
    Tag("Tank.Temperature", "Температура продукта", "°C", float,
        limits=AlarmLimits(l=15, h=70, hh=80, deadband=0.5)),
    # --- Насос ---
    Tag("Pump.Running", "Насос в работе", "", bool),
    Tag("Pump.Speed", "Частота насоса", "%", float),
    Tag("Pump.Current", "Ток двигателя насоса", "A", float,
        limits=AlarmLimits(h=38, hh=42, deadband=0.5)),
    Tag("Pump.Command", "Команда на насос (авто/стоп)", "", bool, writable=True),
    # --- Нагрев ---
    Tag("Heater.On", "Нагреватель включён", "", bool, writable=True),
    Tag("Heater.Power", "Мощность нагревателя", "kW", float),
    # --- Трубопровод ---
    Tag("Pipe.Pressure", "Давление на выходе насоса", "bar", float,
        limits=AlarmLimits(l=0.5, h=5.5, hh=6.0, deadband=0.1)),
    # --- Служебные ---
    Tag("PLC.Heartbeat", "Счётчик жизни ПЛК", "", int),
    Tag("PLC.Mode", "Режим работы", "", str),
    Tag("PLC.CycleTime", "Время цикла ПЛК", "ms", float),
]

TAGS_BY_NAME: dict[str, Tag] = {t.name: t for t in TAGS}
