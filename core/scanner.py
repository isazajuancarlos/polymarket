"""
core/scanner.py
===============
Loop principal de escaneo. Cada `scan_interval_seconds` (30s por defecto):

    1. Obtiene los mercados activos de Polymarket.
    2. Para cada mercado: estima prob_modelo (analyzer) y llama
       math_engine.decision_final().
    3. Filtra por liquidez mínima.
    4. Ordena las oportunidades por ganancia esperada (USD).
    5. Envía alertas Telegram para oportunidades con ganancia > MIN_EV.
    6. Registra cada snapshot/decisión en la DB.
    7. Ejecuta las mejores oportunidades vía Executor (respeta riesgo).

El scanner no decide paper-vs-live: eso lo hace el Executor.
"""

from __future__ import annotations

import time
from typing import List

from config.settings import settings, logger
from core.math_engine import (
    MotorMatematico,
    MercadoView,
    TIPO_ARBITRAJE_SUMA,
    TIPO_EV_POSITIVO,
)
from core.analyzer import Analyzer
from core.executor import Executor
from core.market_maker import MarketMaker
from core.risk import risk_manager
from feeds.polymarket import PolymarketClient
from db.models import get_session, Mercado, Log, purgar_antiguos
from alerts import telegram as tg

# Máximo de eventos a confirmar con orderbook por ciclo (cada uno = N llamadas).
MAX_DEEP_CHECK_EVENTS = 15

# Cada cuánto podar la DB (snapshots de mercados/logs antiguos). No tiene sentido
# hacerlo cada ciclo de 30s; una vez por hora mantiene el archivo acotado sin
# meter un DELETE en el camino caliente del escaneo.
PURGA_INTERVAL_SEGUNDOS = 3600


