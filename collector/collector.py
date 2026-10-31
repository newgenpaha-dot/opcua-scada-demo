"""Коллектор данных: OPC UA клиент → SQLite.

- Подключается к серверу, подписывается на изменения всех тегов (Subscription / MonitoredItems).
- Складывает значения в очередь, пишет в БД пачками раз в секунду.
- Прогоняет числовые значения через движок алармов, пишет журнал.
- При потере связи — переподключается с экспоненциальной задержкой.

Запуск:  python -m collector.collector
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from asyncua import Client, Node, ua

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.alarms import AlarmEngine  # noqa: E402
from common.config import DB_PATH, OPC_NAMESPACE, OPC_URL, SUBSCRIPTION_PERIOD_MS, TAGS  # noqa: E402
from collector.storage import Storage  # noqa: E402

log = logging.getLogger("collector")

FLUSH_INTERVAL_S = 1.0
HEARTBEAT_TIMEOUT_S = 5.0


class SubscriptionHandler:
    """Callback'и asyncua вызываются из event loop — просто кладём данные в очередь."""

    def __init__(self, queue: asyncio.Queue, node_to_tag: dict[Node, str]) -> None:
        self.queue = queue
        self.node_to_tag = node_to_tag
        self.last_data = datetime.now(timezone.utc)

    def datachange_notification(self, node: Node, val, data: ua.DataChangeNotification) -> None:
        dv = data.monitored_item.Value
        ts = dv.SourceTimestamp or dv.ServerTimestamp or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        sc = getattr(dv, "StatusCode", None) or getattr(dv, "StatusCode_", None)
        quality = "Good" if sc is None or sc.is_good() else sc.name
        self.last_data = datetime.now(timezone.utc)
        self.queue.put_nowait((ts, self.node_to_tag[node], val, quality))

    def status_change_notification(self, status: ua.StatusChangeNotification) -> None:
        log.warning("Subscription status changed: %s", status.Status)


async def flush_loop(queue: asyncio.Queue, storage: Storage, engine: AlarmEngine, stop: asyncio.Event) -> None:
    """Раз в секунду забираем всё из очереди, пишем в БД одной транзакцией, считаем алармы."""
    while not stop.is_set():
        await asyncio.sleep(FLUSH_INTERVAL_S)
        rows = []
        while not queue.empty():
            rows.append(queue.get_nowait())
        if not rows:
            continue
        storage.write_measurements(rows)
        for ts, tag, val, quality in rows:
            if quality == "Good" and isinstance(val, (int, float)) and not isinstance(val, bool):
                for ev in engine.process(tag, float(val), ts):
                    log.warning("ALARM %s", ev.message)
                    storage.write_alarm(ev)
        log.debug("flushed %d rows", len(rows))


async def run_session(storage: Storage, engine: AlarmEngine) -> None:
    """Одна сессия: подключение → подписка → работа до разрыва."""
    queue: asyncio.Queue = asyncio.Queue()
    async with Client(url=OPC_URL) as client:
        idx = await client.get_namespace_index(OPC_NAMESPACE)
        node_to_tag = {client.get_node(ua.NodeId(t.name, idx)): t.name for t in TAGS}
        handler = SubscriptionHandler(queue, node_to_tag)

        # Один Subscription, в нём MonitoredItem на каждый тег. queuesize>1 — чтобы не терять
        # промежуточные значения, если publish interval больше частоты изменений.
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, handler)
        await sub.subscribe_data_change(list(node_to_tag), queuesize=10)
        storage.write_event("connect", f"Connected to {OPC_URL}, {len(node_to_tag)} tags subscribed")
        log.info("Subscribed to %d tags, publish interval %d ms", len(node_to_tag), SUBSCRIPTION_PERIOD_MS)

        stop = asyncio.Event()
        flusher = asyncio.create_task(flush_loop(queue, storage, engine, stop))
        try:
            while True:
                await asyncio.sleep(1.0)
                # Контроль связи: heartbeat ПЛК меняется каждый цикл, значит данные должны идти
                silent = (datetime.now(timezone.utc) - handler.last_data).total_seconds()
                if silent > HEARTBEAT_TIMEOUT_S:
                    raise ConnectionError(f"No data for {silent:.0f}s (heartbeat lost)")
                await client.check_connection()
        finally:
            stop.set()
            await flusher
            try:
                await sub.delete()
            except Exception:
                pass


async def main() -> None:
    storage = Storage(DB_PATH)
    engine = AlarmEngine(limits={t.name: t.limits for t in TAGS if t.limits})
    backoff = 1.0
    log.info("Collector started, DB=%s", DB_PATH)
    while True:
        try:
            await run_session(storage, engine)
            backoff = 1.0
        except (OSError, asyncio.TimeoutError, ConnectionError, ua.UaError) as e:
            now = datetime.now(timezone.utc)
            log.error("Connection lost: %s. Reconnect in %.0fs", e, backoff)
            storage.write_event("disconnect", str(e), now)
            storage.close_all_open_alarms(now, "connection lost")
            engine.active.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
