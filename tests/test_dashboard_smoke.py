"""Smoke-тест дашборда: скрипт рендерится без исключений на пустой/заполненной БД."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "scada.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("DASH_AUTOREFRESH", "0")
    # config читается при импорте — сбрасываем кэш модулей
    import sys
    for m in [k for k in sys.modules if k.startswith("common")]:
        del sys.modules[m]
    return path


def test_renders_without_db(db):
    at = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not at.exception
    assert any("не создана" in w.value for w in at.warning)


def test_renders_with_data(db):
    from collector.storage import Storage
    from common.alarms import AlarmEngine
    from common.config import TAGS

    st = Storage(db)
    now = datetime.now(timezone.utc)
    rows = [(now, t.name, 1.0 if t.dtype is not str else "AUTO", "Good") for t in TAGS]
    rows.append((now, "Tank.Level", 97.0, "Good"))
    st.write_measurements(rows)
    eng = AlarmEngine({t.name: t.limits for t in TAGS if t.limits})
    for ev in eng.process("Tank.Level", 97.0, now):
        st.write_alarm(ev)
    st.write_event("connect", "test")
    st.close()

    at = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not at.exception, at.exception
    assert any("Активные алармы: 1" in s.value for s in at.subheader)
    assert sqlite3.connect(db).execute("select count(*) from alarms").fetchone()[0] == 1