class Scanner:
    def __init__(
        self,
        client: PolymarketClient | None = None,
        motor: MotorMatematico | None = None,
        analyzer: Analyzer | None = None,
        executor: Executor | None = None,
        market_maker: MarketMaker | None = None,
    ) -> None:
        self.client = client or PolymarketClient()
        self.motor = motor or MotorMatematico()
        self.analyzer = analyzer or Analyzer()
        self.executor = executor or Executor(client=self.client)
        self.market_maker = market_maker or MarketMaker(client=self.client, motor=self.motor)
        self._corriendo = False
        # Se inicializa "ahora" para que el primer ciclo no pode (la poda inicial
        # la hace main.py al arrancar). A partir de ahí, cada PURGA_INTERVAL.
        self._ultima_purga = time.monotonic()
        # Offset de la ventana rotatoria de confirmación binaria (rate limit).
        self._binario_offset = 0

    # ------------------------------------------------------------------ #
    def escanear_una_vez(self, ejecutar: bool = True) -> List[dict]:
        """Un ciclo de escaneo. Devuelve oportunidades ordenadas."""
        # Comprobación de stop loss al inicio de cada ciclo.
        if risk_manager.verificar_stop_loss():
            tg.notifier.enviar_alerta_stop_loss()

        # Mantenimiento: poda de la DB cada PURGA_INTERVAL (nunca rompe el ciclo).
        self._purgar_si_toca()

        try:
            mercados = self.client.get_markets()
        except Exception as exc:  # noqa: BLE001
            logger.error("Fallo al obtener mercados: %s", exc)
            self._log_db("ERROR", "get_markets falló", {"error": str(exc)})
            return []

        capital = risk_manager.reporte_exposicion()["capital_actual"]
        oportunidades: List[dict] = []

        # Filtro de liquidez + persistencia. Recolecta los que se pueden
        # confirmar con orderbook (necesitan los dos token_ids CLOB).
        confirmables: List[dict] = []
        for m in mercados:
            if m.get("liquidez", 0.0) < settings.min_liquidity:
                continue
            self._persistir_mercado(m)
            if len(m.get("clob_token_ids") or []) >= 2:
                confirmables.append(m)

        # Detección binaria HONESTA: decidir sobre asks REALES del orderbook,
        # no sobre los mids de Gamma (que suman 1.0 por construcción y nunca
        # mostrarían un arb). El rate limit impide confirmar todos cada ciclo,
        # así que se confirma una ventana rotatoria y se registra la medición.
        sumas_reales: List[float] = []
        for m in self._ventana_binaria(confirmables):
            decision, suma_real = self._evaluar_binario_real(m, capital)
            if suma_real is not None:
                sumas_reales.append(suma_real)
            if decision is None:
                continue
            oportunidades.append(decision)
            tg.registrar_oportunidad(decision)
            self._alertar(decision)
            self._log_db("INFO", "Oportunidad detectada", {
                "tipo": decision.get("tipo"),
                "market_id": decision.get("market_id"),
                "ganancia": decision.get("ganancia_minima_usd"),
            })

        # Medición: registra cuán cerca quedó el mejor candidato (visibilidad,
        # para que `logs` deje de estar vacío y se pueda medir la frecuencia).
        if sumas_reales:
            mejor = min(sumas_reales)
            logger.info(
                "Binario: %s confirmados, mejor suma_real=%.4f (arb si <%.2f)",
                len(sumas_reales), mejor, settings.arb_threshold,
            )
            self._log_db("INFO", "Medición binaria", {
                "confirmados": len(sumas_reales),
                "suma_real_min": mejor,
                "umbral_arb": settings.arb_threshold,
                "arbs": sum(1 for s in sumas_reales if s < settings.arb_threshold),
            })

        # Ordenar por ganancia esperada en USD, descendente.
        oportunidades.sort(key=MotorMatematico.ganancia_esperada_usd, reverse=True)

        logger.info(
            "Ciclo completo: %s mercados, %s confirmados, %s oportunidades",
            len(mercados),
            len(sumas_reales),
            len(oportunidades),
        )

        if ejecutar and not risk_manager.pausado:
            # Prioridad 1-2: ejecutar arbitraje y EV detectados.
            for decision in oportunidades:
                op = self.executor.ejecutar(decision)
                if op is None:
                    # Si el riesgo bloquea, lo más probable es que el resto
                    # también se bloquee (capital/cupo). Cortamos el ciclo.
                    break

            # Prioridad 3: market making (solo en paper real, no en dry-run ni
            # live). Es una estrategia aún sin validar y se mantiene aislada.
            if not settings.dry_run and not settings.es_live:
                cotizaciones = self.market_maker.evaluar(mercados)
                for c in cotizaciones:
                    tg.registrar_oportunidad(c)
                    self._log_db("INFO", "Cotización market making", {
                        "market_id": c.get("market_id"),
                        "edge": c.get("edge_neto"),
                    })

        # Arbitraje multi-resultado (eventos). Detección siempre que esté
        # habilitado; ejecución solo si procede. Es estructural y seguro como el
        # binario, por eso corre también en dry-run/live (no como el MM).
        multi = self._escanear_multi(capital, ejecutar and not risk_manager.pausado)
        oportunidades.extend(multi)

        return oportunidades

    # ------------------------------------------------------------------ #
    def _ventana_binaria(self, confirmables: List[dict]) -> List[dict]:
        """Ventana rotatoria de mercados a confirmar con orderbook este ciclo.

        No se pueden confirmar todos cada ciclo (rate limit). Se toma un bloque
        de `deep_check_binary_max` y se avanza el offset, barriendo todo el
        universo a lo largo de varios ciclos."""
        n = len(confirmables)
        if n == 0:
            return []
        k = min(settings.deep_check_binary_max, n)
        off = self._binario_offset % n
        ventana = (confirmables + confirmables)[off:off + k]  # wrap-around
        self._binario_offset = (off + k) % n
        return ventana

    def _evaluar_binario_real(self, m: dict, capital: float):
        """Confirma un mercado con asks REALES del orderbook y decide.

        Devuelve (decision|None, suma_real|None). suma_real es None si falta
        libro en algún lado (no se pudo medir)."""
        toks = m["clob_token_ids"]
        ask_si = self.client.best_ask(toks[0])
        ask_no = self.client.best_ask(toks[1])
        if ask_si is None or ask_no is None:
            return None, None  # sin libro ejecutable: no se decide ni se mide

        prob = self.analyzer.estimar_probabilidad(m)
        vista = MercadoView(
            precio_si=ask_si,
            precio_no=ask_no,
            liquidez=m.get("liquidez", 0.0),
            prob_modelo=prob,
            market_id=m.get("market_id"),
            titulo=m.get("titulo", ""),
            clob_token_ids=toks,
        )
        return self.motor.decision_final(vista, capital), ask_si + ask_no

    # ------------------------------------------------------------------ #
    def _escanear_multi(self, capital: float, ejecutar: bool) -> list:
        """Detecta (y opcionalmente ejecuta) arbitraje multi-resultado."""
        if not settings.multi_arb_enabled:
            return []
        try:
            eventos = self.client.get_events(max_outcomes=settings.multi_arb_max_outcomes)
        except Exception as exc:  # noqa: BLE001
            logger.error("get_events falló: %s", exc)
            return []

        capital_arb = capital * settings.max_position_pct

        # Pre-filtro BARATO con precios Gamma (lay-the-field: Σno < n−1) y nos
        # quedamos solo con los más prometedores: confirmar con el orderbook
        # cuesta N llamadas por evento, así que acotamos por ciclo.
        candidatos = []
        for ev in eventos:
            outcomes = ev["outcomes"]
            n = len(outcomes)
            suma_no = sum(o["no_ask"] for o in outcomes)
            if suma_no <= 0:
                continue
            profit_pre = (n - 1) / suma_no - 1.0 - self.motor.fee
            if profit_pre > settings.multi_arb_min_profit:
                candidatos.append((profit_pre, ev))
        candidatos.sort(key=lambda c: c[0], reverse=True)

        encontradas: list = []
        profits_reales: list = []  # profit a precios REALES (incl. casi-fallos)
        for _profit_pre, ev in candidatos[:MAX_DEEP_CHECK_EVENTS]:
            outcomes = ev["outcomes"]
            n = len(outcomes)

            # Confirmar con precios ejecutables reales del orderbook (NO tokens).
            outcomes_exec = []
            ok = True
            for o in outcomes:
                ask = self.client.best_ask(o["token_no"])
                if ask is None:
                    ok = False
                    break
                outcomes_exec.append({**o, "no_ask": ask})
            if not ok:
                continue

            # Medición: profit lay-the-field a precios REALES, aunque no sea
            # rentable. El pre-filtro usa mids (optimistas); aquí vemos la verdad
            # (recuerda: el 8.68% de Gamma se volvió -15% con asks reales).
            suma_no_real = sum(o["no_ask"] for o in outcomes_exec)
            if suma_no_real > 0:
                profits_reales.append((n - 1) / suma_no_real - 1.0 - self.motor.fee)

            decision = self.motor.calcular_arbitraje_multiple(
                outcomes_exec,
                capital_arb,
                exhaustivo=ev.get("exhaustivo", False),
                max_outcomes=settings.multi_arb_max_outcomes,
                min_profit=settings.multi_arb_min_profit,
            )
            if decision is None:
                continue

            # Defensa en profundidad: un arb estructural real es pequeño. Un
            # profit altísimo = la suposición falla (evento no excluyente, libro
            # incompleto, precios stale). Se omite y se avisa.
            if decision["profit_pct"] > settings.multi_arb_max_profit:
                logger.warning(
                    "[multi] Profit %.1f%% sospechoso en '%s' (>%.0f%%): se omite "
                    "(¿no excluyente / libro incompleto?).",
                    decision["profit_pct"] * 100,
                    ev["titulo"][:40],
                    settings.multi_arb_max_profit * 100,
                )
                continue

            decision["event_id"] = ev["event_id"]
            decision["market_id"] = ev["event_id"]
            decision["titulo"] = ev["titulo"]
            encontradas.append(decision)
            tg.registrar_oportunidad(decision)
            tg.notifier.enviar_mensaje(
                f"🟢 *Multi-arb {decision['forma']}* ({decision['profit_pct']:.2%})\n"
                f"{ev['titulo'][:70]} | {n} resultados"
            )
            self._log_db("INFO", "Multi-arb detectado", {
                "event_id": ev["event_id"],
                "forma": decision["forma"],
                "profit_pct": decision["profit_pct"],
            })
            if ejecutar:
                self.executor.ejecutar(decision)

        if encontradas:
            logger.info("Multi-arb: %s oportunidades", len(encontradas))

        # Medición: cuántos candidatos se confirmaron y el mejor profit REAL,
        # para medir la frecuencia del arb estructural (visibilidad de la fase B).
        if profits_reales:
            mejor = max(profits_reales)
            logger.info(
                "Multi-arb: %s candidatos confirmados, mejor profit_real=%.2f%% "
                "(rentable si >%.0f%%)",
                len(profits_reales), mejor * 100, settings.multi_arb_min_profit * 100,
            )
            self._log_db("INFO", "Medición multi-arb", {
                "confirmados": len(profits_reales),
                "profit_real_max": mejor,
                "min_profit": settings.multi_arb_min_profit,
                "rentables": sum(1 for p in profits_reales if p > settings.multi_arb_min_profit),
            })
        return encontradas

    # ------------------------------------------------------------------ #
    def loop(self) -> None:
        """Bucle infinito con intervalo configurable."""
        self._corriendo = True
        logger.info("Scanner iniciado (intervalo %ss).", settings.scan_interval_seconds)
        while self._corriendo:
            inicio = time.monotonic()
            try:
                self.escanear_una_vez(ejecutar=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Error en ciclo de escaneo: %s", exc)
                self._log_db("ERROR", "Excepción en ciclo", {"error": str(exc)})
            transcurrido = time.monotonic() - inicio
            espera = max(settings.scan_interval_seconds - transcurrido, 1.0)
            time.sleep(espera)

    def detener(self) -> None:
        self._corriendo = False
        logger.info("Scanner detenido.")

    # ------------------------------------------------------------------ #
    def _purgar_si_toca(self) -> None:
        """Poda la DB si pasó PURGA_INTERVAL desde la última. Defensivo: una
        excepción aquí (DB bloqueada, etc.) jamás debe abortar el escaneo."""
        if time.monotonic() - self._ultima_purga < PURGA_INTERVAL_SEGUNDOS:
            return
        self._ultima_purga = time.monotonic()
        try:
            borrado = purgar_antiguos(settings.db_retention_days)
            if borrado["mercados"] or borrado["logs"]:
                logger.info(
                    "Poda DB: %s mercados y %s logs > %sd eliminados.",
                    borrado["mercados"], borrado["logs"], settings.db_retention_days,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Poda de DB falló (se reintentará): %s", exc)

    # ------------------------------------------------------------------ #
    def _alertar(self, decision: dict) -> None:
        tipo = decision.get("tipo")
        if tipo == TIPO_ARBITRAJE_SUMA:
            if decision.get("ganancia_garantizada_pct", 0.0) > settings.min_ev:
                tg.notifier.enviar_alerta_arbitraje(decision)
        elif tipo == TIPO_EV_POSITIVO:
            if decision.get("ev", 0.0) > settings.min_ev:
                tg.notifier.enviar_alerta_ev(decision)

    def _persistir_mercado(self, m: dict) -> None:
        try:
            with get_session() as s:
                s.add(
                    Mercado(
                        market_id=m.get("market_id"),
                        titulo=m.get("titulo", "")[:512],
                        precio_si=m["precio_si"],
                        precio_no=m["precio_no"],
                        volumen=m.get("volumen", 0.0),
                        liquidez=m.get("liquidez", 0.0),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo persistir mercado %s: %s", m.get("market_id"), exc)

    def _log_db(self, nivel: str, mensaje: str, datos: dict | None = None) -> None:
        import json

        try:
            with get_session() as s:
                s.add(
                    Log(
                        nivel=nivel,
                        mensaje=mensaje,
                        datos_json=json.dumps(datos) if datos else None,
                    )
                )
        except Exception:  # noqa: BLE001 - el logging nunca debe romper el scan
            pass
