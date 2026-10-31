"""Мини-SCADA дашборд на Streamlit: текущие значения, тренды, алармы, управление уставками.

Запуск:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.config import DB_PATH, OPC_NAMESPACE, OPC_URL, TAGS, TAGS_BY_NAME  # noqa: E402

st.set_page_config(page_title="PLC Simulator SCADA", layout="wide", page_icon="🏭")

TREND_TAGS = ["Tank.Level", "Tank.InletValve", "Tank.Temperature", "Pump.Speed", "Pump.Current", "Pipe.Pressure"]


# ---------- Данные ----------

@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA query_only=1")
    return conn


def latest_values(conn: sqlite3.Connection) -> pd.DataFrame:
    sql = """
    SELECT m.tag, m.ts, m.value, m.value_s, m.quality
    FROM measurements m
    JOIN (SELECT tag, MAX(id) AS id FROM measurements GROUP BY tag) last ON last.id = m.id
    """
    return pd.read_sql(sql, conn).set_index("tag")


def trend(conn: sqlite3.Connection, tags: list[str], minutes: int) -> pd.DataFrame:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    ph = ",".join("?" * len(tags))
    df = pd.read_sql(
        f"SELECT ts, tag, value FROM measurements WHERE ts >= ? AND tag IN ({ph}) ORDER BY ts",
        conn, params=[since, *tags])
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None)
    return df.pivot_table(index="ts", columns="tag", values="value").ffill()


def alarms(conn: sqlite3.Connection, active_only: bool, limit: int = 50) -> pd.DataFrame:
    where = "WHERE ts_off IS NULL" if active_only else ""
    return pd.read_sql(
        f"SELECT ts_on, ts_off, tag, level, value_on, value_off FROM alarms {where} ORDER BY id DESC LIMIT {limit}",
        conn)


def events(conn: sqlite3.Connection, limit: int = 20) -> pd.DataFrame:
    return pd.read_sql(f"SELECT ts, kind, text FROM events ORDER BY id DESC LIMIT {limit}", conn)


# ---------- Запись в ПЛК (синхронный клиент — Streamlit не asyncio) ----------

def write_tag(tag: str, value) -> str:
    from asyncua.sync import Client, ua
    try:
        with Client(OPC_URL, timeout=3) as client:
            idx = client.get_namespace_index(OPC_NAMESPACE)
            node = client.get_node(ua.NodeId(tag, idx))
            vt = node.read_data_type_as_variant_type()
            node.write_value(ua.Variant(value, vt))
        return f"OK: {tag} = {value}"
    except Exception as e:  # noqa: BLE001
        return f"Ошибка записи {tag}: {e}"


# ---------- UI ----------

def fmt(tag: str, row) -> str:
    t = TAGS_BY_NAME[tag]
    if t.dtype is str:
        return str(row.value_s)
    if t.dtype is bool:
        return "ON" if row.value else "OFF"
    if t.dtype is int:
        return f"{int(row.value)}"
    return f"{row.value:.2f} {t.unit}".strip()


def alarm_color(tag: str, value: float | None) -> str:
    t = TAGS_BY_NAME[tag]
    if not t.limits or value is None:
        return "normal"
    lim = t.limits
    if (lim.hh is not None and value >= lim.hh) or (lim.ll is not None and value <= lim.ll):
        return "inverse"
    if (lim.h is not None and value >= lim.h) or (lim.l is not None and value <= lim.l):
        return "off"
    return "normal"


def main() -> None:
    st.title("🏭 PLC Simulator — мини-SCADA")

    with st.sidebar:
        st.header("Настройки")
        minutes = st.slider("Окно тренда, мин", 1, 60, 10)
        refresh = st.slider("Автообновление, с", 1, 30, 2)
        auto = st.toggle("Автообновление", value=os.getenv("DASH_AUTOREFRESH", "1") == "1")
        st.caption(f"OPC UA: `{OPC_URL}`")
        st.caption(f"DB: `{DB_PATH}`")

        st.header("Управление")
        sp = st.number_input("Уставка уровня, %", 0.0, 100.0, 55.0, 1.0)
        if st.button("Записать уставку"):
            st.toast(write_tag("Tank.LevelSetpoint", float(sp)))
        c1, c2 = st.columns(2)
        if c1.button("Насос: ПУСК"):
            st.toast(write_tag("Pump.Command", True))
        if c2.button("Насос: СТОП"):
            st.toast(write_tag("Pump.Command", False))
        c3, c4 = st.columns(2)
        if c3.button("Нагрев: ВКЛ"):
            st.toast(write_tag("Heater.On", True))
        if c4.button("Нагрев: ВЫКЛ"):
            st.toast(write_tag("Heater.On", False))

    if not DB_PATH.exists():
        st.warning("База данных ещё не создана. Запустите симулятор и коллектор.")
        st.stop()

    conn = get_conn()
    last = latest_values(conn)
    if last.empty:
        st.info("Данных пока нет — ждём первые значения от коллектора...")
        time.sleep(refresh)
        st.rerun()

    # Статус связи: по возрасту последнего значения heartbeat
    hb_ts = pd.to_datetime(last.loc["PLC.Heartbeat", "ts"], utc=True) if "PLC.Heartbeat" in last.index else None
    age = (datetime.now(timezone.utc) - hb_ts).total_seconds() if hb_ts is not None else 999
    status = "🟢 ONLINE" if age < 5 else "🔴 OFFLINE"
    st.subheader(f"Связь с ПЛК: {status}  ·  последние данные {age:.0f} с назад")

    # Текущие значения
    cols = st.columns(6)
    for i, tag in enumerate([t.name for t in TAGS if t.name in last.index]):
        row = last.loc[tag]
        t = TAGS_BY_NAME[tag]
        color = alarm_color(tag, row.value if t.dtype in (float, int) else None)
        cols[i % 6].metric(label=f"{t.description}", value=fmt(tag, row), delta_color=color,
                           delta=("" if color == "normal" else "ALARM"))

    # Активные алармы
    act = alarms(conn, active_only=True)
    st.subheader(f"Активные алармы: {len(act)}")
    if act.empty:
        st.success("Нет активных алармов")
    else:
        st.dataframe(act, width="stretch", hide_index=True)

    # Тренды
    st.subheader(f"Тренды за последние {minutes} мин")
    df = trend(conn, TREND_TAGS, minutes)
    if df.empty:
        st.info("Нет данных за выбранный период")
    else:
        c1, c2 = st.columns(2)
        c1.line_chart(df[[c for c in ["Tank.Level", "Tank.InletValve"] if c in df]], height=250)
        c2.line_chart(df[[c for c in ["Tank.Temperature"] if c in df]], height=250)
        c3, c4 = st.columns(2)
        c3.line_chart(df[[c for c in ["Pump.Speed", "Pump.Current"] if c in df]], height=250)
        c4.line_chart(df[[c for c in ["Pipe.Pressure"] if c in df]], height=250)

    # Журналы
    with st.expander("Журнал алармов (последние 50)"):
        st.dataframe(alarms(conn, active_only=False), width="stretch", hide_index=True)
    with st.expander("Системные события"):
        st.dataframe(events(conn), width="stretch", hide_index=True)

    if auto:
        time.sleep(refresh)
        st.rerun()


main()
