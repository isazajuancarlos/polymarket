"""
tests/test_db_retention.py
==========================
Verifica la poda de la DB (`purgar_antiguos`): borra snapshots de `mercados` y
`logs` viejos, conserva los recientes y NUNCA toca el historial de trading
(`operaciones`, `portafolio`).

Ejecutar:  pytest tests/test_db_retention.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

import db.models as m


@pytest.fixture
def db_temporal(tmp_path, monkeypatch):
    """Engine SQLite en un archivo temporal, aislado por test."""
    from config.settings import settings

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'t.db'}")
    # Resetear los globals perezosos para que apunten a la DB temporal.
    monkeypatch.setattr(m, "_engine", None)
    monkeypatch.setattr(m, "_SessionFactory", None)
    m.init_db()
    yield


def _insertar_mercado(dias_atras: float) -> None:
    ts = datetime.now(timezone.utc) - timedelta(days=dias_atras)
    with m.get_session() as s:
        s.add(m.Mercado(market_id="x", titulo="t", precio_si=0.5,
                        precio_no=0.5, timestamp=ts))


def test_purga_borra_viejos_conserva_recientes(db_temporal):
    _insertar_mercado(dias_atras=10)   # viejo -> debe borrarse
    _insertar_mercado(dias_atras=10)   # viejo -> debe borrarse
    _insertar_mercado(dias_atras=1)    # reciente -> debe quedarse

    borrado = m.purgar_antiguos(dias=7)

    assert borrado["mercados"] == 2
    with m.get_session() as s:
        assert s.query(m.Mercado).count() == 1


def test_purga_logs(db_temporal):
    viejo = datetime.now(timezone.utc) - timedelta(days=30)
    with m.get_session() as s:
        s.add(m.Log(nivel="INFO", mensaje="viejo", timestamp=viejo))
        s.add(m.Log(nivel="INFO", mensaje="nuevo"))  # timestamp = ahora

    borrado = m.purgar_antiguos(dias=7)

    assert borrado["logs"] == 1
    with m.get_session() as s:
        assert s.query(m.Log).count() == 1


def test_dias_cero_desactiva_poda(db_temporal):
    _insertar_mercado(dias_atras=100)
    borrado = m.purgar_antiguos(dias=0)
    assert borrado == {"mercados": 0, "logs": 0}
    with m.get_session() as s:
        assert s.query(m.Mercado).count() == 1


def test_wal_mode_activo(db_temporal):
    """SQLite debe quedar en WAL (resiliencia ante cierres en duro)."""
    with m.get_session() as s:
        modo = s.execute(text("PRAGMA journal_mode")).scalar()
    assert modo.lower() == "wal"


def test_poda_no_toca_operaciones_ni_portafolio(db_temporal):
    """El historial de trading se conserva pase lo que pase."""
    viejo = datetime.now(timezone.utc) - timedelta(days=365)
    with m.get_session() as s:
        s.add(m.Operacion(tipo="ARBITRAJE_SUMA", mercado_id="x", timestamp=viejo))
        s.add(m.Portafolio(capital_inicial=500, capital_actual=500, timestamp=viejo))

    m.purgar_antiguos(dias=7)

    with m.get_session() as s:
        assert s.query(m.Operacion).count() == 1
        assert s.query(m.Portafolio).count() == 1
