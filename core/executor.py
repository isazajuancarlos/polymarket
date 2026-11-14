"""
core/executor.py
================
Orquesta la ejecución de una decisión validada:

    decision -> risk.puede_operar() -> (paper | live) -> registro en DB

En modo paper delega en PaperSimulator. En modo LIVE ejecuta órdenes reales con
py-clob-client. El executor es el único sitio que decide paper-vs-live, de modo
que el resto del bot es agnóstico al modo.

Seguridad del camino LIVE (estrategia "ganar en SÍ y en NO" = arbitraje):
    1. Doble verja: settings.es_live exige PHASE=3 + LIVE_TRADING=true.
    2. Preflight: balance USDC y allowance suficientes (web3.py). Si no se
       puede verificar, se ABORTA (no se opera a ciegas).
    3. Recheck del orderbook: se reconfirma que el arbitraje sigue siendo
       rentable a PRECIOS EJECUTABLES (best ask + slippage) antes de enviar.
    4. Órdenes FOK (todo-o-nada): evita fills parciales.
    5. Leg risk: en arbitraje se compra primero un lado; si el segundo lado NO
       se llena, se DESHACE el primero (venta FOK). Si tampoco se puede
       deshacer, la operación queda DESBALANCEADA y se alerta para intervención
       manual.
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings, logger
from core.math_engine import (
    MotorMatematico,
    TIPO_ARBITRAJE_SUMA,
    TIPO_ARBITRAJE_MULTIPLE,
    TIPO_EV_POSITIVO,
    FORMA_LAY_FIELD,
)
from core.risk import risk_manager
from feeds.polymarket import PolymarketClient
from paper_trading.simulator import PaperSimulator
from db.models import get_session, Operacion


class Executor:
    def __init__(
        self,
        client: Optional[PolymarketClient] = None,
        simulator: Optional[PaperSimulator] = None,
        wallet: Optional[object] = None,
        motor: Optional[MotorMatematico] = None,
    ) -> None:
        self.client = client or PolymarketClient()
        self.simulator = simulator or PaperSimulator()
        self.motor = motor or MotorMatematico()
        # Wallet on-chain (web3.py). Se crea perezosamente solo si hace falta en
        # LIVE; puede inyectarse en tests.
        self._wallet = wallet

    # ------------------------------------------------------------------ #
    def _monto_de_decision(self, decision: dict) -> float:
        if decision.get("tipo") == TIPO_ARBITRAJE_SUMA:
            return float(decision["apuesta_si"]) + float(decision["apuesta_no"])
        if decision.get("tipo") == TIPO_ARBITRAJE_MULTIPLE:
            return float(decision["capital_comprometido"])
        if decision.get("tipo") == TIPO_EV_POSITIVO:
            return float(decision["apuesta"])
        return 0.0

    def ejecutar(self, decision: dict) -> Optional[Operacion]:
        """Valida riesgo y ejecuta. Devuelve la Operacion registrada o None."""
        monto = self._monto_de_decision(decision)
        veredicto = risk_manager.puede_operar(monto)
        if not veredicto.permitido:
            logger.info("Operación rechazada por riesgo: %s", veredicto.motivo)
            return None

        # Reescalar la decisión si el risk manager ajustó el monto.
        if veredicto.monto_ajustado is not None and monto > 0:
            factor = veredicto.monto_ajustado / monto
            if factor < 1.0:
                decision = self._reescalar(decision, factor)

        # EV solo va a órdenes reales con la compuerta explícita EV_LIVE. Sin
        # ella se simula siempre (aunque LIVE_TRADING esté activo), porque el EV
        # exige validación propia antes de arriesgar capital.
        if decision.get("tipo") == TIPO_EV_POSITIVO and not settings.ev_live:
            if settings.es_live:
                logger.info("EV detectado pero EV_LIVE=false: se simula (no se opera real).")
            return self.simulator.ejecutar(decision)

        # El arbitraje multi-resultado va a real solo con MULTI_ARB_LIVE (es
        # multi-pata: mayor leg risk, exige su propia validación). Si no, simula.
        if decision.get("tipo") == TIPO_ARBITRAJE_MULTIPLE and not settings.multi_arb_live:
            if settings.es_live:
                logger.info("Multi-arb detectado pero MULTI_ARB_LIVE=false: se simula.")
            return self.simulator.ejecutar(decision)

        # El camino LIVE se recorre tanto en live real como en dry-run (este
        # último se detiene antes de enviar la orden).
        if settings.es_live or settings.dry_run:
            return self._ejecutar_live(decision)
        return self.simulator.ejecutar(decision)

    @staticmethod
    def _reescalar(decision: dict, factor: float) -> dict:
        d = dict(decision)
        for clave in ("apuesta_si", "apuesta_no", "apuesta", "ganancia_minima_usd"):
            if clave in d and d[clave] is not None:
                d[clave] = float(d[clave]) * factor
        return d

    # ------------------------------------------------------------------ #
    # Preflight on-chain
    # ------------------------------------------------------------------ #
    def _get_wallet(self):
        if self._wallet is None:
            from feeds.wallet import Wallet  # import perezoso (web3)

            self._wallet = Wallet()
        return self._wallet

    def _preflight_live(self, total_usd: float) -> bool:
        """Verifica balance y allowance. Si no puede verificar, devuelve False."""
        try:
            w = self._get_wallet()
            balance = w.balance_usdc()
            allowance = w.allowance()
        except Exception as exc:  # noqa: BLE001 - cualquier fallo => abortar live
            logger.error("Preflight LIVE falló (no se opera): %s", exc)
            return False

        if balance < total_usd:
            logger.error("Balance USDC insuficiente: %.2f < %.2f", balance, total_usd)
            return False
        if allowance < total_usd:
            logger.error(
                "Allowance insuficiente (%.2f < %.2f). Ejecuta asegurar_allowance().",
                allowance, total_usd,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    # Ejecución LIVE
    # ------------------------------------------------------------------ #
    def _ejecutar_live(self, decision: dict) -> Optional[Operacion]:
        tipo = decision.get("tipo")
        if tipo == TIPO_ARBITRAJE_SUMA:
            return self._live_arbitraje(decision)
        if tipo == TIPO_ARBITRAJE_MULTIPLE:
            return self._live_multiple(decision)
        if tipo == TIPO_EV_POSITIVO:
            return self._live_ev(decision)
        return None

    def _live_multiple(self, decision: dict) -> Optional[Operacion]:
        """Arbitraje multi-pata: compra N patas FOK con deshacer ante fallo."""
        forma = decision.get("forma")
        legs = decision.get("legs") or []
        n = len(legs)
        total = float(decision["capital_comprometido"])
        market_id = decision.get("market_id") or decision.get("event_id")

        if n < 2 or any(l.get("token_id") is None for l in legs):
            logger.error("Multi-arb sin tokens válidos; se omite.")
            return None
        if not self._preflight_live(total):
            return None

        slip = settings.live_slippage

        # Revalidación a precios ejecutables del orderbook.
        precios_exec = []
        for l in legs:
            ask = self.client.best_ask(l["token_id"])
            if ask is None:
                logger.error("Multi-arb sin orderbook en una pata; se omite.")
                return None
            precios_exec.append(min(ask * (1 + slip), 0.999))

        suma = sum(precios_exec)
        if forma == FORMA_LAY_FIELD:
            profit = (n - 1) / suma - 1.0 - self.motor.fee
        else:  # FORMA_BUY_ALL_YES
            profit = 1.0 / suma - 1.0 - self.motor.fee
        if profit <= settings.multi_arb_min_profit:
            logger.warning(
                "[LIVE] Multi-arb ya no rentable a precios ejecutables "
                "(profit=%.4f). Se omite %s.", profit, market_id,
            )
            return None

        shares = total / suma  # mismo nº de shares por pata

        if settings.dry_run:
            logger.warning(
                "[DRY-RUN] NO se envía. Multi-arb %s (%s, %s patas): %.2f shares "
                "c/pata, coste≈%.2f, ganancia≈%.2f",
                market_id, forma, n, shares, total, total * profit,
            )
            return self._registrar_live(decision, total, "DRY_RUN", modo="dry")

        # Enviar patas FOK; si una falla, deshacer las ya llenas.
        llenas = []
        for i, l in enumerate(legs):
            fill = self.client.place_order(
                l["token_id"], "BUY", precios_exec[i], shares,
                order_type="FOK", market_id=market_id,
            )
            if fill.get("filled"):
                llenas.append((l["token_id"], precios_exec[i]))
            else:
                logger.error(
                    "[LIVE] Pata %s/%s no se llenó en multi-arb %s; deshaciendo.",
                    i + 1, n, market_id,
                )
                unwound = self._deshacer_patas(llenas, shares, market_id)
                self._alertar_desbalance(market_id, unwound)
                if not unwound:
                    risk_manager.pausar()
                return self._registrar_live(
                    decision, total, "CERRADA" if unwound else "DESBALANCEADA"
                )

        logger.warning("[LIVE] Multi-arb cubierto (%s patas) en %s", n, market_id)
        return self._registrar_live(decision, total, "EJECUTADA")

    def _deshacer_patas(self, llenas: list, shares: float, market_id: str) -> bool:
        """Vende (FOK) las patas ya compradas. True si todas se deshicieron."""
        todas = True
        for token_id, _precio in llenas:
            bid = self.client.best_bid(token_id)
            if bid is None:
                todas = False
                continue
            r = self.client.place_order(
                token_id, "SELL", bid, shares, order_type="FOK", market_id=market_id
            )
            if not r.get("filled"):
                todas = False
        return todas

    def _live_arbitraje(self, decision: dict) -> Optional[Operacion]:
        """Compra SÍ y NO (ganancia en cualquier desenlace) con control de leg risk."""
        market_id = decision.get("market_id")
        tokens = decision.get("clob_token_ids") or []
        if len(tokens) < 2:
            logger.error("Arbitraje LIVE sin clob_token_ids; se omite %s", market_id)
            return None
        token_si, token_no = tokens[0], tokens[1]

        total = float(decision["apuesta_si"]) + float(decision["apuesta_no"])
        if not self._preflight_live(total):
            return None

        slip = settings.live_slippage

        # 1) Recheck del orderbook: precios ejecutables reales.
        ask_si = self.client.best_ask(token_si)
        ask_no = self.client.best_ask(token_no)
        if ask_si is None or ask_no is None:
            logger.error("Sin orderbook ejecutable para %s; se omite.", market_id)
            return None

        # 2) ¿Sigue siendo rentable a precios ejecutables (con slippage)?
        precio_si_exec = min(ask_si * (1 + slip), 0.999)
        precio_no_exec = min(ask_no * (1 + slip), 0.999)
        suma_exec = precio_si_exec + precio_no_exec
        if suma_exec >= settings.arb_threshold:
            logger.warning(
                "[LIVE] Arbitraje ya no rentable a precios ejecutables "
                "(suma=%.4f >= %.4f). Se omite %s.",
                suma_exec, settings.arb_threshold, market_id,
            )
            return None

        # Tamaño en shares: mismo nº de shares en ambos lados (hedge perfecto).
        shares_si = float(decision["apuesta_si"]) / precio_si_exec
        shares_no = float(decision["apuesta_no"]) / precio_no_exec
        shares = min(shares_si, shares_no)  # iguala ambos lados de forma conservadora

        logger.warning(
            "[LIVE] Arbitraje %s | %.2f shares c/lado | si@%.3f no@%.3f",
            market_id, shares, precio_si_exec, precio_no_exec,
        )

        # DRY-RUN: todo el flujo real ya se ejecutó (preflight + revalidación);
        # aquí nos detenemos sin enviar ninguna orden.
        if settings.dry_run:
            logger.warning(
                "[DRY-RUN] NO se envía. Se habría comprado: BUY %s %.2f@%.3f  +  "
                "BUY %s %.2f@%.3f  (coste≈%.2f USDC, ganancia esperada≈%.2f)",
                token_si, shares, precio_si_exec,
                token_no, shares, precio_no_exec,
                total, float(decision.get("ganancia_minima_usd", 0.0)),
            )
            return self._registrar_live(decision, total, "DRY_RUN", modo="dry")

        # 3) Pata 1: comprar SÍ (FOK).
        fill_si = self.client.place_order(
            token_si, "BUY", precio_si_exec, shares, order_type="FOK", market_id=market_id
        )
        if not fill_si.get("filled"):
            logger.warning("[LIVE] Pata SÍ no se llenó; sin posición. %s", market_id)
            return self._registrar_live(decision, total, "CANCELADA")

        # 4) Pata 2: comprar NO (FOK).
        fill_no = self.client.place_order(
            token_no, "BUY", precio_no_exec, shares, order_type="FOK", market_id=market_id
        )
        if fill_no.get("filled"):
            logger.warning("[LIVE] Arbitraje cubierto (SÍ+NO) en %s", market_id)
            return self._registrar_live(decision, total, "EJECUTADA")

        # 5) Leg risk: SÍ quedó abierto y NO no se llenó -> deshacer SÍ.
        logger.error("[LIVE] Pata NO falló; deshaciendo pata SÍ en %s", market_id)
        bid_si = self.client.best_bid(token_si)
        unwound = False
        if bid_si is not None:
            unwind = self.client.place_order(
                token_si, "SELL", bid_si, shares, order_type="FOK", market_id=market_id
            )
            unwound = bool(unwind.get("filled"))
        self._alertar_desbalance(market_id, unwound)
        if not unwound:
            # Control PRECISO del bot de arbitraje: una posición sin cubrir es
            # la única vía real de pérdida del modelo "ganar en SÍ y en NO".
            # Pausa inmediata (requiere /reanudar manual); no esperamos al SL.
            logger.error(
                "[LIVE] PAUSA INMEDIATA por posición DESBALANCEADA en %s. "
                "Requiere intervención manual.", market_id,
            )
            risk_manager.pausar()
        return self._registrar_live(
            decision, total, "CERRADA" if unwound else "DESBALANCEADA"
        )

    def _live_ev(self, decision: dict) -> Optional[Operacion]:
        """Compra el lado SÍ por valor esperado (apuesta direccional)."""
        market_id = decision.get("market_id")
        tokens = decision.get("clob_token_ids") or []
        if not tokens:
            logger.error("EV LIVE sin clob_token_ids; se omite %s", market_id)
            return None
        token_si = tokens[0]

        total = float(decision["apuesta"])
        if not self._preflight_live(total):
            return None

        ask_si = self.client.best_ask(token_si)
        if ask_si is None:
            logger.error("Sin orderbook para EV en %s; se omite.", market_id)
            return None
        precio_exec = min(ask_si * (1 + settings.live_slippage), 0.999)
        shares = total / precio_exec

        if settings.dry_run:
            logger.warning(
                "[DRY-RUN] NO se envía. Se habría comprado: BUY %s %.2f@%.3f "
                "(coste≈%.2f USDC)",
                token_si, shares, precio_exec, total,
            )
            return self._registrar_live(decision, total, "DRY_RUN", modo="dry")

        fill = self.client.place_order(
            token_si, "BUY", precio_exec, shares, order_type="FOK", market_id=market_id
        )
        estado = "EJECUTADA" if fill.get("filled") else "CANCELADA"
        return self._registrar_live(decision, total, estado)

    # ------------------------------------------------------------------ #
    def _alertar_desbalance(self, market_id: str, unwound: bool) -> None:
        try:
            from alerts import telegram as tg

            if unwound:
                tg.notifier.enviar_mensaje(
                    f"⚠️ Arbitraje no cubierto en {market_id}: pata SÍ deshecha OK."
                )
            else:
                tg.notifier.enviar_mensaje(
                    f"🔴 *POSICIÓN DESBALANCEADA* en {market_id}. Pata SÍ abierta sin "
                    "cobertura y no se pudo deshacer. INTERVENCIÓN MANUAL REQUERIDA."
                )
        except Exception:  # noqa: BLE001
            pass

    def _registrar_live(
        self, decision: dict, total: float, estado: str, modo: str = "live"
    ) -> Operacion:
        with get_session() as s:
            op = Operacion(
                tipo=decision.get("tipo"),
                mercado_id=decision.get("market_id"),
                titulo=decision.get("titulo", ""),
                apuesta_si=float(decision.get("apuesta_si", decision.get("apuesta", 0.0))),
                apuesta_no=float(decision.get("apuesta_no", 0.0)),
                apuesta_total=total,
                ganancia_esperada=float(decision.get("ganancia_minima_usd", 0.0)),
                estado=estado,
                modo=modo,
            )
            s.add(op)
            s.flush()
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
