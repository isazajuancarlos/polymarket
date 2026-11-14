"""
core/math_engine.py
===================
Motor matemático del bot. Implementa las tres estrategias en orden de
prioridad:

    1. Arbitraje de suma   (ganancia garantizada cuando precio_si + precio_no < umbral)
    2. EV positivo         (Expected Value > umbral, sizing con Kelly conservador)
    3. Market making       (gestionado en core/executor.py sobre la base de estos cálculos)

Todas las funciones son PURAS (sin estado ni efectos secundarios) para poder
testearlas trivialmente. Ver tests/test_math_engine.py.

NOTA SOBRE EL SIZING DEL ARBITRAJE
----------------------------------
Para que un arbitraje de suma sea realmente "ganancia garantizada" hay que
comprar el MISMO número de shares de SÍ y de NO, de forma que cualquiera sea
el desenlace el payout (1 USD por share) sea idéntico.

    shares = capital / suma
    apuesta_si = shares * precio_si = capital * precio_si / suma
    apuesta_no = shares * precio_no = capital * precio_no / suma

Así, resuelva SÍ o NO, recibimos `shares` USD, con coste `capital`. Esto es lo
implementado abajo (corrige la inversión precio_si/precio_no del borrador
original, que dejaba la posición desbalanceada y por tanto NO garantizada).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Any

from config.settings import settings


# Constantes de tipo de oportunidad (evita strings mágicos dispersos).
TIPO_ARBITRAJE_SUMA = "ARBITRAJE_SUMA"
TIPO_ARBITRAJE_MULTIPLE = "ARBITRAJE_MULTIPLE"
TIPO_EV_POSITIVO = "EV_POSITIVO"
TIPO_MARKET_MAKING = "MARKET_MAKING"

# Formas del arbitraje multi-resultado.
FORMA_LAY_FIELD = "LAY_FIELD"        # comprar NO en todos (solo exige exclusión mutua)
FORMA_BUY_ALL_YES = "BUY_ALL_YES"    # comprar SÍ en todos (exige exhaustividad)


@dataclass
class MercadoView:
    """Vista mínima de un mercado que el motor necesita para decidir.

    Permite testear el motor sin depender de SQLAlchemy ni de la API.
    """

    precio_si: float
    precio_no: float
    liquidez: float = 0.0
    prob_modelo: Optional[float] = None
    market_id: Optional[str] = None
    titulo: str = ""
    # IDs de token CLOB [token_SI, token_NO]; necesarios para órdenes LIVE.
    clob_token_ids: Optional[list] = None


class MotorMatematico:
    """Encapsula los cálculos de las estrategias.

    Los umbrales se leen de `settings` por defecto, pero pueden inyectarse
    para tests deterministas.
    """

    def __init__(
        self,
        fee: Optional[float] = None,
        arb_threshold: Optional[float] = None,
        kelly_fraction: Optional[float] = None,
        max_position_pct: Optional[float] = None,
        min_ev: Optional[float] = None,
        min_liquidity: Optional[float] = None,
        mm_min_volume: Optional[float] = None,
        mm_half_spread: Optional[float] = None,
        mm_min_edge: Optional[float] = None,
        mm_skew: Optional[float] = None,
        ev_enabled: Optional[bool] = None,
        ev_min_divergence: Optional[float] = None,
    ) -> None:
        self.fee = settings.fee if fee is None else fee
        self.arb_threshold = settings.arb_threshold if arb_threshold is None else arb_threshold
        self.kelly_fraction = (
            settings.kelly_fraction if kelly_fraction is None else kelly_fraction
        )
        self.max_position_pct = (
            settings.max_position_pct if max_position_pct is None else max_position_pct
        )
        self.min_ev = settings.min_ev if min_ev is None else min_ev
        self.min_liquidity = settings.min_liquidity if min_liquidity is None else min_liquidity
        self.mm_min_volume = settings.mm_min_volume if mm_min_volume is None else mm_min_volume
        self.mm_half_spread = (
            settings.mm_half_spread if mm_half_spread is None else mm_half_spread
        )
        self.mm_min_edge = settings.mm_min_edge if mm_min_edge is None else mm_min_edge
        self.mm_skew = settings.mm_skew if mm_skew is None else mm_skew
        self.ev_enabled = settings.ev_enabled if ev_enabled is None else ev_enabled
        self.ev_min_divergence = (
            settings.ev_min_divergence if ev_min_divergence is None else ev_min_divergence
        )

    # ------------------------------------------------------------------ #
    # Estrategia 1: Arbitraje de suma
    # ------------------------------------------------------------------ #
    def calcular_arbitraje_suma(
        self, precio_si: float, precio_no: float, capital: float
    ) -> Optional[dict]:
        """Detecta arbitraje cuando precio_si + precio_no < arb_threshold.

        Devuelve un dict con el plan de apuestas y la ganancia garantizada,
        o None si no hay oportunidad (o entradas inválidas).
        """
        if precio_si <= 0 or precio_no <= 0 or capital <= 0:
            return None

        suma = precio_si + precio_no
        if suma >= self.arb_threshold:
            return None

        # Ganancia neta por unidad de capital tras restar la fee.
        # payout = capital / suma  ; coste = capital ; fee sobre la suma de precios.
        ganancia_pct = (1.0 - suma - self.fee) / suma
        if ganancia_pct <= 0:
            return None  # tras fee ya no es rentable

        # Compra el MISMO número de shares de cada lado => hedge perfecto.
        apuesta_si = capital * precio_si / suma
        apuesta_no = capital * precio_no / suma

        return {
            "tipo": TIPO_ARBITRAJE_SUMA,
            "suma": suma,
            "precio_si": precio_si,
            "precio_no": precio_no,
            "ganancia_garantizada_pct": ganancia_pct,
            "apuesta_si": apuesta_si,
            "apuesta_no": apuesta_no,
            "ganancia_minima_usd": capital * ganancia_pct,
            "capital_comprometido": capital,
        }

    # ------------------------------------------------------------------ #
    # Estrategia 1b: Arbitraje multi-resultado (estructural)
    # ------------------------------------------------------------------ #
    def calcular_arbitraje_multiple(
        self,
        outcomes: list,
        capital: float,
        exhaustivo: bool = False,
        max_outcomes: int = 8,
        min_profit: float = 0.01,
    ) -> Optional[dict]:
        """Arbitraje en un evento con N resultados mutuamente excluyentes.

        Cada `outcome` es un dict con al menos:
            'yes_ask' (precio ejecutable de comprar SÍ),
            'no_ask'  (precio ejecutable de comprar NO),
        y opcionalmente 'token_yes', 'token_no', 'titulo'.

        Dos formas (se elige la mejor válida):

        - **Lay the field** (`FORMA_LAY_FIELD`): comprar NO en los N resultados.
          Como exactamente uno resuelve SÍ (exclusión mutua), N−1 de los NO
          pagan 1 → payout ≥ N−1 *pase lo que pase*. Garantizado SIN suponer
          exhaustividad. Rentable si  Σ no_ask · (1+fee) < N−1.

        - **Buy all YES** (`FORMA_BUY_ALL_YES`): comprar SÍ en los N resultados.
          Si el evento es **exhaustivo** (alguno resuelve SÍ seguro), el ganador
          paga 1 → rentable si  Σ yes_ask · (1+fee) < 1. Solo se evalúa con
          `exhaustivo=True` (si no, podría no resolver ninguno y perderlo todo).

        Compra el MISMO nº de shares `s` en cada pata (hedge estructural).
        Devuelve la mejor oportunidad o None.
        """
        n = len(outcomes)
        if n < 2 or n > max_outcomes or capital <= 0:
            return None

        yes = [o.get("yes_ask") for o in outcomes]
        no = [o.get("no_ask") for o in outcomes]
        if any(p is None or not (0.0 < p < 1.0) for p in no):
            return None

        candidatas = []

        # Lay the field (siempre seguro bajo exclusión mutua).
        suma_no = sum(no)
        # profit por unidad de capital desplegado: payout/cost − 1 − fee.
        if suma_no > 0:
            profit_lay = (n - 1) / suma_no - 1.0 - self.fee
            if profit_lay > min_profit:
                s = capital / suma_no  # shares por pata; coste total = capital
                legs = [
                    {
                        "token_id": o.get("token_no"),
                        "side": "BUY",
                        "outcome": "NO",
                        "price": no[i],
                        "shares": s,
                        "titulo": o.get("titulo", ""),
                    }
                    for i, o in enumerate(outcomes)
                ]
                candidatas.append((profit_lay, FORMA_LAY_FIELD, s, legs))

        # Buy all YES (solo si el evento es exhaustivo).
        if exhaustivo and all(p is not None and 0.0 < p < 1.0 for p in yes):
            suma_yes = sum(yes)
            if suma_yes > 0:
                profit_yes = 1.0 / suma_yes - 1.0 - self.fee
                if profit_yes > min_profit:
                    s = capital / suma_yes
                    legs = [
                        {
                            "token_id": o.get("token_yes"),
                            "side": "BUY",
                            "outcome": "SI",
                            "price": yes[i],
                            "shares": s,
                            "titulo": o.get("titulo", ""),
                        }
                        for i, o in enumerate(outcomes)
                    ]
                    candidatas.append((profit_yes, FORMA_BUY_ALL_YES, s, legs))

        if not candidatas:
            return None

        profit, forma, s, legs = max(candidatas, key=lambda c: c[0])
        return {
            "tipo": TIPO_ARBITRAJE_MULTIPLE,
            "forma": forma,
            "n": n,
            "profit_pct": profit,
            "shares": s,
            "legs": legs,
            "capital_comprometido": capital,
            "ganancia_minima_usd": capital * profit,
        }

    # ------------------------------------------------------------------ #
    # Estrategia 2: Expected Value
    # ------------------------------------------------------------------ #
    def calcular_ev(self, prob_modelo: float, precio_mercado: float) -> float:
        """EV por unidad apostada en una apuesta binaria estilo Polymarket.

        b = ganancia neta por unidad si acierta = (1 - precio) / precio
        EV = prob * b - (1 - prob)
        """
        if precio_mercado <= 0 or precio_mercado >= 1:
            return -1.0
        b = (1.0 - precio_mercado) / precio_mercado
        return (prob_modelo * b) - (1.0 - prob_modelo)

    def kelly_criterion(self, prob: float, precio: float, capital: float) -> float:
        """Tamaño de apuesta en USD usando Kelly fraccionado y conservador.

        f* = (prob * b - q) / b      (Kelly completo)
        f_conservador = f* * kelly_fraction
        f_final = min(f_conservador, max_position_pct)   (cap por operación)

        Devuelve siempre un valor >= 0 (nunca apuesta si f* es negativo).
        """
        if precio <= 0 or precio >= 1 or capital <= 0:
            return 0.0

        b = (1.0 - precio) / precio
        q = 1.0 - prob
        f_star = (prob * b - q) / b
        f_conservador = f_star * self.kelly_fraction
        f_final = min(f_conservador, self.max_position_pct)
        return max(capital * f_final, 0.0)

    # ------------------------------------------------------------------ #
    # Estrategia 3: Market making
    # ------------------------------------------------------------------ #
    def calcular_market_making(
        self,
        fair_value: float,
        volumen: float,
        capital: float,
        inventario_shares: float = 0.0,
    ) -> Optional[dict]:
        """Genera cotizaciones bid/ask para proveer liquidez y capturar spread.

        Idea
        ----
        Postamos una compra (bid) por debajo del fair value y una venta (ask)
        por encima. Cada round-trip captura `spread = ask - bid`. Solo es
        rentable si ese spread supera el coste de fees en ambos lados más un
        margen de seguridad (adverse selection):

            bid  = fair - half_spread - skew
            ask  = fair + half_spread - skew
            spread     = ask - bid
            coste_fees = fee * (bid + ask)          # fee en cada lado ejecutado
            edge_neto  = spread - coste_fees

        Gestión de inventario
        ----------------------
        `skew` desplaza ambas cotizaciones para revertir el inventario a 0: si
        ya acumulamos shares (inventario_shares > 0) bajamos las quotes para
        favorecer la venta, y viceversa. La intensidad la fija `mm_skew`.

        Filtros
        -------
        - Solo mercados de alto volumen (`volumen >= mm_min_volume`).
        - Solo si `edge_neto > mm_min_edge`.

        Devuelve un dict con las cotizaciones y el tamaño por lado, o None.
        """
        if not (0.0 < fair_value < 1.0) or capital <= 0:
            return None
        if volumen < self.mm_min_volume:
            return None  # market making solo en alto volumen

        half = self.mm_half_spread

        # Tamaño máximo por lado (en USD) y su equivalente en shares al fair.
        size_usd = capital * self.max_position_pct
        max_inv_shares = size_usd / fair_value if fair_value > 0 else 0.0

        # Skew proporcional al inventario relativo, acotado a [-half, half].
        inv_rel = 0.0
        if max_inv_shares > 0:
            inv_rel = max(-1.0, min(1.0, inventario_shares / max_inv_shares))
        skew = self.mm_skew * inv_rel * half

        bid = fair_value - half - skew
        ask = fair_value + half - skew

        # Mantener las cotizaciones dentro de (0, 1) y a ambos lados del fair.
        bid = max(0.01, min(bid, fair_value - 0.001))
        ask = min(0.99, max(ask, fair_value + 0.001))

        spread = ask - bid
        coste_fees = self.fee * (bid + ask)
        edge_neto = spread - coste_fees
        if edge_neto <= self.mm_min_edge:
            return None

        return {
            "tipo": TIPO_MARKET_MAKING,
            "fair_value": fair_value,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "edge_neto": edge_neto,
            "skew": skew,
            "inventario_shares": inventario_shares,
            "size_usd_por_lado": size_usd,
            # Ganancia esperada por round-trip completo si ambas patas se llenan.
            "ganancia_minima_usd": size_usd * edge_neto,
        }

    # ------------------------------------------------------------------ #
    # Decisión combinada (aplica prioridades)
    # ------------------------------------------------------------------ #
    def decision_final(self, mercado: Any, capital: float) -> Optional[dict]:
        """Devuelve la mejor acción para un mercado, respetando prioridades.

        `mercado` debe exponer: precio_si, precio_no, liquidez y (opcional)
        prob_modelo. Acepta tanto MercadoView como un modelo ORM con esos
        atributos.
        """
        # --- Prioridad 1: arbitraje de suma ---
        # Limitamos el capital del arb a max_position_pct del total.
        capital_arb = capital * self.max_position_pct
        arb = self.calcular_arbitraje_suma(
            mercado.precio_si, mercado.precio_no, capital_arb
        )
        if arb and getattr(mercado, "liquidez", 0.0) >= self.min_liquidity:
            arb["market_id"] = getattr(mercado, "market_id", None)
            arb["titulo"] = getattr(mercado, "titulo", "")
            arb["clob_token_ids"] = getattr(mercado, "clob_token_ids", None)
            return arb

        # --- Prioridad 2: EV positivo con Kelly ---
        # Gateado: solo si EV está habilitado Y el modelo discrepa del precio por
        # un margen real (no ruido). Sin esto el EV NO opera (lección del bug del
        # 1900%: nunca operar sobre señal que no supera claramente al mercado).
        prob_modelo = getattr(mercado, "prob_modelo", None)
        if self.ev_enabled and prob_modelo is not None:
            divergencia = abs(prob_modelo - mercado.precio_si)
            ev = self.calcular_ev(prob_modelo, mercado.precio_si)
            if (
                ev > self.min_ev
                and divergencia >= self.ev_min_divergence
                and getattr(mercado, "liquidez", 0.0) >= self.min_liquidity
            ):
                apuesta = self.kelly_criterion(prob_modelo, mercado.precio_si, capital)
                if apuesta > 0:
                    return {
                        "tipo": TIPO_EV_POSITIVO,
                        "ev": ev,
                        "divergencia": divergencia,
                        "apuesta": apuesta,
                        "precio_si": mercado.precio_si,
                        "prob_modelo": prob_modelo,
                        "market_id": getattr(mercado, "market_id", None),
                        "titulo": getattr(mercado, "titulo", ""),
                        "clob_token_ids": getattr(mercado, "clob_token_ids", None),
                        "ganancia_minima_usd": apuesta * ev,
                    }

        return None

    # ------------------------------------------------------------------ #
    # Utilidad de ranking (para el scanner)
    # ------------------------------------------------------------------ #
    @staticmethod
    def ganancia_esperada_usd(decision: dict) -> float:
        """Métrica común para ordenar oportunidades heterogéneas."""
        return float(decision.get("ganancia_minima_usd", 0.0))
