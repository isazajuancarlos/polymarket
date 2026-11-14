"""
paper_trading/simulator.py
==========================
Simulador de ejecución de órdenes con fees reales de Polymarket.

- Registra cada operación simulada en la tabla `operaciones` (modo="paper").
- Actualiza el portafolio simulado.
- Calcula métricas: win rate, ROI, drawdown máximo, ganancia acumulada.
- Genera un reporte diario en consola.
- Usa los precios reales que le pasa el scanner (en tiempo real).

El simulador es la pieza que valida la estrategia ANTES de arriesgar capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from config.settings import settings, logger
from db.models import get_session, Operacion, Portafolio
from core.math_engine import (
    TIPO_ARBITRAJE_SUMA,
    TIPO_ARBITRAJE_MULTIPLE,
    TIPO_EV_POSITIVO,
)


@dataclass
class Metricas:
    operaciones: int = 0
    ganadoras: int = 0
    perdedoras: int = 0
    ganancia_acumulada: float = 0.0
    roi: float = 0.0
    drawdown_maximo: float = 0.0
    win_rate: float = 0.0
    historial_capital: List[float] = field(default_factory=list)


class PaperSimulator:
    """Ejecuta operaciones de forma simulada y lleva la contabilidad."""

    def __init__(self) -> None:
        self.fee = settings.fee

    # ------------------------------------------------------------------ #
    # Ejecución simulada
    # ------------------------------------------------------------------ #
    def ejecutar(self, decision: dict) -> Optional[Operacion]:
        """Simula la ejecución de una decisión del motor matemático.

        Para arbitraje de suma: la ganancia es determinista (hedge perfecto),
        así que se registra como CERRADA con ganancia_real = ganancia_esperada
        neta de fee.

        Para EV positivo: la posición queda PENDIENTE (su resultado real solo
        se conoce al resolver el mercado); se registra la ganancia esperada.
        """
        tipo = decision.get("tipo")
        market_id = decision.get("market_id")
        titulo = decision.get("titulo", "")

        if tipo == TIPO_ARBITRAJE_SUMA:
            apuesta_si = float(decision["apuesta_si"])
            apuesta_no = float(decision["apuesta_no"])
            total = apuesta_si + apuesta_no
            ganancia_esperada = float(decision["ganancia_minima_usd"])
            # En arbitraje el resultado es garantizado => ganancia_real conocida.
            op = self._registrar(
                tipo=tipo,
                market_id=market_id,
                titulo=titulo,
                apuesta_si=apuesta_si,
                apuesta_no=apuesta_no,
                total=total,
                ganancia_esperada=ganancia_esperada,
                ganancia_real=ganancia_esperada,
                estado="CERRADA",
            )
            self._aplicar_pnl_portafolio(ganancia_esperada)
            return op

        if tipo == TIPO_ARBITRAJE_MULTIPLE:
            # Estructuralmente garantizado (exclusión mutua): ganancia conocida.
            total = float(decision.get("capital_comprometido", 0.0))
            ganancia_esperada = float(decision["ganancia_minima_usd"])
            op = self._registrar(
                tipo=tipo,
                market_id=market_id,
                titulo=f"[{decision.get('forma')}] {titulo}",
                apuesta_si=total,
                apuesta_no=0.0,
                total=total,
                ganancia_esperada=ganancia_esperada,
                ganancia_real=ganancia_esperada,
                estado="CERRADA",
            )
            self._aplicar_pnl_portafolio(ganancia_esperada)
            return op

        if tipo == TIPO_EV_POSITIVO:
            apuesta = float(decision["apuesta"])
            ganancia_esperada = float(decision.get("ganancia_minima_usd", 0.0))
            op = self._registrar(
                tipo=tipo,
                market_id=market_id,
                titulo=titulo,
                apuesta_si=apuesta,
                apuesta_no=0.0,
                total=apuesta,
                ganancia_esperada=ganancia_esperada,
                ganancia_real=None,  # se conocerá al resolver el mercado
                estado="PENDIENTE",
            )
            return op

        logger.warning("PaperSimulator: tipo de decisión desconocido %r", tipo)
        return None

    def _registrar(
        self,
        tipo: str,
        market_id: Optional[str],
        titulo: str,
        apuesta_si: float,
        apuesta_no: float,
        total: float,
        ganancia_esperada: float,
        ganancia_real: Optional[float],
        estado: str,
    ) -> Operacion:
        with get_session() as s:
            op = Operacion(
                tipo=tipo,
                mercado_id=market_id,
                titulo=titulo,
                apuesta_si=apuesta_si,
                apuesta_no=apuesta_no,
                apuesta_total=total,
                ganancia_esperada=ganancia_esperada,
                ganancia_real=ganancia_real,
                estado=estado,
                modo="paper",
            )
            s.add(op)
            s.flush()
            logger.info(
                "[PAPER] %s registrada | %s | total=%.2f | g_esp=%.2f | estado=%s",
                tipo,
                titulo[:50],
                total,
                ganancia_esperada,
                estado,
            )
            # Devolver una copia desacoplada de la sesión.
            return Operacion(
                id=op.id,
                tipo=op.tipo,
                mercado_id=op.mercado_id,
                titulo=op.titulo,
                apuesta_si=op.apuesta_si,
                apuesta_no=op.apuesta_no,
                apuesta_total=op.apuesta_total,
                ganancia_esperada=op.ganancia_esperada,
                ganancia_real=op.ganancia_real,
                estado=op.estado,
                modo=op.modo,
            )

    def _aplicar_pnl_portafolio(self, pnl: float) -> None:
        """Crea un nuevo snapshot de portafolio con el PnL aplicado."""
        with get_session() as s:
            ultimo = s.query(Portafolio).order_by(Portafolio.timestamp.desc()).first()
            if ultimo is None:
                inicial = settings.capital_usdc
                actual = settings.capital_usdc + pnl
                acumulada = pnl
            else:
                inicial = ultimo.capital_inicial
                actual = ultimo.capital_actual + pnl
                acumulada = ultimo.ganancia_acumulada + pnl
            s.add(
                Portafolio(
                    capital_inicial=inicial,
                    capital_actual=actual,
                    ganancia_acumulada=acumulada,
                )
            )

    # ------------------------------------------------------------------ #
    # Métricas
    # ------------------------------------------------------------------ #
    def calcular_metricas(self, dias: int = 7) -> Metricas:
        desde = datetime.now(timezone.utc) - timedelta(days=dias)
        m = Metricas()
        with get_session() as s:
            ops = (
                s.query(Operacion)
                .filter(Operacion.modo == "paper", Operacion.timestamp >= desde)
                .order_by(Operacion.timestamp.asc())
                .all()
            )
            for op in ops:
                m.operaciones += 1
                resultado = op.ganancia_real if op.ganancia_real is not None else 0.0
                m.ganancia_acumulada += resultado
                if op.ganancia_real is not None:
                    if resultado > 0:
                        m.ganadoras += 1
                    elif resultado < 0:
                        m.perdedoras += 1

            snaps = (
                s.query(Portafolio)
                .filter(Portafolio.timestamp >= desde)
                .order_by(Portafolio.timestamp.asc())
                .all()
            )
            m.historial_capital = [sp.capital_actual for sp in snaps]

        cerradas = m.ganadoras + m.perdedoras
        m.win_rate = (m.ganadoras / cerradas) if cerradas else 0.0
        if settings.capital_usdc > 0:
            m.roi = m.ganancia_acumulada / settings.capital_usdc
        m.drawdown_maximo = self._max_drawdown(m.historial_capital)
        return m

    @staticmethod
    def _max_drawdown(serie: List[float]) -> float:
        if not serie:
            return 0.0
        pico = serie[0]
        max_dd = 0.0
        for v in serie:
            pico = max(pico, v)
            if pico > 0:
                dd = (pico - v) / pico
                max_dd = max(max_dd, dd)
        return max_dd

    # ------------------------------------------------------------------ #
    # Reporte
    # ------------------------------------------------------------------ #
    def reporte_diario(self, dias: int = 1) -> str:
        m = self.calcular_metricas(dias=dias)
        lineas = [
            "=" * 52,
            f"  REPORTE PAPER TRADING — últimos {dias} día(s)",
            "=" * 52,
            f"  Operaciones:        {m.operaciones}",
            f"  Ganadoras:          {m.ganadoras}",
            f"  Perdedoras:         {m.perdedoras}",
            f"  Win rate:           {m.win_rate:.1%}",
            f"  Ganancia acumulada: {m.ganancia_acumulada:+.2f} USDC",
            f"  ROI:                {m.roi:+.2%}",
            f"  Drawdown máximo:    {m.drawdown_maximo:.2%}",
            "=" * 52,
        ]
        reporte = "\n".join(lineas)
        print(reporte)
        return reporte
