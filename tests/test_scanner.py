"""
tests/test_scanner.py
====================
Pruebas del scanner con un cliente falso (sin red ni DB real).

Tras el refactor de "detección honesta", el arbitraje binario se decide con los
ASKS REALES del orderbook (best_ask de cada lado), NO con los mids de Gamma
(que en Polymarket suman 1.0 por construcción). El FakeClient provee esos asks.

Ejecutar:  pytest tests/test_scanner.py -v
"""

import pytest

from core.scanner import Scanner
from core.math_engine import MotorMatematico, TIPO_ARBITRAJE_SUMA
from core.analyzer import Analyzer
import core.risk as risk_mod


class FakeClient:
    """Cliente de Polymarket falso.

    `mercados`: lista de dicts de mercado (con clob_token_ids).
    `asks`: dict token_id -> best_ask. Lo que devuelve best_ask() (precio
    ejecutable real). Si un token no está, best_ask devuelve None (sin libro).
    """

    def __init__(self, mercados, asks=None):
        self._mercados = mercados
        self._asks = asks or {}

    def get_markets(self):
        return self._mercados

    def best_ask(self, token_id):
        return self._asks.get(token_id)

    def best_bid(self, token_id):
        return self._asks.get(token_id)

    def get_events(self, max_outcomes=8):
        return []


@pytest.fixture(autouse=True)
def aislar_db_y_riesgo(monkeypatch):
    """Neutraliza accesos a DB y fuerza un estado de riesgo conocido."""
    monkeypatch.setattr(
        risk_mod.risk_manager, "reporte_exposicion",
        lambda: {"capital_actual": 1000.0, "pausado": False},
    )
    monkeypatch.setattr(risk_mod.risk_manager, "verificar_stop_loss", lambda: False)


@pytest.fixture
def motor():
    return MotorMatematico(
        fee=0.02, arb_threshold=0.97, kelly_fraction=0.25,
        max_position_pct=0.10, min_ev=0.05, min_liquidity=200.0,
    )


def _mercado(mid, asks_tokens, liquidez=500.0):
    """Helper: arma un dict de mercado con sus dos tokens CLOB."""
    return {
        "market_id": mid, "titulo": f"m{mid}",
        # Mids de Gamma: irrelevantes para el arb (siempre ~1.0). Lo que decide
        # es best_ask. Los ponemos sumando 1.0 como en la realidad.
        "precio_si": 0.5, "precio_no": 0.5,
        "liquidez": liquidez, "volumen": 1000.0,
        "clob_token_ids": [f"Y{mid}", f"N{mid}"],
    }


def _scanner(mercados, asks, motor):
    s = Scanner(client=FakeClient(mercados, asks), motor=motor, analyzer=Analyzer())
    s._persistir_mercado = lambda m: None
    s._log_db = lambda *a, **k: None
    return s


def test_filtra_por_liquidez(motor):
    # Liquidez insuficiente: ni se confirma con orderbook (no llega al arb).
    mercados = [_mercado("1", None, liquidez=50.0)]
    asks = {"Y1": 0.45, "N1": 0.45}  # sumaría 0.90 (arb) pero la liquidez lo corta
    s = _scanner(mercados, asks, motor)
    ops = s.escanear_una_vez(ejecutar=False)
    assert ops == []


def test_detecta_arbitraje_con_asks_reales(motor):
    # Asks reales suman 0.90 < 0.97 -> arbitraje real.
    mercados = [_mercado("1", None)]
    asks = {"Y1": 0.45, "N1": 0.45}
    s = _scanner(mercados, asks, motor)
    ops = s.escanear_una_vez(ejecutar=False)
    assert len(ops) == 1
    assert ops[0]["tipo"] == TIPO_ARBITRAJE_SUMA


def test_no_hay_arb_si_asks_suman_uno(motor):
    """El caso REAL de Polymarket: asks suman ~1.0 -> NO hay arbitraje."""
    mercados = [_mercado("1", None)]
    asks = {"Y1": 0.51, "N1": 0.50}  # suma 1.01 (spread) -> sin arb
    s = _scanner(mercados, asks, motor)
    ops = s.escanear_una_vez(ejecutar=False)
    assert ops == []


def test_sin_libro_se_descarta(motor):
    """Si un lado no tiene orderbook (best_ask None), no se decide nada."""
    mercados = [_mercado("1", None)]
    asks = {"Y1": 0.45}  # falta N1 -> best_ask(N1)=None
    s = _scanner(mercados, asks, motor)
    ops = s.escanear_una_vez(ejecutar=False)
    assert ops == []


