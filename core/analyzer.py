"""
core/analyzer.py
================
Estimación de `prob_modelo` para la estrategia de EV positivo.

Filosofía (aprendida a la mala): **sin una señal independiente NO hay EV**. El
precio de mercado ya es la mejor estimación; transformarlo (nudges, suelos)
inventa edge falso y hace perder dinero. Por eso:

    - El modelo por defecto (`MarketPriceModel`) devuelve None -> no opera EV.
    - El primer modelo REAL es `LLMProbabilityModel` (Claude): usa conocimiento
      del mundo, una señal independiente del precio. Aun así debe VALIDARSE en
      paper antes de operar en vivo (EV_LIVE).

La detección de EV solo se activa si `EV_ENABLED=true`. El motor (math_engine)
añade además un requisito de divergencia mínima frente al precio.

Diseño enchufable: implementa `ProbabilityModel.estimate(mercado) -> float|None`
y conéctalo en `Analyzer` para usar tu propio modelo (deportes, elecciones, etc.).
"""

from __future__ import annotations

import time
from typing import Optional, Protocol

from config.settings import settings, logger

# La probabilidad de "¿ocurrirá X?" no cambia cada 30s. Cacheamos la estimación
# del LLM por mercado durante este tiempo para no gastar una llamada por ciclo
# (sin caché: ~20 llamadas/ciclo => decenas de miles/día). Refresca cada 6h.
LLM_CACHE_TTL_SEGUNDOS = 6 * 3600


class ProbabilityModel(Protocol):
    """Interfaz de un estimador de probabilidad."""

    def estimate(self, mercado: dict) -> Optional[float]:
        """Devuelve P(SÍ) en (0,1), o None si no tiene señal para ese mercado."""
        ...


class MarketPriceModel:
    """Modelo honesto por defecto: NO tiene señal propia -> None.

    Existe para dejar explícito que, sin un modelo real, no se opera EV.
    """

    def estimate(self, mercado: dict) -> Optional[float]:  # noqa: D401
        return None


class LLMProbabilityModel:
    """Estimador con Claude. Señal independiente del precio (conocimiento).

    Guardas: devuelve None si la respuesta no es un número válido en (0,1) o si
    la API falla. NO ve el precio de mercado en el prompt, para no anclar la
    estimación al propio precio (evita realimentar el sesgo del mercado).
    """

    def __init__(self, cache_ttl: float = LLM_CACHE_TTL_SEGUNDOS) -> None:
        self._cliente = None
        self._cache: dict = {}  # clave -> (prob, monotonic_ts)
        self._ttl = cache_ttl
        if not settings.anthropic_api_key:
            return
        try:
            import anthropic  # import perezoso

            self._cliente = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        except Exception as exc:  # pragma: no cover
            logger.warning("No se pudo inicializar Anthropic: %s", exc)
            self._cliente = None

    @property
    def disponible(self) -> bool:
        return self._cliente is not None

    def estimate(self, mercado: dict) -> Optional[float]:
        if self._cliente is None:
            return None
        titulo = (mercado.get("titulo") or "").strip()
        if not titulo:
            return None

        # Caché por mercado: evita una llamada al LLM por ciclo (coste). Solo se
        # cachean valores válidos: un None (fallo transitorio) debe reintentarse.
        clave = mercado.get("market_id") or titulo
        hit = self._cache.get(clave)
        if hit is not None and (time.monotonic() - hit[1]) < self._ttl:
            return hit[0]

        prob = self._consultar_llm(titulo)
        if prob is not None:
            self._cache[clave] = (prob, time.monotonic())
        return prob

    def _consultar_llm(self, titulo: str) -> Optional[float]:  # pragma: no cover - red
        """Una llamada al LLM para estimar P(SÍ). None si falla o es inválida.

        NO incluye el precio de mercado en el prompt: anclar la estimación al
        propio precio realimentaría el sesgo del mercado y mataría el edge.
        """
        prompt = (
            "Eres un estimador de probabilidades calibrado. Para el siguiente "
            "evento de un mercado de predicción, estima la probabilidad real de "
            "que ocurra. Responde EXCLUSIVAMENTE con un número entre 0 y 1 "
            "(p. ej. 0.37). No expliques.\n\n"
            f"Evento: {titulo}"
        )
        try:
            resp = self._cliente.messages.create(
                model=settings.llm_model,
                max_tokens=16,
                messages=[{"role": "user", "content": prompt}],
            )
            texto = resp.content[0].text.strip()
            prob = float(texto.split()[0])
            if 0.0 < prob < 1.0:
                return prob
            logger.warning("LLM devolvió prob fuera de rango: %r", texto)
        except Exception as exc:
            logger.warning("Estimación LLM falló: %s", exc)
        return None


class Analyzer:
    """Orquesta el/los modelos de probabilidad.

    Por defecto usa el modelo LLM si EV está habilitado y hay API key; si no,
    el modelo honesto (None). Puedes inyectar tu propio `model`.
    """

    def __init__(self, model: Optional[ProbabilityModel] = None) -> None:
        if model is not None:
            self.model: ProbabilityModel = model
        elif settings.ev_enabled and settings.anthropic_api_key:
            llm = LLMProbabilityModel()
            self.model = llm if llm.disponible else MarketPriceModel()
        else:
            self.model = MarketPriceModel()

    def estimar_probabilidad(self, mercado: dict) -> Optional[float]:
        """Devuelve prob_modelo en (0,1) o None.

        Si EV no está habilitado, nunca devuelve señal (no se opera EV).
        """
        if not settings.ev_enabled:
            return None
        precio_si = mercado.get("precio_si")
        if precio_si is None or not (0.0 < precio_si < 1.0):
            return None
        prob = self.model.estimate(mercado)
        if prob is None:
            return None
        if not (0.0 < prob < 1.0):
            return None
        return prob
