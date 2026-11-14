"""
feeds/polymarket.py
===================
Cliente para la API de Polymarket (CLOB / Gamma):

    - REST paginado de mercados activos:        get_markets()
    - Orderbook de un mercado:                  get_orderbook(market_id)
    - Colocación de órdenes (con modo paper):   place_order(market_id, lado, monto)
    - WebSocket de precios en tiempo real:      stream_prices(...)  (async)
    - Rate limiting integrado y reconexión automática del WS.

Para no acoplar el resto del bot a `py-clob-client`, este módulo expone una
interfaz simple. Si `LIVE_TRADING` está desactivado (o la fase no permite
live), place_order() jamás envía nada real: devuelve un fill simulado.

Endpoints públicos usados (no requieren auth para lectura):
    - Gamma:   https://gamma-api.polymarket.com/markets
    - CLOB:    https://clob.polymarket.com/book
    - WS CLOB: wss://ws-subscriptions-clob.polymarket.com/ws/market
"""

from __future__ import annotations

import json
import time
import threading
from typing import Any, Callable, Dict, List, Optional

import requests

try:  # websockets es opcional para los modos que no usan streaming.
    import asyncio
    import websockets
except Exception:  # pragma: no cover
    websockets = None  # type: ignore

from config.settings import settings, logger


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class RateLimiter:
    """Limitador simple basado en intervalo mínimo entre llamadas (thread-safe)."""

    def __init__(self, max_per_second: float = 5.0) -> None:
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            ahora = time.monotonic()
            delta = ahora - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


