"""
db/models.py
============
Modelos SQLAlchemy y utilidades de sesión.

Tablas:
    - mercados     : snapshot de cada mercado observado
    - operaciones  : cada operación (paper o live) decidida/ejecutada
    - portafolio   : evolución del capital
    - logs         : log estructurado persistente

Uso:
    from db.models import init_db, get_session
    init_db()                      # crea tablas si no existen
    with get_session() as s:
        ...
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config.settings import settings, logger

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Mercado(Base):
    __tablename__ = "mercados"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String(128), index=True)  # id externo de Polymarket
    titulo = Column(String(512))
    precio_si = Column(Float, nullable=False)
    precio_no = Column(Float, nullable=False)
    volumen = Column(Float, default=0.0)
    liquidez = Column(Float, default=0.0)
    timestamp = Column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Mercado {self.market_id} si={self.precio_si} no={self.precio_no}>"


class Operacion(Base):
    __tablename__ = "operaciones"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tipo = Column(String(32), nullable=False)  # ARBITRAJE_SUMA | EV_POSITIVO | MARKET_MAKING
    mercado_id = Column(String(128), index=True)
    titulo = Column(String(512), default="")
    apuesta_si = Column(Float, default=0.0)
    apuesta_no = Column(Float, default=0.0)
    apuesta_total = Column(Float, default=0.0)
    ganancia_esperada = Column(Float, default=0.0)
    ganancia_real = Column(Float, nullable=True)
    estado = Column(String(32), default="PENDIENTE")  # PENDIENTE|EJECUTADA|CERRADA|CANCELADA|PAPER
    modo = Column(String(8), default="paper")  # paper | live
    timestamp = Column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Operacion {self.tipo} {self.mercado_id} estado={self.estado}>"


class Portafolio(Base):
    __tablename__ = "portafolio"

    id = Column(Integer, primary_key=True, autoincrement=True)
    capital_inicial = Column(Float, nullable=False)
    capital_actual = Column(Float, nullable=False)
    ganancia_acumulada = Column(Float, default=0.0)
    timestamp = Column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Portafolio actual={self.capital_actual} pnl={self.ganancia_acumulada}>"


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nivel = Column(String(16), default="INFO")
    mensaje = Column(Text, nullable=False)
    datos_json = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), default=_utcnow, index=True)


# ---------------------------------------------------------------------------
# Engine / sesión (inicialización perezosa)
# ---------------------------------------------------------------------------
# Se crean en el primer uso para que importar este módulo no exija tener el
# driver de la base de datos instalado (útil en tests del motor matemático).
# `future=True` para semántica 2.0. pool_pre_ping evita conexiones muertas.
_engine = None
_SessionFactory = None


def _aplicar_pragmas_sqlite(dbapi_conn, _rec) -> None:
    """PRAGMAs por conexión en SQLite. Clave: WAL.

    La DB de 2,4 GB que se corrompió corría en el modo journal por defecto
    (`delete`), frágil si el proceso muere en duro a mitad de escritura. WAL
    sobrevive mucho mejor a cierres sucios y permite que el scanner escriba
    mientras el bot de Telegram lee, sin bloquear. `synchronous=NORMAL` es
    seguro con WAL y más rápido; `busy_timeout` evita 'database is locked'
    cuando los dos hilos coinciden.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_engine():
    global _engine, _SessionFactory
    if _engine is None:
        kwargs = {"future": True, "pool_pre_ping": True}
        # SQLite + hilos (scanner + bot de comandos Telegram): hay que permitir
        # compartir la conexión entre hilos.
        if settings.database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(settings.database_url, **kwargs)
        if settings.database_url.startswith("sqlite"):
            event.listen(_engine, "connect", _aplicar_pragmas_sqlite)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """Crea todas las tablas si no existen."""
    Base.metadata.create_all(get_engine())
    logger.info("Base de datos inicializada (%s)", settings.database_url)


@contextmanager
def get_session() -> Iterator[Session]:
    """Context manager transaccional: commit al salir, rollback si hay error."""
    get_engine()  # asegura que _SessionFactory está inicializada
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def purgar_antiguos(dias: int) -> dict:
    """Borra snapshots de `mercados` y `logs` más antiguos que `dias`.

    El scanner persiste un mercado por ciclo y por mercado; sin poda la tabla
    crece sin límite y termina llenando el disco en una corrida 24/7. Las tablas
    `operaciones` y `portafolio` NO se tocan: son el historial de trading que
    queremos conservar. `dias <= 0` desactiva la poda (no borra nada).

    Devuelve {"mercados": n, "logs": n} con las filas eliminadas.
    """
    if dias <= 0:
        return {"mercados": 0, "logs": 0}
    corte = _utcnow() - timedelta(days=dias)
    with get_session() as s:
        n_merc = (
            s.query(Mercado)
            .filter(Mercado.timestamp < corte)
            .delete(synchronize_session=False)
        )
        n_logs = (
            s.query(Log)
            .filter(Log.timestamp < corte)
            .delete(synchronize_session=False)
        )
    return {"mercados": n_merc, "logs": n_logs}


def asegurar_portafolio_inicial() -> None:
    """Crea la primera fila de portafolio si la tabla está vacía."""
    with get_session() as s:
        existe = s.query(Portafolio).first()
        if existe is None:
            s.add(
                Portafolio(
                    capital_inicial=settings.capital_usdc,
                    capital_actual=settings.capital_usdc,
                    ganancia_acumulada=0.0,
                )
            )
            logger.info("Portafolio inicial creado con %s USDC", settings.capital_usdc)
