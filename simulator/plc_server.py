"""Симулятор ПЛК — OPC UA сервер (asyncua).

Моделирует простой технологический узел: ёмкость с регулированием уровня,
откачивающий насос, нагреватель и трубопровод. Внутри крутится "программа ПЛК":
ПИ-регулятор уровня, защита насоса от сухого хода, термостат, периодические возмущения.

Запуск:  python -m simulator.plc_server
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from pathlib import Path

from asyncua import Server, ua

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.config import OPC_NAMESPACE, OPC_URL, PLC_CYCLE_S, TAGS  # noqa: E402

log = logging.getLogger("plc-sim")

VARIANT_BY_TYPE = {
    float: ua.VariantType.Double,
    int: ua.VariantType.Int32,
    bool: ua.VariantType.Boolean,
    str: ua.VariantType.String,
}
DEFAULTS = {float: 0.0, int: 0, bool: False, str: ""}


class Process:
    """Физическая модель объекта (то, что в реальности — железо и трубы)."""

    def __init__(self) -> None:
        self.level = 50.0          # %
        self.temperature = 20.0    # °C
        self.ambient = 20.0
        self.pump_speed = 0.0      # %
        self.pressure = 0.2        # bar
        self.inlet_k = 1.0         # множитель притока (для возмущений)
        self.outlet_k = 1.0

    def step(self, dt: float, valve: float, pump_cmd: bool, heater_kw: float) -> None:
        # Насос разгоняется/останавливается с рампой
        target = 80.0 if pump_cmd else 0.0
        ramp = 25.0 * dt
        self.pump_speed += max(-ramp, min(ramp, target - self.pump_speed))

        inflow = valve / 100 * 6.0 * self.inlet_k           # %/с при полностью открытом клапане
        outflow = self.pump_speed / 100 * 5.0 * self.outlet_k
        self.level += (inflow - outflow) * dt
        self.level = max(0.0, min(100.0, self.level))

        # Давление зависит от скорости насоса и уровня (гидростатика), плюс шум
        self.pressure = 0.2 + self.pump_speed / 100 * 5.5 + self.level / 100 * 0.4 \
            + random.gauss(0, 0.03)
        self.pressure = max(0.0, self.pressure)

        # Теплообмен: нагрев минус потери в окружающую среду и с притоком холодного продукта
        heat_in = heater_kw * 0.012
        loss = (self.temperature - self.ambient) * 0.004 + inflow * 0.02
        self.temperature += (heat_in - loss) * dt


class PlcProgram:
    """Логика "прошивки ПЛК": регуляторы, блокировки, диагностика."""

    def __init__(self) -> None:
        self.integral = 0.0
        self.kp, self.ki = 4.0, 0.15
        self.heater_on = False
        self.pump_running = False
        self.heartbeat = 0
        self.disturbance_until = 0.0

    def level_pi(self, sp: float, pv: float, dt: float) -> float:
        err = sp - pv
        self.integral += err * dt
        self.integral = max(-300.0, min(300.0, self.integral))  # anti-windup
        return max(0.0, min(100.0, self.kp * err + self.ki * self.integral))

    def pump_interlock(self, cmd: bool, level: float) -> bool:
        # Защита от сухого хода: стоп при уровне < 12%, повторный пуск только > 18%
        if not cmd:
            self.pump_running = False
        elif self.pump_running and level < 12:
            self.pump_running = False
            log.warning("Pump tripped: low level %.1f%%", level)
        elif not self.pump_running and level > 18:
            self.pump_running = True
        return self.pump_running

    def thermostat(self, heater_enable: bool, temperature: float) -> float:
        if not heater_enable:
            self.heater_on = False
        elif temperature < 60:
            self.heater_on = True
        elif temperature > 66:
            self.heater_on = False
        return 50.0 if self.heater_on else 0.0

    def maybe_disturb(self, proc: Process, now: float) -> str | None:
        """Раз в ~2–3 минуты вносим возмущение, чтобы в демо возникали алармы."""
        if now < self.disturbance_until:
            return None
        if random.random() < 0.004:  # ~ раз в 250 циклов
            kind = random.choice(["inlet_drop", "inlet_surge", "outlet_block"])
            proc.inlet_k = {"inlet_drop": 0.2, "inlet_surge": 2.2, "outlet_block": 1.0}[kind]
            proc.outlet_k = 0.3 if kind == "outlet_block" else 1.0
            self.disturbance_until = now + random.uniform(25, 45)
            log.warning("Disturbance injected: %s", kind)
            return kind
        if proc.inlet_k != 1.0 or proc.outlet_k != 1.0:
            proc.inlet_k = proc.outlet_k = 1.0
            log.info("Disturbance cleared")
        return None


async def build_address_space(server: Server) -> dict[str, object]:
    idx = await server.register_namespace(OPC_NAMESPACE)
    plc = await server.nodes.objects.add_object(idx, "PLC1")
    folders: dict[str, object] = {}
    nodes: dict[str, object] = {}
    for tag in TAGS:
        group, _, short = tag.name.partition(".")
        if group not in folders:
            folders[group] = await plc.add_object(idx, group)
        node = await folders[group].add_variable(
            ua.NodeId(tag.name, idx), short,
            ua.Variant(DEFAULTS[tag.dtype], VARIANT_BY_TYPE[tag.dtype]),
        )
        await node.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(ua.LocalizedText(f"{tag.description} [{tag.unit}]"))),
        )
        if tag.writable:
            await node.set_writable()
        nodes[tag.name] = node
    return nodes


async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(OPC_URL)
    server.set_server_name("BMZ PLC Simulator")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])  # учебный проект, без шифрования

    nodes = await build_address_space(server)
    await nodes["Tank.LevelSetpoint"].write_value(55.0)
    await nodes["Pump.Command"].write_value(True)
    await nodes["Heater.On"].write_value(True)
    await nodes["PLC.Mode"].write_value("AUTO")

    proc, prog = Process(), PlcProgram()
    log.info("OPC UA server listening on %s", OPC_URL)

    async with server:
        last = time.monotonic()
        while True:
            cycle_start = time.perf_counter()
            now = time.monotonic()
            dt, last = now - last, now

            # Чтение "входов" — уставок, которые могли записать клиенты
            sp = await nodes["Tank.LevelSetpoint"].read_value()
            pump_cmd = await nodes["Pump.Command"].read_value()
            heater_enable = await nodes["Heater.On"].read_value()

            # Программа ПЛК
            prog.maybe_disturb(proc, now)
            valve = prog.level_pi(sp, proc.level, dt)
            pump_run = prog.pump_interlock(pump_cmd, proc.level)
            heater_kw = prog.thermostat(heater_enable, proc.temperature)

            # Физика
            proc.step(dt, valve, pump_run, heater_kw)
            current = (0.3 * proc.pump_speed + 1.8 * proc.pressure + random.gauss(0, 0.3)) \
                if proc.pump_speed > 1 else 0.0

            # Запись "выходов" в OPC UA
            prog.heartbeat = (prog.heartbeat + 1) % 32767
            await nodes["Tank.Level"].write_value(round(proc.level, 2))
            await nodes["Tank.InletValve"].write_value(round(valve, 1))
            await nodes["Tank.Temperature"].write_value(round(proc.temperature, 2))
            await nodes["Pump.Running"].write_value(pump_run)
            await nodes["Pump.Speed"].write_value(round(proc.pump_speed, 1))
            await nodes["Pump.Current"].write_value(round(max(0.0, current), 2))
            await nodes["Heater.Power"].write_value(heater_kw)
            await nodes["Pipe.Pressure"].write_value(round(proc.pressure, 3))
            await nodes["PLC.Heartbeat"].write_value(ua.Variant(prog.heartbeat, ua.VariantType.Int32))
            await nodes["PLC.CycleTime"].write_value(
                round((time.perf_counter() - cycle_start) * 1000, 2))

            await asyncio.sleep(max(0.0, PLC_CYCLE_S - (time.perf_counter() - cycle_start)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