class PolymarketClient:
    def __init__(
        self,
        max_per_second: float = 5.0,
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polymarket-bot/1.0"})
        self._rl = RateLimiter(max_per_second)
        self._timeout = timeout
        self._max_retries = max_retries
        self._clob = None  # cliente py-clob-client (lazy, solo LIVE)

    # ------------------------------------------------------------------ #
    # REST con reintentos + rate limiting
    # ------------------------------------------------------------------ #
    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        ultimo_error: Optional[Exception] = None
        for intento in range(1, self._max_retries + 1):
            self._rl.wait()
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
                if resp.status_code == 429:  # rate limited por el servidor
                    espera = min(2 ** intento, 30)
                    logger.warning("429 de Polymarket, esperando %ss", espera)
                    time.sleep(espera)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                # 4xx (salvo 429) es definitivo: no tiene sentido reintentar.
                # P. ej. 404 = ese token no tiene orderbook.
                code = exc.response.status_code if exc.response is not None else None
                if code is not None and 400 <= code < 500 and code != 429:
                    raise RuntimeError(f"GET {url} -> {code} (no reintentable)") from exc
                ultimo_error = exc
                espera = min(2 ** intento, 30)
                logger.warning("Error GET %s (intento %s): %s", url, intento, exc)
                time.sleep(espera)
            except (requests.RequestException, ValueError) as exc:
                ultimo_error = exc
                espera = min(2 ** intento, 30)
                logger.warning("Error GET %s (intento %s): %s", url, intento, exc)
                time.sleep(espera)
        raise RuntimeError(f"GET {url} falló tras {self._max_retries} intentos: {ultimo_error}")

    # ------------------------------------------------------------------ #
    # Mercados
    # ------------------------------------------------------------------ #
    def get_markets(
        self,
        limit_por_pagina: int = 100,
        max_paginas: int = 20,
        solo_activos: bool = True,
    ) -> List[Dict[str, Any]]:
        """Devuelve mercados activos, paginando sobre la Gamma API.

        Normaliza cada mercado a un dict con: market_id, titulo, precio_si,
        precio_no, volumen, liquidez, clob_token_ids.
        """
        mercados: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(max_paginas):
            params = {
                "limit": limit_por_pagina,
                "offset": offset,
                "closed": "false" if solo_activos else "true",
                "active": "true" if solo_activos else "false",
            }
            data = self._get(f"{GAMMA_BASE}/markets", params=params)
            if not data:
                break
            for m in data:
                normalizado = self._normalizar_mercado(m)
                if normalizado is not None:
                    mercados.append(normalizado)
            if len(data) < limit_por_pagina:
                break
            offset += limit_por_pagina
        logger.info("get_markets: %s mercados normalizados", len(mercados))
        return mercados

    @staticmethod
    def _normalizar_mercado(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convierte la respuesta cruda de Gamma a nuestro formato interno."""
        try:
            # outcomePrices llega como string JSON: '["0.62", "0.38"]'
            precios_raw = m.get("outcomePrices") or m.get("outcome_prices")
            if isinstance(precios_raw, str):
                precios = json.loads(precios_raw)
            else:
                precios = precios_raw or []
            if not precios or len(precios) < 2:
                return None
            precio_si = float(precios[0])
            precio_no = float(precios[1])

            tokens_raw = m.get("clobTokenIds")
            if isinstance(tokens_raw, str):
                tokens = json.loads(tokens_raw)
            else:
                tokens = tokens_raw or []

            return {
                "market_id": str(m.get("id") or m.get("conditionId") or ""),
                "titulo": m.get("question") or m.get("title") or "",
                "precio_si": precio_si,
                "precio_no": precio_no,
                "volumen": float(m.get("volume") or 0.0),
                "liquidez": float(m.get("liquidity") or 0.0),
                "clob_token_ids": tokens,
            }
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def get_events(
        self,
        limit_por_pagina: int = 50,
        max_paginas: int = 20,
        min_outcomes: int = 2,
        max_outcomes: int = 8,
    ) -> List[Dict[str, Any]]:
        """Eventos activos con sus resultados agrupados (para arbitraje multi).

        Un evento agrupa varios mercados binarios mutuamente excluyentes (ej.
        "¿Quién gana el Mundial?" -> un mercado por equipo). Normaliza a:
            {event_id, titulo, outcomes: [{titulo, yes_ask, no_ask,
             token_yes, token_no}], exhaustivo}

        Los precios son de Gamma (mid/último), válidos como PRE-FILTRO; la
        ejecución se confirma luego con el orderbook real.

        NOTA: verifica contra tu respuesta real de la Gamma API; la forma de
        `events`/`markets` puede variar entre versiones.
        """
        eventos: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(max_paginas):
            params = {
                "limit": limit_por_pagina,
                "offset": offset,
                "closed": "false",
                "active": "true",
            }
            data = self._get(f"{GAMMA_BASE}/events", params=params)
            if not data:
                break
            for ev in data:
                norm = self._normalizar_evento(ev, min_outcomes, max_outcomes)
                if norm is not None:
                    eventos.append(norm)
            if len(data) < limit_por_pagina:
                break
            offset += limit_por_pagina
        logger.info("get_events: %s eventos multi-resultado normalizados", len(eventos))
        return eventos

    @staticmethod
    def _normalizar_evento(
        ev: Dict[str, Any], min_outcomes: int, max_outcomes: int
    ) -> Optional[Dict[str, Any]]:
        # SEGURIDAD CRÍTICA: lay-the-field exige EXCLUSIÓN MUTUA (exactamente un
        # SÍ). Polymarket la garantiza con `negRisk` (NegRiskAdapter). Sin este
        # filtro, eventos de umbrales anidados ("FDV > X", donde varios resuelven
        # SÍ a la vez) generarían "arbs" del 37% falsos que perderían dinero.
        if not bool(ev.get("negRisk")):
            return None

        mercados = ev.get("markets") or []
        outcomes = []
        for m in mercados:
            norm = PolymarketClient._normalizar_mercado(m)
            if norm is None or len(norm.get("clob_token_ids", [])) < 2:
                continue
            outcomes.append(
                {
                    "titulo": norm["titulo"],
                    "yes_ask": norm["precio_si"],
                    "no_ask": norm["precio_no"],
                    "token_yes": norm["clob_token_ids"][0],
                    "token_no": norm["clob_token_ids"][1],
                }
            )
        if not (min_outcomes <= len(outcomes) <= max_outcomes):
            return None
        return {
            "event_id": str(ev.get("id") or ""),
            "titulo": ev.get("title") or ev.get("slug") or "",
            "outcomes": outcomes,
            "neg_risk": True,  # ya filtrado: evento mutuamente excluyente
            # exhaustivo=False: en el scanner solo confirmamos asks reales de NO
            # (lay-the-field). Habilitar buy-all-yes exigiría asks reales de SÍ.
            "exhaustivo": False,
        }

    def get_orderbook(self, market_id: str) -> Dict[str, Any]:
        """Devuelve el orderbook (bids/asks) de un token CLOB.

        En Polymarket el orderbook se consulta por token_id, no por el id de
        Gamma. Aquí `market_id` debe ser un clob_token_id.
        """
        vacio = {
            "market_id": market_id, "bids": [], "asks": [],
            "liquidez_bid": 0.0, "liquidez_ask": 0.0,
        }
        try:
            data = self._get(f"{CLOB_BASE}/book", params={"token_id": market_id})
        except Exception as exc:  # noqa: BLE001 - un token sin libro no debe romper el scan
            logger.debug("Sin orderbook para %s: %s", market_id, exc)
            return vacio
        bids = data.get("bids", []) if isinstance(data, dict) else []
        asks = data.get("asks", []) if isinstance(data, dict) else []

        def _liquidez(niveles: List[Dict[str, Any]]) -> float:
            total = 0.0
            for n in niveles:
                try:
                    total += float(n["price"]) * float(n["size"])
                except (KeyError, ValueError, TypeError):
                    continue
            return total

        return {
            "market_id": market_id,
            "bids": bids,
            "asks": asks,
            "liquidez_bid": _liquidez(bids),
            "liquidez_ask": _liquidez(asks),
        }

    # ------------------------------------------------------------------ #
    # Mejores precios (para verificar ejecutabilidad antes de operar)
    # ------------------------------------------------------------------ #
    def best_ask(self, token_id: str) -> Optional[float]:
        """Mejor precio de venta (lo que pagas como taker comprador)."""
        ob = self.get_orderbook(token_id)
        asks = ob.get("asks", [])
        precios = [float(a["price"]) for a in asks if "price" in a]
        return min(precios) if precios else None

    def best_bid(self, token_id: str) -> Optional[float]:
        """Mejor precio de compra (lo que recibes como taker vendedor)."""
        ob = self.get_orderbook(token_id)
        bids = ob.get("bids", [])
        precios = [float(b["price"]) for b in bids if "price" in b]
        return max(precios) if precios else None

    # ------------------------------------------------------------------ #
    # Cliente CLOB para firmar/enviar órdenes (lazy)
    # ------------------------------------------------------------------ #
    def _get_clob(self):
        """Inicializa py-clob-client una sola vez (firma EIP-712, gasless)."""
        if self._clob is not None:
            return self._clob
        from py_clob_client.client import ClobClient  # import perezoso

        if not settings.polymarket_private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY no configurada para LIVE.")
        client = ClobClient(
            host=CLOB_BASE,
            key=settings.polymarket_private_key,
            chain_id=137,  # Polygon
        )
        # Deriva (o crea) las credenciales de API a partir de la clave.
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        self._clob = client
        logger.info("Cliente CLOB inicializado para LIVE.")
        return self._clob

    # ------------------------------------------------------------------ #
    # Órdenes (con modo paper)
    # ------------------------------------------------------------------ #
    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
        order_type: str = "FOK",
        market_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Coloca una orden sobre un token CLOB.

        side: "BUY" o "SELL" (dirección sobre ESE token; para arbitraje se hace
              BUY del token SÍ y BUY del token NO).
        price: límite de precio (en [0,1]).
        size_shares: tamaño en shares.
        order_type: "FOK" (taker todo-o-nada, recomendado para arbitraje/EV)
                    o "GTC" (resting, para market making).

        En modo paper devuelve un fill simulado (no envía nada real).
        Devuelve siempre un dict con al menos: {"filled": bool, ...}.
        """
        side = side.upper()
        if not settings.es_live:
            logger.info(
                "[PAPER] place_order %s token=%s price=%.3f size=%.2f (%s)",
                side, token_id, price, size_shares, order_type,
            )
            return {
                "filled": True,
                "status": "PAPER_FILLED",
                "token_id": token_id,
                "market_id": market_id,
                "side": side,
                "price": price,
                "size_shares": size_shares,
                "order_type": order_type,
                "simulado": True,
                "ts": time.time(),
            }

        # --- Camino LIVE ---
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob = self._get_clob()
        side_const = BUY if side == "BUY" else SELL
        ot = OrderType.FOK if order_type.upper() == "FOK" else OrderType.GTC

        args = OrderArgs(
            token_id=token_id,
            price=round(float(price), 3),
            size=round(float(size_shares), 2),
            side=side_const,
        )
        signed = clob.create_order(args)
        resp = clob.post_order(signed, ot)
        return self._parse_fill(resp, token_id, side, price, size_shares, order_type)

    @staticmethod
    def _parse_fill(
        resp: Any,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
        order_type: str,
    ) -> Dict[str, Any]:
        """Normaliza la respuesta de post_order a un dict con 'filled'.

        Best-effort: la forma exacta del payload de py-clob-client puede variar
        entre versiones; verifica contra tu versión en la primera operación real.
        """
        resp = resp or {}
        success = bool(resp.get("success", False))
        status = str(resp.get("status", "")).lower()
        # FOK: o se llena completo o se cancela. Tratamos como lleno si la API
        # reporta éxito y un estado de casado/llenado.
        filled = success and status in {"matched", "filled", "live", "success"}
        return {
            "filled": filled,
            "status": status or ("ok" if success else "rejected"),
            "token_id": token_id,
            "side": side,
            "price": price,
            "size_shares": size_shares,
            "order_type": order_type,
            "order_id": resp.get("orderID") or resp.get("orderId"),
            "raw": resp,
            "simulado": False,
            "ts": time.time(),
        }

    # ------------------------------------------------------------------ #
    # WebSocket de precios en tiempo real
    # ------------------------------------------------------------------ #
    async def stream_prices(
        self,
        token_ids: List[str],
        on_message: Callable[[Dict[str, Any]], None],
        reconnect: bool = True,
    ) -> None:
        """Suscribe a cambios de precio del canal `market` y llama on_message.

        Reconexión automática con backoff exponencial si reconnect=True.
        """
        if websockets is None:  # pragma: no cover
            raise RuntimeError("El paquete 'websockets' no está instalado.")

        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    sub = {"assets_ids": token_ids, "type": "market"}
                    await ws.send(json.dumps(sub))
                    logger.info("WS conectado, suscrito a %s tokens", len(token_ids))
                    backoff = 1  # reset tras conexión exitosa
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        on_message(msg)
            except Exception as exc:  # noqa: BLE001 - queremos reconectar ante cualquier fallo
                if not reconnect:
                    logger.error("WS error sin reconexión: %s", exc)
                    raise
                logger.warning("WS caído (%s). Reconectando en %ss...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
