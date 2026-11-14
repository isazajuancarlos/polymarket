"""
core/risk.py
============
Gestor de riesgo del portafolio. Es la última línea de defensa antes de
ejecutar cualquier operación.

Controles:
    - Capital disponible suficiente (>= capital mínimo operacional).
    - Tamaño máximo por operación: max_position_pct del capital.
    - Máximo de operaciones simultáneas (por defecto 3).
    - Stop loss del portafolio (8%): pausa automática si se supera.
    - Reporte de exposición actual.

El estado de pausa es global al proceso (RiskManager singleton) y también lo
comparte el control manual de Telegram (/pausar, /reanudar).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import func

from config.settings import settings, logger
from db.models import get_session, Operacion, Portafolio


@dataclass
class ResultadoRiesgo:
    permitido: bool
    motivo: str = ""
    monto_ajustado: Optional[float] = None


class RiskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pausado = False
        self._pausa_por_stop_loss = False

    # ------------------------------------------------------------------ #
    # Estado de pausa
    # ------------------------------------------------------------------ #
    @property
    def pausado(self) -> bool:
        return self._pausado

    @property
    def pausa_por_stop_loss(self) -> bool:
        return self._pausa_por_stop_loss

    def pausar(self, por_stop_loss: bool = False) -> None:
        with self._lock:
            self._pausado = True
            if por_stop_loss:
                self._pausa_por_stop_loss = True
            logger.warning("Bot PAUSADO%s", " (stop loss)" if por_stop_loss else "")

    def reanudar(self) -> None:
        with self._lock:
            self._pausado = False
            self._pausa_por_stop_loss = False
            logger.info("Bot REANUDADO")

    # ------------------------------------------------------------------ #
    # Lectura del estado del portafolio
    # ------------------------------------------------------------------ #
    def _portafolio_actual(self) -> Portafolio:
        with get_session() as s:
            p = s.query(Portafolio).order_by(Portafolio.timestamp.desc()).first()
            if p is None:
                p = Portafolio(
                    capital_inicial=settings.capital_usdc,
                    capital_actual=settings.capital_usdc,
                    ganancia_acumulada=0.0,
                )
                s.add(p)
                s.flush()
            # Desacoplar del session para usar fuera del context manager.
            return Portafolio(
                id=p.id,
                capital_inicial=p.capital_inicial,
                capital_actual=p.capital_actual,
                ganancia_acumulada=p.ganancia_acumulada,
            )

    def capital_disponible(self) -> float:
        """Capital actual menos lo comprometido en operaciones abiertas."""
        p = self._portafolio_actual()
        comprometido = self._capital_comprometido()
        return max(p.capital_actual - comprometido, 0.0)

    def _capital_comprometido(self) -> float:
        with get_session() as s:
            total = (
                s.query(func.coalesce(func.sum(Operacion.apuesta_total), 0.0))
                .filter(Operacion.estado.in_(["PENDIENTE", "EJECUTADA"]))
                .scalar()
            )
            return float(total or 0.0)

    def operaciones_abiertas(self) -> int:
        with get_session() as s:
            return (
                s.query(Operacion)
                .filter(Operacion.estado.in_(["PENDIENTE", "EJECUTADA"]))
                .count()
            )

    # ------------------------------------------------------------------ #
    # Stop loss
    # ------------------------------------------------------------------ #
    def verificar_stop_loss(self) -> bool:
        """Devuelve True si el stop loss del portafolio está activado.

        Pausa automáticamente el bot la primera vez que se cruza el umbral.
        """
        p = self._portafolio_actual()
        if p.capital_inicial <= 0:
            return False
        drawdown = (p.capital_inicial - p.capital_actual) / p.capital_inicial
        if drawdown >= settings.stop_loss_pct:
            if not self._pausa_por_stop_loss:
                logger.error(
                    "STOP LOSS ACTIVADO: drawdown %.2f%% >= %.2f%%",
                    drawdown * 100,
                    settings.stop_loss_pct * 100,
                )
                self.pausar(por_stop_loss=True)
            return True
        return False

    # ------------------------------------------------------------------ #
    # Verificación previa a cada operación
    # ------------------------------------------------------------------ #
    def puede_operar(self, monto_solicitado: float) -> ResultadoRiesgo:
        """Comprueba todas las reglas. Devuelve permiso y monto ajustado."""
        if self._pausado:
            return ResultadoRiesgo(False, "Bot en pausa.")

        if self.verificar_stop_loss():
            return ResultadoRiesgo(False, "Stop loss del portafolio activado.")

        p = self._portafolio_actual()

        # Capital mínimo operacional.
        if p.capital_actual < settings.min_operational_capital:
            return ResultadoRiesgo(
                False,
                f"Capital ({p.capital_actual:.2f}) por debajo del mínimo operacional "
                f"({settings.min_operational_capital}).",
            )

        # Máximo de operaciones simultáneas.
        if self.operaciones_abiertas() >= settings.max_concurrent_trades:
            return ResultadoRiesgo(
                False,
                f"Límite de operaciones simultáneas alcanzado "
                f"({settings.max_concurrent_trades}).",
            )

        # Capital disponible.
        disponible = self.capital_disponible()
        if disponible <= 0:
            return ResultadoRiesgo(False, "Sin capital disponible.")

        # Tamaño máximo por operación.
        max_por_op = p.capital_actual * settings.max_position_pct
        monto_ajustado = min(monto_solicitado, max_por_op, disponible)
        if monto_ajustado <= 0:
            return ResultadoRiesgo(False, "Monto ajustado no positivo.")

        return ResultadoRiesgo(True, "OK", monto_ajustado=monto_ajustado)

    # ------------------------------------------------------------------ #
    # Reporte de exposición
    # ------------------------------------------------------------------ #
    def posiciones_desbalanceadas(self) -> int:
        """Cuenta operaciones que quedaron sin cubrir (hedge roto sin deshacer)."""
        with get_session() as s:
            return (
                s.query(Operacion).filter(Operacion.estado == "DESBALANCEADA").count()
            )

    def reporte_exposicion(self) -> dict:
        p = self._portafolio_actual()
        comprometido = self._capital_comprometido()
        drawdown = 0.0
        if p.capital_inicial > 0:
            drawdown = (p.capital_inicial - p.capital_actual) / p.capital_inicial
        return {
            "posiciones_desbalanceadas": self.posiciones_desbalanceadas(),
            "capital_inicial": p.capital_inicial,
            "capital_actual": p.capital_actual,
            "ganancia_acumulada": p.ganancia_acumulada,
            "capital_comprometido": comprometido,
            "capital_disponible": max(p.capital_actual - comprometido, 0.0),
            "operaciones_abiertas": self.operaciones_abiertas(),
            "drawdown_pct": drawdown,
            "pausado": self._pausado,
            "stop_loss_activo": self._pausa_por_stop_loss,
        }


# Singleton de proceso.
risk_manager = RiskManager()