def test_ordena_por_ganancia_esperada(motor):
    mercados = [_mercado("1", None), _mercado("2", None)]
    asks = {
        "Y1": 0.47, "N1": 0.48,  # suma 0.95 -> arb chico
        "Y2": 0.40, "N2": 0.40,  # suma 0.80 -> arb grande
    }
    s = _scanner(mercados, asks, motor)
    ops = s.escanear_una_vez(ejecutar=False)
    assert len(ops) == 2
    ganancias = [o["ganancia_minima_usd"] for o in ops]
    assert ganancias == sorted(ganancias, reverse=True)
    assert ops[0]["market_id"] == "2"


def test_no_ejecuta_en_modo_scan(motor):
    mercados = [_mercado("1", None)]
    asks = {"Y1": 0.45, "N1": 0.45}
    s = _scanner(mercados, asks, motor)
    llamado = {"v": False}
    s.executor.ejecutar = lambda d: llamado.__setitem__("v", True)
    s.escanear_una_vez(ejecutar=False)
    assert llamado["v"] is False


def test_multi_omite_profit_sospechoso(monkeypatch, motor):
    # Un profit altísimo (evento mal modelado) debe descartarse por el techo.
    from config.settings import settings
    monkeypatch.setattr(settings, "multi_arb_enabled", True)

    class FC:
        def get_events(self, max_outcomes=8):
            return [{
                "event_id": "E", "titulo": "raro", "exhaustivo": False,
                "outcomes": [
                    {"titulo": f"o{i}", "yes_ask": 0.05, "no_ask": 0.30,
                     "token_yes": f"Y{i}", "token_no": f"N{i}"}
                    for i in range(3)
                ],
            }]

        def best_ask(self, t):
            return 0.30  # Σno=0.9, n-1=2 -> profit ~120% (irreal) -> omitido

        def best_bid(self, t):
            return 0.29

    s = Scanner(client=FC(), motor=motor, analyzer=Analyzer())
    s._log_db = lambda *a, **k: None
    multi = s._escanear_multi(capital=500.0, ejecutar=False)
    assert multi == []


def test_multi_registra_medicion_en_casi_fallo(monkeypatch, motor):
    """Candidato que pasa el pre-filtro (mids) pero muere con asks reales:
    no es oportunidad, PERO la medición debe quedar registrada (fase B)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "multi_arb_enabled", True)

    class FC:
        def get_events(self, max_outcomes=8):
            # mids no_ask=0.30 (Σ=0.90, n=3 -> profit_pre alto -> pasa pre-filtro)
            return [{
                "event_id": "E", "titulo": "casi", "exhaustivo": False,
                "outcomes": [
                    {"titulo": f"o{i}", "yes_ask": 0.65, "no_ask": 0.30,
                     "token_yes": f"Y{i}", "token_no": f"N{i}"}
                    for i in range(3)
                ],
            }]

        def best_ask(self, t):
            return 0.70  # Σno_real=2.1 -> profit_real<0 -> no rentable

    s = Scanner(client=FC(), motor=motor, analyzer=Analyzer())
    capturado = []
    s._log_db = lambda nivel, msg, datos=None: capturado.append((msg, datos))

    multi = s._escanear_multi(capital=500.0, ejecutar=False)
    assert multi == []  # no hay oportunidad real
    medicion = [d for msg, d in capturado if msg == "Medición multi-arb"]
    assert len(medicion) == 1
    assert medicion[0]["confirmados"] == 1
    assert medicion[0]["rentables"] == 0
    assert medicion[0]["profit_real_max"] < 0


def test_ventana_binaria_rota(motor, monkeypatch):
    """La ventana rotatoria barre el universo en varios ciclos."""
    from config.settings import settings
    monkeypatch.setattr(settings, "deep_check_binary_max", 2)

    mercados = [_mercado(str(i), None) for i in range(5)]
    s = _scanner(mercados, {}, motor)
    # Ventana de 2: ciclo 1 -> {0,1}, ciclo 2 -> {2,3}, ciclo 3 -> {4,0} (wrap).
    v1 = [m["market_id"] for m in s._ventana_binaria(mercados)]
    v2 = [m["market_id"] for m in s._ventana_binaria(mercados)]
    v3 = [m["market_id"] for m in s._ventana_binaria(mercados)]
    assert v1 == ["0", "1"]
    assert v2 == ["2", "3"]
    assert v3 == ["4", "0"]
