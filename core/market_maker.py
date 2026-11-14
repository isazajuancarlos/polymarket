"""
core/market_maker.py
====================
Estrategia 3: Market making en mercados de alto volumen.

Postea cotizaciones bid/ask alrededor del fair value para capturar el spread,
gestionando el inventario para revertirlo a cero. Es la estrategia de menor
prioridad (se ejecuta después de arbitraje y EV) y la más exigente
operativamente, por lo que está fuertemente acotada:

    - Solo mercados con volumen >= MM_MIN_VOLUME.
    - Como mucho MM_MAX_MARKETS mercados cotizados a la vez.
    - Tamaño por lado limitado por MAX_POSITION_PCT y por el RiskManager.

La matemática de las cotizaciones vive en MotorMatematico.calcular_market_making
(pura y testeable). Aquí se orquesta: selección de mercados, control de riesgo,
colocación de órdenes (paper o live) y seguimiento de inventario.

NOTA: el llenado real de órdenes resting requiere el orderbook en vivo. En
paper trading modelamos el inventario de forma simplificada y registramos la
ganancia esperada por round-trip; la validación fina se hace en Fase 2 con
datos en tiempo real.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from config.settings import settings, logger
from core.math_engine import MotorMatematico, TIPO_MARKET_MAKING
from core.risk import risk_manager
from feeds.polymarket import PolymarketClient
from db.models import get_session, Operacion


class MarketMaker:
    def __init__(
        self,
        client: Optional[PolymarketClient] = None,
        motor: Optional[MotorMatematico] = None,
    ) -> None:
        self.client = client or PolymarketClient()
        self.motor = motor or MotorMatematico()
        # Inventario por market_id, en shares netas (positivo = largo SÍ).
        self._inventario: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def inventario(self, market_id: str) -> float:
        with self._lock:
            return self._inventario.get(market_id, 0.0)

    def _ajustar_inventario(self, market_id: str, delta_shares: float) -> None:
        with self._lock:
            self._inventario[market_id] = self._inventario.get(market_id, 0.0) + delta_shares

    # ------------------------------------------------------------------ #
    def cotizar_mercado(self, mercado: dict) -> Optional[dict]:
        """Calcula y postea cotizaciones para un mercado. Devuelve la cotización.

        `mercado` es el dict normalizado de feeds/polymarket.py.
        """
        if risk_manager.pausado:
            return None

        # SEGURIDAD: el market making aún no está validado para LIVE (requiere
        # ciclo maker post/cancel/replace + simulación de fills). Solo paper.
        if settings.es_live:
            logger.info("MM omitido en LIVE (estrategia aún en validación paper).")
            return None

        market_id = mercado.get("market_id")
        volumen = mercado.get("volumen", 0.0)
        tokens = mercado.get("clob_token_ids") or [market_id, market_id]
        token_si = tokens[0]
        capital = risk_manager.reporte_exposicion()["capital_actual"]

        # Fair value REAL del orderbook (mid), no el precio de Gamma. Exige libro
        # de DOS lados: sin bid o sin ask no hay mercado que hacer.
        try:
            best_bid = self.client.best_bid(token_si)
            best_ask = self.client.best_ask(token_si)
        except Exception as exc:  # noqa: BLE001
            logger.debug("MM sin orderbook para %s: %s", market_id, exc)
            return None
        if best_bid is None or best_ask is None or best_ask <= best_bid:
            return None  # libro de un solo lado o cruzado: no cotizar

        fair = (best_bid + best_ask) / 2.0

        # Filtro de banda: fuera de [min, max] el outcome es demasiado extremo
        # (longshot o casi-seguro). Hacer mercado ahí es una trampa de inventario
        # — es justo el caso "Will Jesus return?" que el dry-run destapó.
        if not (settings.mm_min_price <= fair <= settings.mm_max_price):
            return None

        cotizacion = self.motor.calcular_market_making(
            fair_value=fair,
            volumen=volumen,
            capital=capital,
            inventario_shares=self.inventario(market_id),
        )
        if cotizacion is None:
            return None

        # Control de riesgo sobre el tamaño comprometido (dos patas).
        size_por_lado = cotizacion["size_usd_por_lado"]
        veredicto = risk_manager.puede_operar(size_por_lado)
        if not veredicto.permitido:
            logger.info("MM rechazado por riesgo en %s: %s", market_id, veredicto.motivo)
            return None
        if veredicto.monto_ajustado is not None:
            size_por_lado = min(size_por_lado, veredicto.monto_ajustado)
            cotizacion["size_usd_por_lado"] = size_por_lado
            cotizacion["ganancia_minima_usd"] = size_por_lado * cotizacion["edge_neto"]

        cotizacion["market_id"] = market_id
        cotizacion["titulo"] = mercado.get("titulo", "")

        # Postear ambas patas como órdenes resting GTC (en paper se simulan)
        # sobre el outcome SÍ: mismo token a dos precios (bid/ask).
        shares_bid = size_por_lado / cotizacion["bid"] if cotizacion["bid"] > 0 else 0.0
        shares_ask = size_por_lado / cotizacion["ask"] if cotizacion["ask"] > 0 else 0.0
        self.client.place_order(
            token_si, "BUY", cotizacion["bid"], shares_bid, order_type="GTC", market_id=market_id
        )
        self.client.place_order(
            token_si, "SELL", cotizacion["ask"], shares_ask, order_type="GTC", market_id=market_id
        )

        self._registrar(cotizacion)
        logger.info(
            "MM cotizando %s | bid=%.3f ask=%.3f edge=%.4f size=%.2f",
            (mercado.get("titulo") or "")[:40],
            cotizacion["bid"],
            cotizacion["ask"],
            cotizacion["edge_neto"],
            size_por_lado,
        )
        return cotizacion

    # ------------------------------------------------------------------ #
    def evaluar(self, mercados: List[dict]) -> List[dict]:
        """Selecciona los mercados de mayor volumen y los cotiza.

        Devuelve las cotizaciones efectivamente posteadas.
        """
        if risk_manager.pausado:
            return []

        candidatos = [
            m for m in mercados if m.get("volumen", 0.0) >= settings.mm_min_volume
        ]
        candidatos.sort(key=lambda m: m.get("volumen", 0.0), reverse=True)

        cotizaciones: List[dict] = []
        for m in candidatos[: settings.mm_max_markets]:
            c = self.cotizar_mercado(m)
            if c is not None:
                cotizaciones.append(c)
        return cotizaciones

    # ------------------------------------------------------------------ #
    def _registrar(self, cotizacion: dict) -> None:
        size = cotizacion["size_usd_por_lado"]
        try:
            with get_session() as s:
                s.add(
                    Operacion(
                        tipo=TIPO_MARKET_MAKING,
                        mercado_id=cotizacion.get("market_id"),
                        titulo=cotizacion.get("titulo", ""),
                        apuesta_si=size,   # pata de compra (bid)
                        apuesta_no=size,   # pata de venta (ask)
                        apuesta_total=size * 2,
                        ganancia_esperada=cotizacion["ganancia_minima_usd"],
                        ganancia_real=None,  # depende del llenado de ambas patas
                        estado="PENDIENTE",
                        modo="live" if settings.es_live else "paper",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo registrar cotización MM: %s", exc)
