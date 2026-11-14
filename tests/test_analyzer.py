"""
tests/test_analyzer.py
======================
Pruebas del modelo de probabilidad (estrategia C, EV). Se centran en el caché
del LLM: una estimación por mercado debe reutilizarse (no gastar una llamada
por ciclo), pero un fallo (None) debe poder reintentarse.

Ejecutar:  pytest tests/test_analyzer.py -v
"""

from core.analyzer import LLMProbabilityModel, MarketPriceModel


def _modelo_con_cliente(ttl=9999):
    """LLMProbabilityModel sin pasar por __init__ (evita depender de anthropic)."""
    m = LLMProbabilityModel.__new__(LLMProbabilityModel)
    m._cliente = object()  # truthy: simula cliente disponible
    m._cache = {}
    m._ttl = ttl
    return m


def test_market_price_model_no_tiene_senal():
    assert MarketPriceModel().estimate({"titulo": "x", "precio_si": 0.5}) is None


def test_llm_cachea_por_mercado():
    m = _modelo_con_cliente()
    llamadas = {"n": 0}
    m._consultar_llm = lambda t: (llamadas.__setitem__("n", llamadas["n"] + 1) or 0.42)

    merc = {"market_id": "X", "titulo": "Will X happen?", "precio_si": 0.5}
    assert m.estimate(merc) == 0.42
    assert m.estimate(merc) == 0.42  # segunda vez: viene del caché
    assert llamadas["n"] == 1


def test_llm_no_cachea_none():
    """Un None (fallo transitorio) no debe cachearse: se reintenta."""
    m = _modelo_con_cliente()
    llamadas = {"n": 0}
    m._consultar_llm = lambda t: (llamadas.__setitem__("n", llamadas["n"] + 1) or None)

    merc = {"market_id": "Y", "titulo": "Will Y happen?", "precio_si": 0.5}
    assert m.estimate(merc) is None
    assert m.estimate(merc) is None
    assert llamadas["n"] == 2  # se reintentó, no se cacheó el None


def test_llm_ttl_expira(monkeypatch):
    """Pasado el TTL, vuelve a consultar (la prob del mundo puede cambiar)."""
    import core.analyzer as mod
    m = _modelo_con_cliente(ttl=100)
    llamadas = {"n": 0}
    m._consultar_llm = lambda t: (llamadas.__setitem__("n", llamadas["n"] + 1) or 0.30)

    t = {"v": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: t["v"])
    merc = {"market_id": "Z", "titulo": "Will Z happen?", "precio_si": 0.5}
    assert m.estimate(merc) == 0.30
    t["v"] += 200  # supera el TTL de 100
    assert m.estimate(merc) == 0.30
    assert llamadas["n"] == 2
